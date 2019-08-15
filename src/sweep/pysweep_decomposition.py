#Programmer: Anthony Walker
#This file contains all of the necessary functions for implementing process management
# and data decomposition for the swept rule.
import numpy as np
import pycuda.driver as cuda
from pycuda.compiler import SourceModule
#MPI imports
from mpi4py import MPI
import importlib
#System imports
import os.path as op
import inspect

def create_CPU_sarray(comm,arr_shape,dType,arr_bytes):
    """Use this function to create shared memory arrays for node communication."""
    itemsize = int(dType.itemsize)
    #Creating MPI Window for shared memory
    win = MPI.Win.Allocate_shared(arr_bytes, itemsize, comm=comm)
    shared_buf, itemsize = win.Shared_query(0)
    arr = np.ndarray(buffer=shared_buf, dtype=dType.type, shape=arr_shape)
    return arr

def get_affinity_slices(affinity,block_size,arr_shape):
    """Use this function to split the given data based on rank information and the affinity.
    affinity -  a value between zero and one. (GPU work/CPU work)/Total Work
    block_size -  gpu block size
    arr_shape - shape of array initial conditions array (v,x,y)
    ***This function splits to the nearest column, so the affinity may change***
    """
    #Getting number of GPU blocks based on affinity
    blocks_per_column = arr_shape[2]/block_size[1]
    blocks_per_row = arr_shape[1]/block_size[0]
    num_blocks = int(blocks_per_row*blocks_per_column)
    gpu_blocks = round(affinity*num_blocks)
    #Rounding blocks to the nearest column
    col_mod = gpu_blocks%blocks_per_column  #Number of blocks ending in a column
    col_perc = col_mod/blocks_per_column    #What percentage of the column
    gpu_blocks += round(col_perc)*blocks_per_column-col_mod #Round to the closest column and add the appropriate value
    #Getting number of columns and rows
    num_columns = int(gpu_blocks/blocks_per_column)
    #Region Indicies
    gpu_slices = (slice(0,arr_shape[0],1),slice(0,int(block_size[0]*num_columns),1),)
    cpu_slices = (slice(0,arr_shape[0],1),slice(int(block_size[0]*num_columns),arr_shape[1],1),slice(0,arr_shape[2],1))
    return gpu_slices, cpu_slices

def boundary_update(shared_arr,ops):
    """Use this function to update the boundary point ghost nodes."""
    shared_arr[0,:,:ops,:] = shared_arr[0,:,-2*ops:-ops,:]
    shared_arr[0,:,:,:ops] = shared_arr[0,:,:,-2*ops:-ops]
    shared_arr[0,:,-ops:,:] = shared_arr[0,:,ops:2*ops,:]
    shared_arr[0,:,:,-ops:] = shared_arr[0,:,:,ops:2*ops]

def create_write_region(comm,rank,master,total_ranks,block_size,arr_shape,slices,time_steps,ops):
    """Use this function to split regions amongst the architecture."""
    y_blocks = arr_shape[2]/block_size[1]
    blocks_per_rank = round(y_blocks/total_ranks)
    rank_blocks = (blocks_per_rank*(rank),blocks_per_rank*(rank+1))
    rank_blocks = comm.gather(rank_blocks,root=master)
    if rank == master:
        rem = int(y_blocks-rank_blocks[-1][1])
        if rem > 0:
            ct = 0
            for i in range(rem):
                rank_blocks[i] = (rank_blocks[i][0]+ct,rank_blocks[i][1]+1+ct)
                ct +=1

            for j in range(rem,total_ranks):
                rank_blocks[j] = (rank_blocks[j][0]+ct,rank_blocks[j][1]+ct)
        elif rem < 0:
            ct = 0
            for i in range(rem,0,1):
                rank_blocks[i] = (rank_blocks[i][0]+ct,rank_blocks[i][1]-1+ct)
                ct -=1
    rank_blocks = comm.scatter(rank_blocks,root=master)
    x_slice = slice(slices[1].start+ops,slices[1].stop+ops,1)
    y_slice = slice(int(block_size[1]*rank_blocks[0]+ops),int(block_size[1]*rank_blocks[1]+ops),1)
    return (slice(0,time_steps,1),slices[0],x_slice,y_slice)

def create_read_region(region,ops):
    """Use this function to obtain the regions to for reading and writing
        from the shared array. region 1 is standard region 2 is offset by split.
        Note: The rows are divided into regions. So, each rank gets a row or set of rows
        so to speak. This is because affinity split is based on columns.
    """
    #Read Region
    new_region = region[:2]
    new_region += slice(region[2].start-ops,region[2].stop+ops,1),
    new_region += slice(region[3].start-ops,region[3].stop+ops,1),
    return new_region

def get_slices_shape(slices):
    """Use this function to convert slices into a shape tuple."""
    stuple = tuple()
    for s in slices:
        stuple+=(s.stop-s.start,)
    return stuple

def create_local_array(shared_arr,region,dType):
    """Use this function to generate the local arrays from regions."""
    local_shape = get_slices_shape(region)
    local_array = np.zeros(local_shape,dtype=dType)
    local_array[:,:,:,:] = shared_arr[region]
    return local_array


def build_cpu_source(cpu_source):
    """Use this function to build source module from cpu code."""
    module_name = cpu_source.split("/")[-1]
    module_name = module_name.split(".")[0]
    spec = importlib.util.spec_from_file_location(module_name, cpu_source)
    source_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source_mod)
    return source_mod

def build_gpu_source(kernel_source):
    """Use this function to build the given and swept source module together.
    """
    #GPU Swept Calculations
    #----------------Reading In Source Code-------------------------------#
    file = inspect.getfile(build_cpu_source)
    fname = file.split("/")[-1]
    fpath = op.abspath(inspect.getabsfile(build_cpu_source))[:-len(fname)]+"sweep.h"
    source_code = source_code_read(fpath)
    split_source_code = source_code.split("//!!(@#\n")
    source_code = split_source_code[0]+"\n"+source_code_read(kernel_source)+"\n"+split_source_code[1]
    source_mod = SourceModule(source_code)#,options=["--ptxas-options=-v"])
    return source_mod

def source_code_read(filename):
    """Use this function to generate a multi-line string for pycuda from a source file."""
    with open(filename,"r") as f:
        source = """\n"""
        line = f.readline()
        while line:
            source+=line
            line = f.readline()
    f.closed
    return source

def constant_copy(source_mod,const_dict,add_const=None):
    """Use this function to copy constant args to cuda memory.
        source_mod - the source module obtained from pycuda and source code
        const_dict - dictionary of constants where the key is the global
        add_const - additional dictionary with constants. Note, they should have
                    the correct type.
    """
    #Functions to cast data to appropriate gpu types
    int_cast = lambda x:np.int32(x)
    float_cast = lambda x:np.float32(x)
    casters = {type(0.1):float_cast,type(1):int_cast}
    #Generating constants
    for key in const_dict:
        c_ptr,_ = source_mod.get_global(key)
        cst = const_dict[key]
        cuda.memcpy_htod(c_ptr,casters[type(cst)](cst))

    for key in add_const:
        c_ptr,_ = source_mod.get_global(key)
        cuda.memcpy_htod(c_ptr,add_const[key])

def create_bridge_regions(shared_shape,region,SPLITX,SPLITY):
    """Use this function to create bridges."""
    x_bridge = region[:2]
    x_bridge += (slice(region[2].start+SPLITX,region[2].stop+SPLITX,1),region[3])

    if x_bridge[2].stop > shared_shape[2]:
        x_bridge1 = x_bridge[:2]+(slice(x_bridge[2].start,shared_shape[2],1),x_bridge[3])
        x_bridge2 = x_bridge[:2]+(slice(0,x_bridge[2].stop-shared_shape[2],1),x_bridge[3])
        x_bridge = (x_bridge1,x_bridge2)
    else:
        x_bridge = x_bridge,

    y_bridge = region[:3]
    y_bridge += (slice(region[3].start+SPLITX,region[3].stop+SPLITX,1),)
    if y_bridge[3].stop > shared_shape[3]:
        y_bridge1 = y_bridge[:3]+(slice(y_bridge[3].start,shared_shape[3],1),)
        y_bridge2 = y_bridge[:3]+(slice(0,y_bridge[3].stop-shared_shape[3],1),)
        y_bridge = (y_bridge1,y_bridge2)
    else:
        y_bridge = y_bridge,

    return x_bridge, y_bridge

def create_iidx_sets(block_size,ops):
    """Use this function to create index sets."""
    bsx = block_size[0]+2*ops
    bsy = block_size[1]+2*ops
    ly = ops
    uy = bsy-ops
    min_bs = int(min(bsx,bsy)/(2*ops))
    iidx = tuple(np.ndindex((bsx,bsy)))
    idx_sets = tuple()
    for i in range(min_bs):
        iidx = iidx[ops*(bsy-i*2*ops):-ops*(bsy-i*2*ops)]
        iidx = [(x,y) for x,y in iidx if y >= ly and y < uy]
        if len(iidx)>0:
            idx_sets+=(iidx,)
        ly+=ops
        uy-=ops
    return idx_sets

def create_bridge_sets(mbx,mby,block_size,ops,MPSS):
    """Use this function to create the iidx sets for bridges."""
    bsx = block_size[0]+2*ops
    bsy = block_size[1]+2*ops
    ly = ops+ops   #This first block with be in ops, plus the ghost points
    lx = (MPSS-1)*ops+ops    #This first block with be in ops, plus the ghost points
    uy = bsy-ops-ops
    ux = lx+2*ops
    min_bs = int(min(bsx,bsy)/(2*ops))
    iidx = tuple(np.ndindex((bsx,bsy)))
    riidx = [iidx[(x)*bsy:(x+1)*bsy] for x in range(bsx)]
    x_bridge = tuple()
    for i in range(MPSS-1):
        temp = tuple()
        for row in (riidx[ly:uy]):
            temp+=row[lx:ux][:]
        x_bridge+=temp,
        lx-=ops
        ux+=ops
        ly+=ops
        uy-=ops

    # Finding the second bridge - if the block is symmetric then it is the inverse
    if block_size[0] == block_size[1]:
        y_bridge = x_bridge[::-1]
    else:
        lx = ops+ops   #This first block with be in ops, plus the ghost points
        ly = (MPSS-1)*ops+ops    #This first block with be in ops, plus the ghost points
        ux = bsx-ops-ops
        uy = ly+2*ops
        y_bridge = tuple()
        for i in range(MPSS-1):
            temp = tuple()
            for row in (riidx[ly:uy]):
                temp+=row[lx:ux][:]
            y_bridge+=temp,
            ly-=ops
            uy+=ops
            lx+=ops
            ux-=ops
    return x_bridge, y_bridge

def create_shift_regions(wregion,SPLITX,SPLITY,shared_shape,mod=0):
    """Use this function to create a shifted region(s)."""
    #Conditions
    asx = shared_shape[2]-mod
    asy = shared_shape[3]-mod
    wsx = wregion[2].stop + SPLITX
    wsy = wregion[3].stop + SPLITY

    c1 = wsx > asx
    c2 =  wsy > asy
    sregion = tuple()
    if c1 and c2:
        sr1 = wregion[:2]+(slice(wregion[2].start+SPLITX,asx,1),slice(wregion[3].start+SPLITX,asy,1),)
        sr2 = wregion[:2]+(slice(mod,wsx+mod-asx,1),slice(mod,wsy+mod-asy,1),)
        sregion = (sr1,sr2)
    elif c1:
        sr1 = wregion[:2]+(slice(wregion[2].start+SPLITX,asx,1),slice(wregion[3].start+SPLITY,wsy,1))
        sr2 = wregion[:2]+(slice(mod,wsx+mod-asx,1),slice(wregion[3].start+SPLITY,wsy,1))
        sregion = (sr1,sr2)
        print(sregion)
    elif c2:
        sr1 = wregion[:2]+(slice(wregion[2].start+SPLITX,wsx,1),slice(wregion[3].start+SPLITX,asy,1))
        sr2 = wregion[:2]+(slice(wregion[2].start+SPLITX,wsx,1),slice(mod,wsy+mod-asy,1))
        sregion = (sr1,sr2)
    else:
        sregion = (wregion[:2]+(slice(wregion[2].start+SPLITX,wregion[2].stop+SPLITX,1),slice(wregion[3].start+SPLITY,wregion[3].stop+SPLITY,1)),)
    return sregion

def write_and_shift(shared_arr,region1,hdf_set,ops,MPSS,GST):
    """Use this function to write to the hdf file and shift the shared array
        # data after writing."""
    r2 = slice(region1[2].start-ops,region1[2].stop-ops,1)
    r3 = slice(region1[3].start-ops,region1[3].stop-ops,1)
    hdf_set[MPSS*(GST-1):MPSS*(GST),region1[1],r2,r3] = shared_arr[:MPSS,region1[1],region1[2],region1[3]]
    shared_arr[:MPSS+1,region1[1],region1[2],region1[3]] = shared_arr[MPSS+1:,region1[1],region1[2],region1[3]]
    #Do edge comm after this function



# def edge_comm(shared_arr,SPLITX,SPLITY,ops,dir):
#     """Use this function to communicate edges in the shared array."""
#     #Updates shifted section of shared array
#     if not dir:
#         shared_arr[:,:,-SPLITX-ops:,:] = shared_arr[:,:,ops:SPLITX+2*ops,:]
#         shared_arr[:,:,:,-SPLITY-ops:] = shared_arr[:,:,:,ops:SPLITY+2*ops]
#     else:
#         shared_arr[:,:,ops:SPLITX+2*ops,:]=shared_arr[:,:,-SPLITX-ops:,:]
#         shared_arr[:,:,:,ops:SPLITY+2*ops]=shared_arr[:,:,:,-SPLITY-ops:]
#     #Updates ops points at front
#     shared_arr[:,:,:ops,:] = shared_arr[:,:,-SPLITX-2*ops:-SPLITX-ops,:]
#     shared_arr[:,:,:,:ops] = shared_arr[:,:,:,-SPLITY-2*ops:-SPLITY-ops]


def create_blocks_list(arr_shape,block_size,ops):
    """Use this function to create a list of blocks from the array."""
    bsx = int((arr_shape[2]-2*ops)/block_size[0])
    bsy =  int((arr_shape[3]-2*ops)/block_size[1])
    slices = []
    c_slice = (slice(0,arr_shape[0],1),slice(0,arr_shape[1],1),)
    for i in range(ops+block_size[0],arr_shape[2],block_size[0]):
        for j in range(ops+block_size[1],arr_shape[3],block_size[1]):
            t_slice = c_slice+(slice(i-block_size[0]-ops,i+ops,1),slice(j-block_size[1]-ops,j+ops,1))
            slices.append(t_slice)
    return slices


def rebuild_blocks(arr,blocks,local_regions,ops):
    """Use this funciton to rebuild the blocks."""
    #Rebuild blocks into array
    if len(blocks)>1:
        for ct,lr in enumerate(local_regions):
            lr2 = slice(lr[2].start+ops,lr[2].stop-ops,1)
            lr3 = slice(lr[3].start+ops,lr[3].stop-ops,1)
            arr[:,:,lr2,lr3] = blocks[ct][:,:,ops:-ops,ops:-ops]
        return arr
    else:
        return blocks[0]
