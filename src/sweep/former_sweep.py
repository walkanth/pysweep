def archs_phase_1(block_size,num_cpu,num_gpu):
    """Use this function to determine the array splits for the first phase (grid1)"""
    #Axes
    x_ax = 1
    y_ax = 2
    #Getting shape of grid
    grid_shape = np.shape(grid)
    #Creating work element partition integers
    par_x = int(grid_shape[x_ax]/block_size[x_ax])
    par_y = int(grid_shape[y_ax]/block_size[y_ax])
    #Split in y
        #Split in x
            #Add to list

def archs_phase_2(block_size,num_cpu,num_gpu):
    """Use this function to determine the array splits for the second phase (grid2)"""
    #Axes
    x_ax = 1
    y_ax = 2
    #Getting shape of grid
    grid_shape = np.shape(grid)
    #Creating work element partition integers
    par_x = int(grid_shape[x_ax]/block_size[x_ax])
    par_y = int(grid_shape[y_ax]/block_size[y_ax])
    # print("Par(x,y,t): ",par_x,", ",par_y,", ",par_t) #Printing partitions

### ----------------------------- COMPLETED FUNCTIONS -----------------------###
def arch_work_blocks(plane_shape,block_size,gpu_affinity):
    """Use to determine the number of blocks for gpus vs cpus."""
    blocks_x = int(plane_shape[0]/block_size[0])
    blocks_y = int(plane_shape[1]/block_size[1])
    total_blocks = blocks_x*blocks_y
    gpu_blocks = round(total_blocks/(1+1/gpu_affinity))
    block_mod_y = gpu_blocks%blocks_y
    if  block_mod_y!= 0:
        gpu_blocks+=blocks_y-block_mod_y
    cpu_blocks = total_blocks-gpu_blocks
    return (gpu_blocks,cpu_blocks)

def arch_query():
    """Use this method to query the system for information about its hardware"""
    #-----------------------------MPI setup--------------------------------
    comm = MPI.COMM_WORLD
    master_rank = 0 #master rank
    num_ranks = comm.Get_size() #number of ranks
    rank = comm.Get_rank()  #current rank
    rank_info = None    #Set to none for later gather
    cores = mp.cpu_count()  #getting cores of each rank with multiprocessing package

                            #REMOVE ME AFTER TESTING
    #####################################################################
    #Subtracting cores for those used by virtual cluster.
    # if rank == master_rank:
    #     cores -= 14
    #####################################################################

    #Getting avaliable GPUs with GPUtil
    gpu_ids = GPUtil.getAvailable(order = 'load',excludeID=[1],limit=10)
    gpus = 0
    gpu_id = None
    if gpu_ids:
        gpus = len(gpu_ids)
        gpu_id = gpu_ids[0]
    #Gathering all of the information to the master rank
    rank_info = comm.gather((rank, cores, gpus,gpu_id),root=0)
    if rank == master_rank:
        gpu_sum = 0
        cpu_sum = 0
        #Getting total number of cpu's and gpu
        for ri in rank_info:
            cpu_sum += ri[1]
            gpu_sum += ri[2]
            if ri[-1] is not None:
                gpu_id = ri[3]
        return gpu_sum, cpu_sum, gpu_id
    return None,None,gpu_id