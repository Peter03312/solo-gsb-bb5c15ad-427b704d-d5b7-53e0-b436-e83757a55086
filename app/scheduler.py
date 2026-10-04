"""装船时间表求解器（岸桥调度）。

给定已确认配载方案中每箱的吊装时长与时间窗（分钟）、可用岸桥数，
在以下硬性约束下安排每箱的开工分钟与岸桥编号：

* 每箱恰好由一台岸桥作业一次；
* 同一岸桥作业区间 ``[开工, 完工)`` 互不重叠（允许首尾相接）；
* 同堆栈下层箱完工后上层箱才可开工（按已占用层自底向上传递）；
* 每箱满足自身时间窗：``最早开工 <= 开工`` 且 ``完工 <= 最迟完工``。

优化目标：

1. 最小化全部箱的最晚完工时间（makespan）``C``；
2. 在最优 ``C`` 下，按箱号升序使 ``(开工分钟, 岸桥编号)`` 序列字典序最小。
   单箱的“先开工、再选小编号岸桥”编码为 ``K*开工 + 岸桥序号``
   （岸桥序号差值至多 K-1 < K，开工严格占主导）；
   分块基数编码与完整字典序最小严格等价，系数控制在双精度精确整数范围内，
   做法与配载求解器一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pulp

# 单次 CBC 求解时间上限（秒）
TIME_LIMIT = 30


class InfeasibleSchedule(Exception):
    """不存在满足约束的装船时间表。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Job:
    no: str
    stack: str
    stack_tier: int  # 箱所在堆栈层号（自底向上）
    duration: int  # 吊装时长（正整数分钟）
    release: int  # 最早开工分钟
    deadline: int  # 最迟完工分钟


@dataclass
class JobSchedule:
    container_no: str
    start_minute: int
    finish_minute: int
    crane_no: int  # 岸桥编号，自 1 开始


@dataclass
class ScheduleResult:
    jobs: list[JobSchedule]
    makespan: int


def _stack_edges(jobs: Sequence[Job]) -> list[tuple[int, int]]:
    """同堆栈按已占用层自底向上，相邻箱之间建立 下层->上层 直接边。

    配载方案保证同堆栈不悬空，已占用层即构成自底向上的连续链；
    链式约束传递后等价于“任一下层箱完工后任一上层箱才可开工”。
    """
    by_stack: dict[str, list[int]] = {}
    for i, j in enumerate(jobs):
        by_stack.setdefault(j.stack, []).append(i)
    edges: list[tuple[int, int]] = []
    for idxs in by_stack.values():
        idxs.sort(key=lambda i: jobs[i].stack_tier)
        for lower, upper in zip(idxs, idxs[1:]):
            edges.append((lower, upper))
    return edges


def _prechecks(jobs: Sequence[Job]) -> None:
    """时间窗与堆栈先后的快速可行性预检（MILP 前给出明确中文原因）。"""
    for j in jobs:
        if j.release + j.duration > j.deadline:
            raise InfeasibleSchedule(
                f"箱 {j.no} 的吊装时长 {j.duration} 分钟无法落入其时间窗 "
                f"[{j.release}, {j.deadline}]（最早开工+时长 > 最迟完工）"
            )

    # 同一堆栈链上自底向上传播最早完工时刻（已占用层自底向上连续）
    by_stack: dict[str, list[int]] = {}
    for i, j in enumerate(jobs):
        by_stack.setdefault(j.stack, []).append(i)
    for idxs in by_stack.values():
        idxs.sort(key=lambda i: jobs[i].stack_tier)
        lower_finish = -1  # 直接下层最早完工；底层无约束
        for i in idxs:
            j = jobs[i]
            start = max(j.release, lower_finish)
            finish = start + j.duration
            if finish > j.deadline:
                raise InfeasibleSchedule(
                    f"箱 {j.no} 受堆栈下层箱先后作业限制，最早完工 {finish} "
                    f"晚于最迟完工 {j.deadline}，时间窗无解"
                )
            lower_finish = finish


def _build_model(
    jobs: Sequence[Job],
    crane_count: int,
    closure: set[tuple[int, int]],
    big_m: int,
    makespan_bound: int | None = None,
    fixed: Mapping[str, tuple[int, int]] | None = None,
    lex_jobs: Sequence[str] | None = None,
) -> tuple[pulp.LpProblem, dict, dict, pulp.LpVariable]:
    """构建岸桥调度 MILP。

    * makespan_bound: 约束 C <= makespan_bound（字典序阶段固定最优 makespan）；
    * fixed: 箱号 -> (开工分钟, 0 基岸桥序号)，已固定的箱；
    * lex_jobs: 目标改为该箱号序列（重要性从高到低）的 (开工,岸桥) 基数编码和。
    """
    fixed = fixed or {}
    n = len(jobs)
    K = crane_count

    prob = pulp.LpProblem("loading", pulp.LpMinimize)

    s = {j.no: pulp.LpVariable(f"s_{i}", lowBound=0, cat="Integer")
         for i, j in enumerate(jobs)}
    z = {(j.no, k): pulp.LpVariable(f"z_{i}_{k}", cat="Binary")
         for i, j in enumerate(jobs) for k in range(K)}
    C = pulp.LpVariable("C", lowBound=0, cat="Integer")

    if lex_jobs:
        # 数字 v_i = K*s_i + 岸桥序号 ∈ [0, K*(H+1)-1]，以 R=K*(H+1)+1 为基数
        H = big_m - 1  # big_m = max(deadline)+1
        base = K * (H + 1) + 1
        k_block = len(lex_jobs)
        prob += pulp.lpSum(
            (base ** (k_block - 1 - pos))
            * (K * s[cno]
               + pulp.lpSum(k * z[cno, k] for k in range(K)))
            for pos, cno in enumerate(lex_jobs)
        ), "lex_objective"
    else:
        prob += C, "min_makespan"

    # 每箱恰好一台岸桥；时间窗；完工计入 C
    for i, j in enumerate(jobs):
        prob += pulp.lpSum(z[j.no, k] for k in range(K)) == 1, f"onecrane_{j.no}"
        prob += s[j.no] >= j.release, f"release_{j.no}"
        prob += s[j.no] + j.duration <= j.deadline, f"deadline_{j.no}"
        prob += s[j.no] + j.duration <= C, f"cmax_{j.no}"

    # 已固定箱（字典序分块后）
    for cno, (st, k0) in fixed.items():
        prob += s[cno] == st, f"fix_s_{cno}"
        prob += z[cno, k0] == 1, f"fix_z_{cno}"

    # 堆栈下层 -> 上层（含传递闭包，收紧可行域并跳过同桥析取）
    for a, b in closure:
        prob += (
            s[jobs[a].no] + jobs[a].duration <= s[jobs[b].no],
            f"stack_{jobs[a].no}_{jobs[b].no}",
        )

    # 同岸桥不重叠：对无堆栈先后关系的箱对引入析取变量
    for i in range(n):
        for j in range(i + 1, n):
            if (i, j) in closure or (j, i) in closure:
                continue
            order = pulp.LpVariable(f"b_{i}_{j}", cat="Binary")
            ni, nj = jobs[i].no, jobs[j].no
            pi, pj = jobs[i].duration, jobs[j].duration
            for k in range(K):
                apart = 2 - z[ni, k] - z[nj, k]  # 不同在 k 桥时为 >=1，约束松弛
                # order=1：i 在 j 前（第一条生效）；order=0：j 在 i 前（第二条生效）
                prob += (
                    s[ni] + pi <= s[nj] + big_m * (1 - order) + big_m * apart,
                    f"nb_{i}_{j}_{k}_a",
                )
                prob += (
                    s[nj] + pj <= s[ni] + big_m * order + big_m * apart,
                    f"nb_{i}_{j}_{k}_b",
                )

    if makespan_bound is not None:
        prob += C <= makespan_bound, "C_bound"

    return prob, s, z, C


def _solve(prob: pulp.LpProblem) -> str:
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=TIME_LIMIT, gapRel=0)
    prob.solve(solver)
    return pulp.LpStatus[prob.status]


def solve_loading(jobs: Sequence[Job], crane_count: int) -> ScheduleResult:
    """求解装船时间表；无可行排程时抛出 :class:`InfeasibleSchedule`。"""
    if crane_count <= 0:
        raise InfeasibleSchedule("岸桥数量必须为正整数")
    if not jobs:
        return ScheduleResult(jobs=[], makespan=0)

    direct_edges = _stack_edges(jobs)
    _prechecks(jobs)

    # 堆栈先后关系的传递闭包（Floyd-Warshall，规模小）
    n = len(jobs)
    reach = [[False] * n for _ in range(n)]
    for a, b in direct_edges:
        reach[a][b] = True
    for mid in range(n):
        for a in range(n):
            if not reach[a][mid]:
                continue
            for b in range(n):
                if reach[mid][b]:
                    reach[a][b] = True
    closure = {(a, b) for a in range(n) for b in range(n) if reach[a][b]}

    big_m = max(j.deadline for j in jobs) + 1

    # 第一阶段：最小化最晚完工时间
    prob1, s1, z1, C1 = _build_model(
        jobs, crane_count, closure, big_m
    )
    status = _solve(prob1)
    if status == "Infeasible":
        raise InfeasibleSchedule(
            f"在 {crane_count} 台岸桥下，不存在同时满足各箱时间窗、同岸桥不重叠与"
            "堆栈下层先完工约束的装船排程"
        )
    if status != "Optimal":
        raise InfeasibleSchedule(
            f"求解器在 {TIME_LIMIT}s 内未能判定可行排程（状态：{status}），"
            "请缩小规模或重试"
        )
    c_star = int(round(pulp.value(C1)))

    # 第二阶段：固定 C = c_star，按箱号升序分块求 (开工,岸桥) 字典序最优。
    # 每块将 CHUNK 个箱的 K*开工+岸桥序号 以基数 R 编码进单个整数目标，
    # 其最小化与该块（前缀固定后）序列字典序最小严格等价。
    ordered = [j.no for j in sorted(jobs, key=lambda x: x.no)]
    H = big_m - 1
    base = crane_count * (H + 1) + 1
    digits = max(1, len(str(base - 1)))
    chunk = max(1, 13 // digits)

    fixed: dict[str, tuple[int, int]] = {}
    last_s, last_z = s1, z1
    for start in range(0, n, chunk):
        block = ordered[start:start + chunk]
        prob, s, z, _ = _build_model(
            jobs, crane_count, closure, big_m,
            makespan_bound=c_star, fixed=fixed, lex_jobs=block,
        )
        bstatus = _solve(prob)
        if bstatus != "Optimal":
            raise InfeasibleSchedule("字典序优化失败，请重试")
        for cno in block:
            st = int(round(pulp.value(s[cno])))
            k0 = max(range(crane_count), key=lambda k: pulp.value(z[cno, k]) or 0)
            fixed[cno] = (st, k0)
        last_s, last_z = s, z

    by_no = {j.no: j for j in jobs}
    schedules: list[JobSchedule] = []
    for cno in ordered:
        st = int(round(pulp.value(last_s[cno])))
        k0 = max(range(crane_count),
                 key=lambda k: pulp.value(last_z[cno, k]) or 0)
        schedules.append(
            JobSchedule(
                container_no=cno,
                start_minute=st,
                finish_minute=st + by_no[cno].duration,
                crane_no=k0 + 1,
            )
        )
    return ScheduleResult(jobs=schedules, makespan=c_star)
