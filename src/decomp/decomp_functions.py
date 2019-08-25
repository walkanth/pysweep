#Programmer: Anthony Walker
#This file contains functions necessary to implement the standard decomposition
import numpy as np
import pycuda.driver as cuda
from pycuda.compiler import SourceModule
from .decomp_lambda import decomp_lambda
#MPI imports
from mpi4py import MPI
import importlib
#System imports
import os.path as op
import inspect

#Multiprocessing
import multiprocessing as mp

def create_local_array(shared_arr,region,dType):
    """Use this function to generate the local arrays from regions."""
    local_shape = get_slices_shape(region)
    local_array = np.zeros(local_shape,dtype=dType)
    local_array[:,:,:,:] = shared_arr[region]
    return local_array

def edge_comm(shared_arr,ops):
    """Use this function to communicate edges in the shared array."""
    #Shifting data down
    shared_arr[0,:,:,:] = shared_arr[1,:,:,:]
    #Updates shifted section of shared array
    shared_arr[:,:,-ops:,:] = shared_arr[:,:,ops:2*ops,:]
    shared_arr[:,:,:ops,:] = shared_arr[:,:,ops:2*ops,:]
    shared_arr[:,:,:,-ops:] = shared_arr[:,:,:,ops:2*ops]
    shared_arr[:,:,:,:ops] = shared_arr[:,:,:,ops:2*ops]

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
    fpath = op.abspath(inspect.getabsfile(build_cpu_source))[:-len(fname)]+"decomp.h"
    source_code = source_code_read(kernel_source)
    split_source_code = source_code_read(fpath).split("//!!(@#\n")
    source_code = split_source_code[0]+"\n"+source_code+"\n"+split_source_code[1]
    source_mod = SourceModule(source_code)
    return source_mod

def get_slices_shape(slices):
    """Use this function to convert slices into a shape tuple."""
    stuple = tuple()
    for s in slices:
        stuple+=(s.stop-s.start,)
    return stuple

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

def get_gpu_ranks(rank_info,affinity):
    """Use this funciton to determine which ranks with have a GPU.
        given: rank_info - a list of tuples of (rank, processor, gpu_ids, gpu_rank)
        returns: new_rank_inf, total_gpus
    """
    total_gpus = 0
    new_rank_info = list()
    assigned_ctr = dict()
    devices = dict()
    for ri in rank_info:
        temp = ri
        if ri[1] not in assigned_ctr:
            assigned_ctr[ri[1]] = len(ri[2])
            ri[2].reverse()
            devices[ri[1]] = ri[2].copy()   #Device ids
            total_gpus+=len(ri[2])
        if assigned_ctr[ri[1]] > 0 and affinity > 0:
            temp = ri[:-2]+(True,)+(devices[ri[1]].pop(),)
            assigned_ctr[ri[1]]-=1
        new_rank_info.append(temp)
    return new_rank_info, total_gpus

def create_CPU_sarray(comm,arr_shape,dType,arr_bytes):
    """Use this function to create shared memory arrays for node communication."""
    itemsize = int(dType.itemsize)
    #Creating MPI Window for shared memory
    win = MPI.Win.Allocate_shared(arr_bytes, itemsize, comm=comm)
    shared_buf, itemsize = win.Shared_query(0)
    arr = np.ndarray(buffer=shared_buf, dtype=dType.type, shape=arr_shape)
    return arr

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

def decomp_constant_copy(source_mod,const_dict):
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

def decomposition(source_mod,arr,gpu_rank,block_size,grid_size,region,local_cpu_regions,shared_arr,ops):
    """
    This is the starting pyramid for the 2D heterogeneous swept rule cpu portion.
    arr-the array that will be solved (t,v,x,y)
    fcn - the function that solves the problem in question
    ops -  the number of atomic operations
    """
    if gpu_rank:
        #Getting GPU Function
        arr = np.ascontiguousarray(arr) #Ensure array is contiguous
        gpu_fcn = source_mod.get_function("Decomp")
        ss = np.zeros(arr[0,:,:block_size[0]+2*ops,:block_size[1]+2*ops].shape)
        gpu_fcn(cuda.InOut(arr),grid=grid_size, block=block_size,shared=ss.nbytes)
        cuda.Context.synchronize()
    else:   #CPUs do this
        blocks = []
        for local_region in local_cpu_regions:
            block = np.zeros(arr[local_region].shape)
            block += arr[local_region]
            blocks.append(block)
        cpu_fcn = decomp_lambda((CPU_Decomp,source_mod,ops))
        blocks = list(map(cpu_fcn,blocks))
        arr = rebuild_blocks(arr,blocks,local_cpu_regions,ops)
    #Writing to shared array
    shared_arr[region] = arr[:,:,ops:-ops,ops:-ops]

#--------------------------------CPU Specific Swept Functions------------------
def CPU_Decomp(args):
    """Use this function to build the Up Pyramid."""
    block,source_mod,ops = args
    #Removing elements for swept step
    iidx = tuple(np.ndindex((block.shape[2],block.shape[3])))
    iidx = iidx[block.shape[3]*ops:-block.shape[3]*ops]
    iidx = [(x,y) for x,y in iidx if y >= ops and y < block.shape[3]-ops]
    ts = 0
    block = source_mod.step(block,iidx,ts)
    return block

def nan_to_zero(arr,zero=0.):
    """Use this function to turn nans to zero."""
    for i in np.ndindex(arr.shape):
        if np.isnan(arr[i]):
            arr[i]=zero
    return arr
