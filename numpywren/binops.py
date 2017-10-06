import boto3
import itertools
import numpy as np
from .matrix import BigSymmetricMatrix, BigMatrix
from .matrix_utils import load_mmap, chunk, generate_key_name_binop
from .matrix_init import local_numpy_init
import concurrent.futures as fs
import math
import os
import pywren
from pywren.executor import Executor
from scipy.linalg import cholesky, solve
import time


def _gemm_remote_0(block_pairs, XY, X, Y, reduce_idxs=[0]):
    for bp in block_pairs:
        bidx_0, bidx_1 = bp
        XY_block = None
        for r in reduce_idxs:
            block1 = X.get_block(bidx_0, r)
            block2 = Y.get_block(r, bidx_1)
            if (XY_block is None):
                XY_block = block1.dot(block2)
            else:
                XY_block += block1.dot(block2)
        XY.put_block(XY_block, bidx_0, bidx_1)

def gemm(pwex, X, Y, out_bucket=None, tasks_per_job=1, local=False):

    '''
        Compute XY return
        @param pwex - Execution context
        @param X - rhs matrix
        @param Y - lhs matrix
        @param tasks_per_job - number of tasks per job
        @param out_bucket - bucket job writes to
        @param num_jobs - how many lambdas to run
        @param local - run locally? #TODO remove once local pywren executor is provided
    '''
    # 0 -> 1 or 1 -> 0
    reduce_idxs = Y._block_idxs(axis=1)
    if (out_bucket == None):
        out_bucket = X.bucket

    root_key = generate_key_name_binop(X, Y, "gemm")

    if (X.key == Y.key and (X.transposed ^ Y.transposed)):
        XY = BigSymmetricMatrix(root_key, shape=(X.shape[0], X.shape[0]), bucket=out_bucket, shard_sizes=[X.shard_sizes[0], X.shard_sizes[0]])
    else:
        XY = BigMatrix(root_key, shape=(X.shape[0], Y.shape[0]), bucket=out_bucket, shard_sizes=[X.shard_sizes[0], Y.shard_sizes[0]])

    num_out_blocks = len(XY.blocks)
    num_jobs = int(num_out_blocks/float(tasks_per_job))

    print("Total number of output blocks", len(XY.block_idxs))
    print("Total number of output blocks that exist", len(XY.blocks_exist))

    block_idxs_to_map = list(set(XY.block_idxs))

    print("Number of output blocks to generate ", len(block_idxs_to_map))

    chunked_blocks = list(chunk(list(chunk(block_idxs_to_map, tasks_per_job)), num_jobs))


    def pywren_run(x):
        return _gemm_remote_0(x, XY, X, Y, reduce_idxs=reduce_idxs)

    all_futures = []
    for i, c in enumerate(chunked_blocks):
        print("Submitting job for chunk {0} in axis 0".format(i))
        if (local):
            list(map(pywren_run, c))
        else:
            s = time.time()
            futures = pwex.map(pywren_run, c)
            e = time.time()
            print("Pwex Map Time {0}".format(e - s))
            all_futures.append((i,futures))

    if (local):
        return XY

    for i, futures, in all_futures:
        pywren.wait(futures)
        [f.result() for f in futures]

    return XY

# matrix vector multiply
# hard
def gemv(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

# symmetric rank k update
# hard
def syrk(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

# very hard
def posv(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

def block_matmul_update(L, X, L_bb_inv, block_0_idx, block_1_idx):
    L_bb_inv = L_bb_inv.numpy()
    X_block = X.get_block(block_0_idx, block_1_idx)
    L_block = X_block.dot(L_bb_inv.T)
    L.put_block(L_block, block_1_idx, block_0_idx)
    return 0

def syrk_update(L, X, block_0_idx, block_1_idx, block_2_idx):
    block_0_idx = X.get_block(block_1_idx, block_0_idx)
    block_1_idx = X.get_block(block_2_idx, block_0_idx)
    old_block = X.get_block(block_1_idx, block_1_idx)
    update = old_block - update
    L.put_block(update, block_0_idx, block_1_idx)
    return 0

def chol(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    if (out_bucket == None):
        out_bucket = X.bucket
    out_key = generate_key_name_binop(X, Y, "chol")
    L = BigMatrix(out_key, shape=(X.shape[0], X.shape[0]), bucket=out_bucket, shard_sizes=[X.shard_sizes[0], X.shard_sizes[0]])
    all_blocks = list(L.block_idxs)
    for i in X._block_idxs(0):
        if (i == 0):
            diag_block = X.get_block(i,i)
            A = X
        else:
            diag_block = L.get_block(i,i)
            A = L
        L_bb = cholesky(diag_block)
        print(L.put_block(L_bb, i, i))
        L_bb_inv = solve(L_bb, np.eye(L_bb.shape[0]))
        L_bb_inv_bigm = local_numpy_init(L_bb_inv, L_bb_inv.shape)
        def pywren_run(x):
            return block_matmul_update(L, A, L_bb_inv_bigm, *x)
        column_blocks = [block for block in all_blocks if (block[0] == i and block[1] > i)]
        print("COLUMN BLOCKS",column_blocks)
        futures = pwex.map(pywren_run, column_blocks)
        pywren.wait(futures)
        [f.result() for f in futures]
        def pywren_run_2(x):
            return syrk_update(L, A, block_0_idx, block_1_idx, block)
        other_blocks = [block for block in all_blocks if (block[0] > i and block[1] > i)]
        print("TRAILING BLOCKS", other_blocks)
        futures = pwex.map(pywren_run, column_blocks)
        pywren.wait(futures)
        [f.result() for f in futures]
        L_bb_inv_bigm.free()
    return L.T



# easy
def add(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

# easy
def sub(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

# easy
def mul(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

# easy
def div(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

def logical_and(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

def logical_or(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

def xor(pwex, X, Y, out_bucket=None, tasks_per_job=1):
    raise NotImplementedError

def elemwise_binop_func(pwex, X, Y, f, out_bucket=None, tasks_per_job=1, local=False):
    raise NotImplementedError

