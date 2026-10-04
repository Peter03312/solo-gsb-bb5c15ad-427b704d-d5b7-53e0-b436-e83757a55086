"""逐港卸箱顺序求解器。

给定已确认配载方案的每箱箱重、所在槽纵向力臂、堆栈/层与卸港序号，以及
每个卸港的允许剩余总力矩闭区间，在以下硬性约束下安排全程卸箱顺序：

* 按卸港序号从小到大，每港只卸该港箱（位置 p 只能放属于该港的箱）；
* 同堆栈上层箱未卸时不得取下层箱（自底向上连续链，相邻层建立先后约束）；
* 每卸一箱后按该方案所用箱重与槽位力臂重算剩余总力矩
  ``初始总力矩 - Σ已卸(箱重 × 力臂)``，结果必须落入该箱所属卸港的
  允许剩余总力矩闭区间。

优化目标：在所有港口均可完成的顺序中，取全程箱号序列字典序最小者。
与配载/排程求解器一致，采用分块基数编码：每块把 CHUNK 个卸箱位置上的
箱号序号以基数 B=箱数+1 编码进单个整数目标，其最小化与该块（前缀固定
后）箱号序列字典序最小严格等价；系数控制在双精度可精确表示的整数范围内。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pulp

# 浮点比较/边界容差
EPS = 1e-7
# 单次 CBC 求解时间上限（秒）
TIME_LIMIT = 30


class InfeasibleUnloading(Exception):
    """不存在满足各港力矩区间与堆栈先后约束的逐港卸箱顺序。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PlacedContainer:
    """方案中已配载的箱：箱重与所在槽信息按该方案快照使用。"""

    no: str
    weight: float
    arm: float  # 所在槽纵向力臂
    stack: str
    tier: int
    discharge_order: int  # 卸港序号，小者先卸


@dataclass(frozen=True)
class PortWindow:
    discharge_order: int
    min_remaining_moment: float
    max_remaining_moment: float


@dataclass
class UnloadStep:
    step_no: int  # 自 1 开始的全程卸箱步序
    discharge_order: int  # 该步所属卸港序号
    container_no: str
    stack: str
    tier: int
    weight: float
    arm: float
    remaining_moment: float  # 卸该箱后的剩余总力矩


@dataclass
class UnloadResult:
    steps: list[UnloadStep]
    initial_moment: float
    final_moment: float


def _stack_edges(boxes: Sequence[PlacedContainer]) -> list[tuple[int, int]]:
    """同堆栈按已占用层自底向上，相邻箱之间建立 下层->上层 直接边。

    配载方案保证同堆栈不悬空，已占用层构成自底向上的连续链；
    先卸港在上、后卸港在下，故每港开卸时该港箱位于各栈栈顶段，
    链式约束传递后等价于“同堆栈上层未卸不得取下下层”。
    """
    by_stack: dict[str, list[int]] = {}
    for i, b in enumerate(boxes):
        by_stack.setdefault(b.stack, []).append(i)
    edges: list[tuple[int, int]] = []
    for idxs in by_stack.values():
        idxs.sort(key=lambda i: boxes[i].tier)
        for lower, upper in zip(idxs, idxs[1:]):
            edges.append((lower, upper))
    return edges


def _prechecks(
    boxes: Sequence[PlacedContainer],
    windows: Mapping[int, PortWindow],
) -> None:
    """MILP 前的快速可行性预检，给出明确中文原因。"""
    m0 = sum(b.weight * b.arm for b in boxes)
    # 全部卸完后剩余力矩恒为 0：最后一个卸港的区间必须包含 0
    last_order = max(windows)
    last = windows[last_order]
    if not (last.min_remaining_moment - EPS <= 0.0 <= last.max_remaining_moment + EPS):
        raise InfeasibleUnloading(
            f"全部箱卸完后剩余总力矩为 0，不在末港（卸港序号 {last_order}）"
            f"的允许区间 [{last.min_remaining_moment}, "
            f"{last.max_remaining_moment}] 内"
        )

    # 同堆栈逐箱的可靠必要条件：箱 b 卸下那一刻（卸后计），
    #   必已卸：b 自身、同栈 b 上方各箱、所有更早卸港箱（并集 D）；
    #   必仍在：同栈 b 下方各箱（无法先于 b 卸）；
    #   其余同港箱（他栈）可先可后（记为 O，忽略他栈链得到的是放松条件）。
    # 故剩余力矩必落在闭区间 [Rmin, Rmax] 内；与该港区间不相交则必无解。
    by_stack: dict[str, list[PlacedContainer]] = {}
    for b in boxes:
        by_stack.setdefault(b.stack, []).append(b)
    moment = {b.no: b.weight * b.arm for b in boxes}
    for stack_boxes in by_stack.values():
        stack_boxes.sort(key=lambda b: b.tier)  # 自底向上
        for idx, b in enumerate(stack_boxes):
            above = {x.no for x in stack_boxes[idx + 1:]}
            mandatory = above | {b.no} | {
                x.no for x in boxes if x.discharge_order < b.discharge_order
            }
            optional = [
                x for x in boxes
                if x.discharge_order == b.discharge_order
                and x.no not in mandatory
                and x.stack != b.stack
            ]
            removed_min = sum(moment[no] for no in mandatory)
            # 负力臂箱先卸会使剩余力矩变大；正力臂箱先卸使剩余力矩变小
            r_min = m0 - removed_min - sum(
                moment[x.no] for x in optional if moment[x.no] > 0
            )
            r_max = m0 - removed_min - sum(
                moment[x.no] for x in optional if moment[x.no] < 0
            )
            w = windows[b.discharge_order]
            if r_max < w.min_remaining_moment - EPS or r_min > w.max_remaining_moment + EPS:
                raise InfeasibleUnloading(
                    f"箱 {b.no}（卸港 {b.discharge_order}）受堆栈 {b.stack} "
                    "上层箱先后限制，其卸下时剩余总力矩只能处于 "
                    f"[{round(r_min, 6)}, {round(r_max, 6)}]，与该港允许区间 "
                    f"[{w.min_remaining_moment}, {w.max_remaining_moment}] 不相交"
                )


def _build_model(
    boxes: Sequence[PlacedContainer],
    windows: Mapping[int, PortWindow],
    port_of_position: Sequence[int],
    stack_edges: set[tuple[int, int]],
    initial_moment: float,
    fixed: Mapping[int, int] | None = None,
    lex_positions: Sequence[int] | None = None,
) -> tuple[pulp.LpProblem, dict]:
    """构建卸箱顺序 MILP。

    变量 x[p, i]：第 p 个卸箱位置（0 基）卸箱 i。

    * fixed: 位置 -> 箱下标（前面位置已固定的箱）；
    * lex_positions: 目标改为该位置序列（重要性从高到低）的箱号序号
      基数编码和；缺省目标为常数 0（仅判可行性）。
    """
    fixed = fixed or {}
    n = len(boxes)
    ordered_nos = sorted(b.no for b in boxes)
    rank_of_no = {no: j for j, no in enumerate(ordered_nos)}
    rank = [rank_of_no[b.no] for b in boxes]
    box_moment = [b.weight * b.arm for b in boxes]

    prob = pulp.LpProblem("unloading", pulp.LpMinimize)
    x = {
        (p, i): pulp.LpVariable(f"x_{p}_{i}", cat="Binary")
        for p in range(n)
        for i in range(n)
    }

    if lex_positions:
        base = n + 1
        k = len(lex_positions)
        prob += pulp.lpSum(
            (base ** (k - 1 - q))
            * pulp.lpSum(rank[i] * x[p, i] for i in range(n))
            for q, p in enumerate(lex_positions)
        ), "lex_objective"
    else:
        prob += pulp.lpSum([]), "feasibility"

    # 每个位置恰好一箱、每箱恰好占据一个位置（全程为一个排列）
    for p in range(n):
        prob += pulp.lpSum(x[p, i] for i in range(n)) == 1, f"pos_{p}"
    for i in range(n):
        prob += pulp.lpSum(x[p, i] for p in range(n)) == 1, f"box_{boxes[i].no}"

    # 港口顺序：位置 p 只能卸属于该港的箱（每港只卸该港箱）
    for p in range(n):
        allowed = [i for i, b in enumerate(boxes)
                   if b.discharge_order == port_of_position[p]]
        if len(allowed) == 1:
            prob += x[p, allowed[0]] == 1, f"port_{p}"
        else:
            prob += (
                pulp.lpSum(
                    x[p, i]
                    for i in range(n)
                    if boxes[i].discharge_order != port_of_position[p]
                ) == 0,
                f"port_{p}",
            )

    # 已固定位置（字典序分块后）
    for p, i in fixed.items():
        prob += x[p, i] == 1, f"fix_{p}"

    # 同堆栈上层箱必须先于其紧邻的下层箱卸（位置先后经相邻链传递，
    # 故直接相邻已足够，与装船排程求解器一致）。对每对 (upper, lower)
    # 与每对位置 (p < q) 禁止倒置分配 “lower 在 p 且 upper 在 q”：
    #   x[p,lower] + x[q,upper] <= 1。
    # 写法不含位置索引系数，避免大 M 数值歧义。
    for lower, upper in stack_edges:
        for p in range(n):
            for q in range(p + 1, n):
                prob += (
                    x[p, lower] + x[q, upper] <= 1,
                    f"stack_{boxes[lower].no}_{boxes[upper].no}_{p}_{q}",
                )

    # 逐步剩余力矩：R_p = M0 - Σ_{q<=p} Σ_i m_i x[q,i]，
    # 位置 p 属于港口 port_of_position[p]，须落入该港闭区间
    scale = max(
        1.0,
        abs(initial_moment),
        *(abs(w.min_remaining_moment) for w in windows.values()),
        *(abs(w.max_remaining_moment) for w in windows.values()),
    )
    for p in range(n):
        w = windows[port_of_position[p]]
        removed = pulp.lpSum(
            box_moment[i] * x[q, i]
            for q in range(p + 1)
            for i in range(n)
        )
        # min <= M0 - removed <= max 等价于
        # M0 - max <= removed <= M0 - min（直接以 removed 为左式，边界清晰）
        prob += (
            removed >= initial_moment - w.max_remaining_moment - EPS * scale,
            f"moment_min_{p}",
        )
        prob += (
            removed <= initial_moment - w.min_remaining_moment + EPS * scale,
            f"moment_max_{p}",
        )

    return prob, x


def _solve(prob: pulp.LpProblem) -> str:
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=TIME_LIMIT, gapRel=0)
    prob.solve(solver)
    return pulp.LpStatus[prob.status]


def solve_unloading(
    boxes: Sequence[PlacedContainer],
    windows: Sequence[PortWindow],
) -> UnloadResult:
    """求解逐港卸箱顺序；无可行顺序或求解无法确定时抛出
    :class:`InfeasibleUnloading`。"""
    window_by_order = {w.discharge_order: w for w in windows}
    if len(window_by_order) != len(windows):
        seen: set[int] = set()
        dup: list[int] = []
        for w in windows:
            if w.discharge_order in seen and w.discharge_order not in dup:
                dup.append(w.discharge_order)
            seen.add(w.discharge_order)
        raise InfeasibleUnloading(
            f"卸港序号重复提交：{', '.join(map(str, sorted(dup)))}"
        )
    for w in windows:
        if w.min_remaining_moment > w.max_remaining_moment + EPS:
            raise InfeasibleUnloading(
                f"卸港 {w.discharge_order} 的允许剩余力矩区间下限 "
                f"{w.min_remaining_moment} 高于上限 {w.max_remaining_moment}"
            )

    plan_orders = sorted({b.discharge_order for b in boxes})
    submitted_orders = sorted(window_by_order)
    if plan_orders != submitted_orders:
        missing = sorted(set(plan_orders) - set(submitted_orders))
        extra = sorted(set(submitted_orders) - set(plan_orders))
        problems = []
        if missing:
            problems.append(f"缺少卸港序号：{', '.join(map(str, missing))}")
        if extra:
            problems.append(f"多余卸港序号：{', '.join(map(str, extra))}")
        raise InfeasibleUnloading(
            "提交卸港序号须与方案箱清单中的卸港序号完全一致（"
            + "；".join(problems) + "）"
        )

    n = len(boxes)
    if n == 0:
        return UnloadResult(steps=[], initial_moment=0.0, final_moment=0.0)

    box_moment = [b.weight * b.arm for b in boxes]
    initial_moment = sum(box_moment)

    # 各卸箱位置 -> 卸港序号（按港序号升序，港内位置数=该港箱数）
    counts = {d: 0 for d in plan_orders}
    for b in boxes:
        counts[b.discharge_order] += 1
    port_of_position: list[int] = []
    for d in plan_orders:
        port_of_position.extend([d] * counts[d])

    # 堆栈先后关系的传递闭包（Floyd-Warshall，规模小）
    direct_edges = _stack_edges(boxes)
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

    # 预检基于用户原始窗口给出可读的无解原因
    _prechecks(boxes, window_by_order)

    # 物理可达的剩余力矩闭包：剩余力矩 = M0 - 已卸子集力矩和。
    # 部分子集和必落在 [Σmin(0,m_i), Σmax(0,m_i)]，故剩余力矩必落在
    # [M0-Σmax(0,m), M0-Σmin(0,m)]。把用户窗口夹到该闭包：
    # 不改变可行解，但把 1e9 量级的无用边界压回 CBC 数值容差的安全区。
    sum_pos = sum(m for m in box_moment if m > 0)
    sum_neg = sum(m for m in box_moment if m < 0)
    reach_min = initial_moment - sum_pos
    reach_max = initial_moment - sum_neg
    clipped: dict[int, PortWindow] = {}
    for d, w in window_by_order.items():
        clipped[d] = PortWindow(
            discharge_order=d,
            min_remaining_moment=max(w.min_remaining_moment, reach_min),
            max_remaining_moment=min(w.max_remaining_moment, reach_max),
        )
    window_by_order = clipped

    # 第一阶段：判可行性
    prob1, x1 = _build_model(
        boxes, window_by_order, port_of_position, set(direct_edges), initial_moment
    )
    status = _solve(prob1)
    if status == "Infeasible":
        raise InfeasibleUnloading(
            "不存在按卸港顺序、满足同堆栈上层先卸且每步剩余总力矩均落入"
            "各港允许区间的卸箱顺序"
        )
    if status != "Optimal":
        raise InfeasibleUnloading(
            f"求解器在 {TIME_LIMIT}s 内未能判定可行卸箱顺序（状态：{status}），"
            "请缩小规模或重试"
        )

    # 第二阶段：按卸箱位置（即全程箱号序列）逐块字典序最小化。
    # 每块把 CHUNK 个位置上的箱号序号以基数 B=箱数+1 编码进单个整数目标。
    digits = max(1, len(str(n)))
    chunk = max(1, 13 // digits)

    def _assigned(var_x: dict, p: int) -> int:
        for i in range(n):
            v = pulp.value(var_x[p, i])
            if v is not None and v > 0.5:
                return i
        raise InfeasibleUnloading("字典序优化未能确定卸箱箱号，请重试")

    fixed: dict[int, int] = {}
    last_x = x1
    for start in range(0, n, chunk):
        block = list(range(start, min(start + chunk, n)))
        prob, x = _build_model(
            boxes, window_by_order, port_of_position, set(direct_edges),
            initial_moment, fixed=fixed, lex_positions=block,
        )
        bstatus = _solve(prob)
        if bstatus != "Optimal":
            raise InfeasibleUnloading(
                f"字典序优化未能完成（求解状态：{bstatus}），请重试"
            )
        for p in block:
            fixed[p] = _assigned(x, p)
        last_x = x

    by_no = {b.no: b for b in boxes}
    original_windows = {w.discharge_order: w for w in windows}
    assigned = [boxes[_assigned(last_x, p)] for p in range(n)]

    # Python 侧对返回序列做全部硬约束的完整复核：
    # 不盲信求解器（含其数值容差），任何不确定都拒绝而非保存错误结果。
    if len({b.no for b in assigned}) != n:
        raise InfeasibleUnloading("求解结果不是全部箱恰卸一次的排列，请重试")
    # 堆栈先后：每个下层箱必在其上方各箱之后
    pos = {b.no: p for p, b in enumerate(assigned)}
    for lower, upper in closure:
        if pos[boxes[upper].no] >= pos[boxes[lower].no]:
            raise InfeasibleUnloading(
                f"求解结果违反堆栈先后：上层箱 {boxes[upper].no} 未先于下层箱 "
                f"{boxes[lower].no} 卸，请重试"
            )

    steps: list[UnloadStep] = []
    remaining = initial_moment
    for p, b in enumerate(assigned):
        if b.discharge_order != port_of_position[p]:
            raise InfeasibleUnloading(
                f"求解结果第 {p + 1} 步卸下了非本港箱 {b.no}，请重试"
            )
        remaining -= b.weight * b.arm
        w = original_windows[port_of_position[p]]
        if not (w.min_remaining_moment - EPS
                <= remaining <= w.max_remaining_moment + EPS):
            raise InfeasibleUnloading(
                f"求解结果第 {p + 1} 步卸下 {b.no} 后剩余总力矩 "
                f"{round(remaining, 6)} 不在卸港 {b.discharge_order} 的允许区间 "
                f"[{w.min_remaining_moment}, {w.max_remaining_moment}] 内，请重试"
            )
        steps.append(
            UnloadStep(
                step_no=p + 1,
                discharge_order=port_of_position[p],
                container_no=b.no,
                stack=b.stack,
                tier=b.tier,
                weight=b.weight,
                arm=b.arm,
                remaining_moment=round(remaining, 6),
            )
        )
    return UnloadResult(
        steps=steps,
        initial_moment=round(initial_moment, 6),
        final_moment=round(remaining, 6),
    )
