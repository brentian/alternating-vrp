"""
functional interface module for bcd
% consider the model:
%   min c'x
%     s.t. Ax<=b, Bx<=d, x \in {0,1}
%       - A: binding part
%       - B: block diagonal decomposed part
% ALM:
%   min c'x+rho*\|max{Ax-b+lambda/rho,0}\|^2
%     s.t. Bx<=d, x \in {0,1}
% implement the BCD to solve ALM (inc. indefinite proximal version),
% - ordinary linearized proximal BCD
% - indefinite proximal BCD which includes an extrapolation step.
% - restart utilities
"""
import functools
from typing import Dict
import time
import numpy as np
import scipy
import scipy.sparse.linalg as ssl
import tqdm
from gurobipy import *

from route import Route


# BCD params
class BCDParams(object):

    def __init__(self):
        self.kappa = 0.2
        self.alpha = 1.0
        self.beta = 1
        self.gamma = 0.1  # parameter for argmin x
        self.changed = 0
        self.num_stuck = 0
        self.eps_num_stuck = 3
        self.iter = 0
        self.lb = 1e-6
        self.lb_arr = []
        self.ub_arr = []
        self.gap = 1
        self.dual_method = "pdhg"  # "lagrange" or "pdhg"
        self.primal_heuristic_method = "jsp"  # "jsp" or "seq"
        self.feasible_provider = "jsp"  # "jsp" or "seq"
        self.max_number = 1
        self.norms = ([], [], [])
        self.multipliers = ([], [], [])
        self.itermax = 10000
        self.linmax = 10

        self.parse_environ()

    def parse_environ(self):
        import os
        self.primal_heuristic_method = os.environ.get('primal', None)
        self.dual_method = os.environ.get('dual', 'pdhg_alm')

    def update_bound(self, lb):
        if lb >= self.lb:
            self.lb = lb
            self.changed = 1
            self.num_stuck = 0
        else:
            self.changed = 0
            self.num_stuck += 1

        if self.num_stuck >= self.eps_num_stuck:
            self.kappa *= 0.5
            self.num_stuck = 0
        self.lb_arr.append(lb)

    def update_incumbent(self, ub):
        self.ub_arr.append(ub)

    def update_gap(self):
        _best_ub = min(self.ub_arr)
        _best_lb = max(self.lb_arr)
        self.gap = (_best_ub - _best_lb) / (abs(_best_lb) + 1e-3)

    def reset(self):
        self.num_stuck = 0
        self.eps_num_stuck = 3
        self.iter = 0
        self.lb = 1e-6
        self.lb_arr = []
        self.ub_arr = []
        self.gap = 1
        self.dual_method = "pdhg"  # "lagrange" or "pdhg"
        self.primal_heuristic_method = "jsp"  # "jsp" or "seq"
        self.feasible_provider = "jsp"  # "jsp" or "seq"
        self.max_number = 1
        self.norms = ([], [], [])  # l1-norm, l2-norm, infty-norm
        self.multipliers = ([], [], [])
        self.parse_environ()


def _Ax(block, x):
    return block['A'] @ x


@np.vectorize
def _nonnegative(x):
    return max(x, 0)


def show_log_header():
    headers = ["k", "t", "c'x", "lobj", "|Ax - b|", "error", "rho", "tau", "iter"]
    slots = ["{:^3s}", "{:^7s}", "{:^9s}", "{:^9s}", "{:^10s}", "{:^10s}", "{:^9s}", "{:^9s}", "{:4s}"]
    _log_header = " ".join(slots).format(*headers)
    lt = _log_header.__len__()
    print("*" * lt)
    print(("{:^" + f"{lt}" + "}").format("BCD for MILP"))
    print(("{:^" + f"{lt}" + "}").format("(c) Chuwen Zhang, Shanwen Pu, Rui Wang"))
    print(("{:^" + f"{lt}" + "}").format("2022"))
    print("*" * lt)
    print(_log_header)
    print("*" * lt)


def optimize(bcdpar: BCDParams, block_data: Dict, route: Route):
    """

    Args:
        bcdpar: BCDParam
        block_data:  matlab dict storing bcd-styled block vrp instance
            self.block_data["A"] = []  # couple A
            self.block_data["b"] = np.ones((len(V) - 1, 1))
            self.block_data["B"] = []  # sub A
            self.block_data["q"] = []  # sub b
            self.block_data["c"] = []  # demand
            self.block_data["C"] = []  # capacity
            self.block_data["d"] = []  # obj coeff
    Note:
        % basic model:
            self.block_data["B"] = []  # sub A
            self.block_data["q"] = []  # sub b
        % capacity:
            self.block_data["c"] = []  # demand
            self.block_data["C"] = []  # capacity
        % time window:
            self.block_data['M'], self.block_data['T'],
            self.block_data['a'], self.block_data['b']
    Returns:

    """
    # data
    start = time.time()
    A, b, B, q = block_data['A'], block_data['b'], block_data['B'], block_data['q']
    c, C, d = block_data['c'], block_data['C'], block_data['d']
    M, T, l, u = block_data['M'], block_data['T'], block_data['l'], block_data['u']
    # query model size
    A1 = A[0]
    m, n = A1.shape
    nblock = len(A)
    Anorm = 20  # scipy.sparse.linalg.norm(A) / 10

    # alias
    rho = 1e-2
    tau = 1 / (Anorm ** 2 * rho)
    sigma = 2
    xk = [np.ones((n, 1)) for _ in A]
    lbd = rho * np.ones((m, 1))
    # logger

    show_log_header()

    # - k: outer iteration num
    # - it: inner iteration num
    # - idx: 1-n block idx
    #       it may not be the block no
    # A_k x_k
    _vAx = {idx: _A @ xk[idx] for idx, _A in enumerate(A)}
    # c_k x_k
    _vcx = {idx: (_c @ xk[idx]).trace() for idx, _c in enumerate(c)}
    # x_k - x_k* (fixed point error)
    _eps_fix_point = {idx: 0 for idx, _ in enumerate(A)}
    for k in range(bcdpar.itermax):
        for it in range(bcdpar.linmax):
            # idx: A[idx]@x[idx]
            for idx in range(nblock):
                # update gradient
                Ak = A[idx]
                _Ax = sum(_vAx.values())
                _c = c[idx].T \
                     + rho * Ak.T @ _nonnegative(_Ax - b + lbd / rho) \
                     + (0.5 - xk[idx]) / tau
                # save to price
                _x = route.solve_primal_by_mip(np.array(_c).flatten())
                # _x = np.ones((n, 1))
                # accept or not
                _v_sp = (_c.T @ _x).trace()
                if _v_sp > 0:
                    # do not select path
                    _x = np.zeros(_c.shape)

                _eps_fix_point[idx] = np.linalg.norm(xk[idx] - _x)

                # update this block
                xk[idx] = _x
                _vAx[idx] = Ak @ _x
                _vcx[idx] = _cx = (c[idx] @ _x).trace()

            # fixed-point eps
            if sum(_eps_fix_point.values()) < 1e-4:
                break
        _iter_time = time.time() - start
        _Ax = sum(_vAx.values())
        _vpfeas = _nonnegative(_Ax - b)
        eps_pfeas = np.linalg.norm(_vpfeas)
        cx = sum(_vcx.values())

        lobj = cx + (_nonnegative(_Ax - b + lbd / rho) ** 2).sum() * rho / 2 - np.linalg.norm(lbd) ** 2 / 2 / rho
        eps_fp = sum(_eps_fix_point.values())
        _log_line = "{:03d} {:.1e} {:+.2e} {:+.2e} {:+.3e} {:+.3e} {:+.3e} {:.2e} {:04d}".format(
            k, _iter_time, cx, lobj, eps_pfeas, eps_fp, rho, tau, it + 1
        )
        print(_log_line)
        if eps_pfeas == 0 and eps_fp < 1e-4:
            break

        rho *= sigma
        lbd = _nonnegative((_Ax - b) * rho + lbd)

        bcdpar.iter += 1

    return xk
