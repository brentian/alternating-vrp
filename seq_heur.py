import numpy as np


def fix_var_to_0(vars):
    for var in vars:
        var.ub = 0


def restore_var_ub_to_1(vars):
    for var in vars:
        var.ub = 1


def restore_var_lb_to_0(vars):
    for var in vars:
        var.lb = 0


def fix_var_to_x(vars):
    for var in vars:
        var.ub = var.x
        var.lb = var.x


def relax_coup_depot_constrs(vrp):
    m = vrp.m
    for j, c in vrp.coup.items():
        c.rhs = len(vrp.J)
        c.sense = "<"
    for j, c in vrp.depot.items():
        c.rhs = 0
    m.update()
    assert all(c.rhs == 0 for c in vrp.depot.values())


def enforce_depot_constrs(vrp, j):
    c = vrp.depot[j]
    c.rhs = 1
    vrp.m.update()


def enforce_coup_constrs(vrp, city):
    assert city != vrp.p
    c = vrp.coup[city]
    c.rhs = 1
    c.sense = '='
    vrp.m.update()


def print_circle(x: dict, idx):
    circle = {}
    for i, j, k in x.keys():
        if k == idx and x[i, j, k].x > 0.5:
            circle[i] = j
    l = 0
    i = list(circle.keys())[0]
    print(f"circ of {idx}: ", end="")
    while l < len(circle):
        j = circle[i]
        if l == 0:
            print(f"{i}->{j}", end="")
        else:
            print(f"->{j}", end="")
        i = j
        l += 1
    print()


def seq_heur(vrp, d, xk, random_perm=False):
    assert all(var.lb == 0 for var in vrp.x.values())
    assert all(var.ub == 1 for var in vrp.x.values())

    vrp.m.setParam("LogToConsole", 0)

    old_c = [var.obj for var in vrp.m.getVars()]

    cost = {idx: 0 for idx in range(len(d))}
    for idx, (_d, _x) in enumerate(zip(d, xk)):
        vars = vrp.x.select('*', '*', idx)
        for var, coef, xi in zip(vars, _d.flatten(), _x):
            var.Obj = coef
            x = xi
            cost[idx] += x * coef

    if random_perm:
        cost = {k: v for k, v in enumerate(np.random.choice(range(len(d)), len(d), replace=False))}

    relax_coup_depot_constrs(vrp)
    for idx in vrp.J:
        fix_var_to_0(vrp.x.select('*', '*', idx))
        vrp.m.update()

    fix_idx = set()
    free_idx = set(cost.keys())
    for idx, co in sorted(cost.items(), key=lambda x: x[1]):
        vrp.m.update()
        fix_idx.add(idx)
        free_idx.remove(idx)

        # enforce all constraints in the last iteration
        if len(free_idx) == 0:
            for _city in vrp.coup.keys():
                enforce_coup_constrs(vrp, _city)

        x_idx = vrp.x.select('*', '*', idx)
        restore_var_ub_to_1(x_idx)

        enforce_depot_constrs(vrp, idx)

        assert all(vrp.depot[i].RHS == 1 for i in fix_idx)
        assert all(vrp.depot[i].RHS == 0 for i in free_idx)

        vrp.solve()

        if vrp.m.SolCount > 0:
            # print_circle(vrp.x, idx)
            fix_var_to_x(x_idx)

            tour = set()
            for i, j, _idx in vrp.x:
                if _idx != idx:
                    continue
                elif vrp.x[i, j, idx].x > 0.5:
                    tour.add(i)
                    tour.add(j)

            for city in tour:
                if city != vrp.p:
                    assert vrp.coup[city].index == city - 1
                    enforce_coup_constrs(vrp, city)
        else:
            restore_var_lb_to_0(vrp.x.values())
            restore_var_ub_to_1(vrp.x.values())
            vrp.m.update()
            for _city in vrp.coup.keys():
                enforce_coup_constrs(vrp, _city)
            for _idx in cost.keys():
                enforce_depot_constrs(vrp, _idx)
            break

    assert all(c.rhs == 1 for c in vrp.depot.values())
    assert all(c.sense == '=' for c in vrp.depot.values())
    assert all(c.rhs == 1 for c in vrp.coup.values())
    assert all(c.sense == '=' for c in vrp.coup.values())

    # recover old c
    for var, coef in zip(vrp.m.getVars(), old_c):
        var.Obj = coef
    # vrp.m.setParam("LogToConsole", 1)
    vrp.m.reset()  # FIXME: no warm start? set time limit instead of solution limit
    vrp.solve()
    vrp.m.setParam("LogToConsole", 0)
    vrp.m.write("seq_heur.sol")

    try:
        objval = vrp.m.objval
    except:
        objval = np.inf

    restore_var_lb_to_0(vrp.x.values())
    restore_var_ub_to_1(vrp.x.values())
    vrp.m.update()

    # vrp.m.setParam("LogToConsole", 1)

    return objval
