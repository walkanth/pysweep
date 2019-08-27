
#Programmer: Anthony Walker
#PySweep is a package used to implement the swept rule for solving PDEs

#System imports
import os
import sys

#Writing imports
import h5py

#DataStructure and Math imports
import math
import numpy as np
from collections import deque

#CUDA Imports
import pycuda.driver as cuda
import pycuda.autoinit  #Cor debugging only
from pycuda.compiler import SourceModule
# import pycuda.gpuarray as gpuarray

#MPI imports
from mpi4py import MPI

#Multiprocessing Imports
import multiprocessing as mp
import ctypes

#GPU Utility Imports
import GPUtil

#Swept imports
from .pysweep_lambda import sweep_lambda
from .pysweep_functions import *
from .pysweep_decomposition import *
from .pysweep_block import *
from .pysweep_regions import *
from .pysweep_source import *
from .pysweep_printer import *
import importlib.util
#Testing and Debugging
import warnings
import time as timer
warnings.simplefilter("ignore") #THIS IGNORES WARNINGS


def sweep(arr0,gargs,swargs,dType=np.dtype('float32'),filename ="results",exid=[]):
    """Use this function to perform swept rule
    args:
    arr0 -  3D numpy array of initial conditions (v (variables), x,y)
    gargs:   (global args)
        The first three variables must be time arguments (t0,tf,dt).
        Subsequent variables can be anything passed to the source code in the
        "set_globals" function
    swargs: (Swept args)
        TSO - order of the time scheme
        OPS - operating points to remove from each side for a function step
            (e.g. a 5 point stencil would result in OPS=2).
        BS - an integer which represents the x and y dimensions of the gpu block size
        AF -  the GPU affinity (GPU work/CPU work)/TotalWork
        GS - GPU source code file
        CS - CPU source code file
    dType: data type for the run (e.g. np.dtype("float32")).
    filename: Name of the output file excluding hdf5
    exid: GPU ids to exclude from the calculation.
    """
    #Unpacking arguments
    t0,tf,dt = gargs[:3]
    TSO,OPS,BS,AF,GS,CS = swargs
    BS = (BS,BS,1)
    start = timer.time()
    #Local Constants
    ZERO = 0
    QUARTER = 0.25
    HALF = 0.5
    ONE = 1
    TWO = 2
    TOPS = TWO*OPS
    #Getting GPU info
    #-------------MPI Set up----------------------------#
    comm = MPI.COMM_WORLD
    processor = MPI.Get_processor_name()
    master_rank = ZERO #master rank
    num_ranks = comm.Get_size() #number of ranks
    rank = comm.Get_rank()  #current rank

    #-----------------------------INITIAL SPLIT OF DOMAIN------------------------------------#
    if rank == master_rank:
        #Determining the split between gpu and cpu
        gpu_slices,cpu_slices = get_affinity_slices(AF,BS,arr0.shape)
        #Getting gpus
        if AF>0:
            gpu_rank = GPUtil.getAvailable(order = 'load',excludeID=exid,limit=1e8) #getting devices by load
            gpu_rank = [(True,id) for id in gpu_rank]
            num_gpus = len(gpu_rank)
        else:
            gpu_rank = []
            num_gpus = 0
        gpu_rank += [(False,None) for i in range(num_gpus,num_ranks)]
        gpu_ranks = np.array([i for i in range(ZERO,num_gpus)])
        cpu_ranks = np.array([i for i in range(num_gpus,num_ranks)])
        #Update boundary points
    else:
        gpu_rank = None
        gpu_ranks = None
        cpu_ranks = None
        gpu_slices = None
        cpu_slices = None
    #-------------------------------INITIAL COLLECTIVE COMMUNICATION
    gpu_rank = comm.scatter(gpu_rank,root=master_rank)
    gpu_ranks = comm.bcast(gpu_ranks,root=master_rank)
    cpu_ranks = comm.bcast(cpu_ranks,root=master_rank)
    gpu_slices = comm.bcast(gpu_slices,root=master_rank)
    cpu_slices = comm.bcast(cpu_slices,root=master_rank)
    #GPU rank- MPI Variables
    if AF > 0:
        gpu_group  = comm.group.Incl(gpu_ranks)
        gpu_comm = comm.Create_group(gpu_group)
        gpu_master = ZERO

    #Destroys cpu processes if AF is one
    if AF == 1:
        comm = comm.Create_group(gpu_group)
        if rank not in gpu_ranks:
            MPI.Finalize()
            exit(0)
    else:
        #CPU rank- MPI Variables
        cpu_group  = comm.group.Incl(cpu_ranks)
        cpu_comm = comm.Create_group(cpu_group)
        cpu_master = ZERO
    #Number of each architecture
    num_gpus = len(gpu_ranks)
    num_cpus = len(cpu_ranks)
    #---------------------SWEPT VARIABLE SETUP----------------------$
    #Splits for shared array
    SPLITX = int(BS[ZERO]/TWO)   #Split computation shift - add OPS
    # SPLITX = SPLITX if SPLITX%2==0 else SPLITX-1
    SPLITY = int(BS[ONE]/TWO)   #Split computation shift
    # SPLITY = SPLITY if SPLITY%2==0 else SPLITY-1
    up_sets = create_up_sets(BS,OPS)
    down_sets = create_down_sets(BS,OPS)
    oct_sets = down_sets+up_sets[1:]
    MPSS = len(up_sets)
    MOSS = len(oct_sets)
    bridge_sets, bridge_slices = create_bridge_sets(BS,OPS,MPSS)
    #----------------Data Input setup -------------------------#
    time_steps = int((tf-t0)/dt)  #Number of time steps
    #Global swept step
    GST = ZERO #This is a variable to track which swept step the simulation is on
    MGST = TSO*int(np.ceil(time_steps/MOSS))    #THIS ASSUMES THAT time_steps > MOSS
    time_steps = MGST*MOSS+1 #Updating time steps
    #-----------------------SHARED ARRAY CREATION----------------------#
    #Create shared process array for data transfer  - TWO is added to shared shaped for IC and First Step
    shared_shape = (MOSS+TSO+ONE,arr0.shape[ZERO],arr0.shape[ONE]+TOPS+SPLITX,arr0.shape[TWO]+TOPS+SPLITY)
    sarr = create_CPU_sarray(comm,shared_shape,dType,np.prod(shared_shape)*dType.itemsize)
    #Fill shared array and communicate initial boundaries
    if rank == master_rank:
        sarr[TSO-ONE,:,OPS:arr0.shape[ONE]+OPS,OPS:arr0.shape[TWO]+OPS] = arr0[:,:,:]
        boundary_update(sarr,OPS,SPLITX,SPLITY)   #communicate boundaries
    #------------------------------DECOMPOSITION BY REGION CREATION--------------#
    GRB = gpu_rank[ZERO]
    #Splits regions up amongst architecture
    if GRB:
        gri = gpu_comm.Get_rank()
        cri = None
        WR = create_write_region(gpu_comm,gri,gpu_master,num_gpus,BS,arr0.shape,gpu_slices,shared_shape[0],OPS)
    else:
        cri = cpu_comm.Get_rank()
        gri = None
        WR = create_write_region(cpu_comm,cri,cpu_master,num_cpus,BS,arr0.shape,cpu_slices,shared_shape[0],OPS)
    #---------------------_REMOVING UNWANTED RANKS---------------------------#
    #Checking if rank is useful
    if WR[3].start == WR[3].stop or WR[2].start == WR[2].stop:
        include_ranks = None
    else:
        include_ranks = rank
    #Gathering results
    include_ranks = comm.gather(include_ranks,root=master_rank)
    if rank == master_rank:
        include_ranks = list(filter(lambda a: a is not None, include_ranks))
    #Distributing new results
    include_ranks = comm.bcast(include_ranks,root=master_rank)
    #Creating new comm group
    comm_group = comm.group.Incl(include_ranks)
    #Killing unwanted processes
    if rank not in include_ranks:
        MPI.Finalize()
        exit(0)
    #Updating comm
    comm = comm.Create_group(comm_group)
    #Sync all processes - Ensures boundaries are updated
    comm.Barrier()
    #--------------------------CREATING OTHER REGIONS--------------------------#
    RR = create_read_region(WR,OPS)   #Create read region
    SRR,SWR,XR,YR = create_shift_regions(RR,SPLITX,SPLITY,shared_shape,OPS)  #Create shifted read region
    BDR,ERS = create_boundary_regions(WR,SPLITX,SPLITY,OPS,shared_shape,bridge_slices)
    xtr,ytr = create_bridges(WR,SPLITX,SPLITY,OPS,shared_shape,bridge_slices,BS)
    xtrr,ytrr = create_rev_bridges(WR,SPLITX,SPLITY,OPS,shared_shape,bridge_slices,BS)

    #--------------------------------CREATING LOCAL ARRAYS-----------------------------------------#
    larr = create_local_array(sarr,RR,dType)
    xarr = create_local_array(sarr,XR,dType)
    yarr = create_local_array(sarr,YR,dType)

    #---------------Generating Source Modules----------------------------------#
    if GRB:
        # Creating cuda device and Context
        cuda.init()
        cuda_device = cuda.Device(gpu_rank[ONE])
        cuda_context = cuda_device.make_context()
        #Creating local GPU array with split
        GRD = (int((larr.shape[TWO]-TOPS)/BS[ZERO]),int((larr.shape[3]-TOPS)/BS[ONE]))   #Grid size
        #Creating constants
        NV = larr.shape[ONE]
        SGIDS = (BS[ZERO]+TWO*OPS)*(BS[ONE]+TWO*OPS)
        STS = SGIDS*NV #Shared time shift
        VARS =  larr.shape[TWO]*larr.shape[3]
        TIMES = VARS*NV
        const_dict = ({"NV":NV,"SGIDS":SGIDS,"VARS":VARS,"TIMES":TIMES,"SPLITX":SPLITX,"SPLITY":SPLITY,"MPSS":MPSS,"MOSS":MOSS,"OPS":OPS,"TSO":TSO,"STS":STS})
        #Building CUDA source code
        SM = build_gpu_source(GS)
        swept_constant_copy(SM,const_dict)
        cpu_SM = build_cpu_source(CS)   #Building cpu source for set_globals
        cpu_SM.set_globals(GRB,SM,*gargs)
        del cpu_SM
        CRS = None
    else:
        SM = build_cpu_source(CS) #Building Python source code
        SM.set_globals(GRB,SM,*gargs)
        CRS = create_blocks_list(larr.shape,BS,OPS)
        GRD = None

    #------------------------------HDF5 File------------------------------------------#
    filename+=".hdf5"
    hdf5_file = h5py.File(filename, 'w', driver='mpio', comm=comm)
    hdf_bs = hdf5_file.create_dataset("BS",(len(BS),))
    hdf_bs[:] = BS[:]
    hdf_as = hdf5_file.create_dataset("array_size",(len(arr0.shape)+ONE,))
    hdf_as[:] = (time_steps,)+arr0.shape
    hdf_aff = hdf5_file.create_dataset("AF",(ONE,))
    hdf_aff[ZERO] = AF
    hdf_time = hdf5_file.create_dataset("time",(1,))
    hdf5_data_set = hdf5_file.create_dataset("data",(time_steps+ONE,arr0.shape[ZERO],arr0.shape[ONE],arr0.shape[TWO]),dtype=dType)
    hregion = (WR[1],slice(WR[2].start-OPS,WR[2].stop-OPS,1),slice(WR[3].start-OPS,WR[3].stop-OPS,1))
    hdf5_data_set[0,hregion[0],hregion[1],hregion[2]] = sarr[TSO-ONE,WR[1],WR[2],WR[3]]
    cwt = 1 #Current write time
    wb = 1  #Counter for writing on the appropriate step
    comm.Barrier() #Ensure all processes are prepared to solve

    printer = pysweep_printer(rank,master_rank)

    #-------------------------------SWEPT RULE---------------------------------------------#
    pargs = (SM,GRB,BS,GRD,CRS,OPS,TSO) #Passed arguments to the swept functions
    #-------------------------------FIRST PYRAMID-------------------------------------------#
    UpPyramid(sarr,larr,WR,BDR,up_sets,wb,pargs) #THis modifies shared array
    comm.Barrier()
    #-------------------------------FIRST BRIDGE-------------------------------------------#
    #Getting x and y arrays
    xarr[:,:,:,:] = sarr[XR]
    yarr[:,:,:,:] = sarr[YR]
    comm.Barrier()  #Barrier after read
    #Bridge Step
    Bridge(sarr,xarr,yarr,XR,YR,(xtr,ytr),bridge_sets,wb,pargs) #THis modifies shared array
    comm.Barrier()  #Solving Bridge Barrier
    #------------------------------SWEPT LOOP-------------------------------#
    #Getting next points for the local array
    larr[:,:,:,:] = sarr[SRR]
    #Swept Octahedrons and Bridges
    for GST in range(1,MGST):
        comm.Barrier()  #Read barrier for local array
        #-------------------------------FIRST OCTAHEDRON (NONSHIFT)-------------------------------------------#
        #Octahedron Step
        Octahedron(sarr,larr,SWR,tuple(),oct_sets,wb,pargs)
        comm.Barrier()  #Solving Barrier
        #Shifting Data Step
        edge_shift(sarr,ERS,ONE)
        comm.Barrier()  #Communication Barrier
        #Writing Step
        cwt,wb = hdf_swept_write(cwt,wb,sarr,WR,hdf5_data_set,hregion,MPSS,TSO)
        comm.Barrier()  #Write Barrier
        #Updating Boundary Conditions Step
        boundary_update(sarr,OPS,SPLITX,SPLITY) #Communicate all boundaries
        comm.Barrier()

        #-------------------------------FIRST REVERSE BRIDGE-------------------------------------------#
        #Getting reverse x and y arrays
        xarr[:,:,:,:] = sarr[YR] #Regions are purposely switched here
        yarr[:,:,:,:] = sarr[XR]
        comm.Barrier()  #Barrier after read
        #Reverse Bridge Step
        Bridge(sarr,xarr,yarr,YR,XR,(xtrr,ytrr),bridge_sets,wb,pargs) #THis modifies shared array
        comm.Barrier()  #Solving Bridge Barrier

        #-------------------------------SECOND OCTAHEDRON (SHIFT)-------------------------------------------#
        #Getting next points for the local array
        larr[:,:,:,:] = sarr[RR]
        comm.Barrier()
        #Octahedron Step
        Octahedron(sarr,larr,WR,BDR,oct_sets,wb,pargs)
        comm.Barrier()  #Solving Barrier
        #Write step
        cwt,wb = hdf_swept_write(cwt,wb,sarr,WR,hdf5_data_set,hregion,MPSS,TSO)
        comm.Barrier()
        #Updating Boundary Conditions Step
        boundary_update(sarr,OPS,SPLITX,SPLITY) #Communicate all boundaries
        comm.barrier()  #Barrier following data write
        #-------------------------------SECOND BRIDGE (NON-REVERSED)-------------------------------------------#
        #Getting x and y arrays
        xarr[:,:,:,:] = sarr[XR]
        yarr[:,:,:,:] = sarr[YR]
        comm.Barrier()  #Barrier after read
        #Bridge Step
        Bridge(sarr,xarr,yarr,XR,YR,(xtr,ytr),bridge_sets,wb,pargs) #THis modifies shared array
        comm.Barrier()
        #Getting next points for the local array
        larr[:,:,:,:] = sarr[SRR]
    #Last read barrier for down pyramid
    comm.Barrier()
    #--------------------------------------DOWN PYRAMID------------------------#
    DownPyramid(sarr,larr,SWR,down_sets,wb,pargs)
    comm.Barrier()
    #Shifting Data Step
    edge_shift(sarr,ERS,ONE)
    comm.Barrier()  #Communication Barrier
    #Writing Step
    hdf_swept_write(cwt,wb,sarr,WR,hdf5_data_set,hregion,MPSS,TSO)
    comm.Barrier()  #Write Barrier
    # CUDA clean up - One of the last steps
    if GRB:
        cuda_context.pop()
    comm.Barrier()
    #-------------------------------TIMING-------------------------------------#
    stop = timer.time()
    exec_time = abs(stop-start)
    exec_time = comm.gather(exec_time)
    if rank == master_rank:
        avg_time = sum(exec_time)/num_ranks
        hdf_time[0] = avg_time
    comm.Barrier()
    # Close file
    hdf5_file.close()
    comm.Barrier()
    #This if for testing only
    if rank == master_rank:
        return avg_time
