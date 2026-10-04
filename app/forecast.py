"""岸桥停机时的剩余装船作业预测求解器。

给定**截至分钟** ``cutoff`` 时刻的装船实绩状态、冻结基线岸桥数量与各岸桥的
停机半开区间，对全部未完箱（进行中箱 + 待开工箱）安排预计作业：

* **已完工箱**：实际完工分钟均不晚于截至分钟，未完箱开工均不早于截至分钟，
  故“已完工下层先于未完上层”恒成立，无需进入决策；
* **进行中箱**：保持实际岸桥，自截至分钟连续作业，预计区间
  ``[cutoff, cutoff + remaining)``（开始/岸桥固定，不可改、不可中断）；
* **待开工箱**：在自身时间窗内（且不早于截至分钟）由任一基线岸桥作业。

硬性约束：

1. 每个待开工箱恰好由一台岸桥作业一次，进行中箱固定在实际岸桥；
2. 同一岸桥的全部预计作业区间 ``[开工, 完工)`` 互不重叠（允许首尾相接），
   且不与该桥任一停机区间 ``[停机起, 停机止)`` 重叠；
3. 同堆栈下层箱完工后上层箱才可开工（未完箱之间按预计完工传递；
   进行中箱自截至分钟连续）。

优化目标：

1. **全船预计最晚完工时间（makespan）最早**；
2. makespan 相同时，按全部未完箱箱号升序使 ``(开工分钟, 岸桥编号)`` 序列
   字典序最小（进行中箱的开工/岸桥为固定常量，仅占位；待开工箱参与优化；
   编码方式同 :mod:`app.scheduler`，分块基数编码与完整字典序最小严格等价）。

无可行安排时抛出 :class:`InfeasibleForecast`（中文原因）。求解结果在服务内
再以 Python 对全部硬约束逐步复核，不盲信求解器数值容差。

本模块为**纯计算**：不读写数据库，不改变原排程、实绩与冻结基线。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import pulp

# 单次 CBC 求解时间上限（秒）
TIME_LIMIT = 30


class InfeasibleForecast(Exception):
    """不存在满足约束的剩余作业安排。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PendingJob:
    """待开工箱：时长与自身时间窗（有效开工不早于截至分钟）。"""

    no: str
    stack: str
    tier: int
    duration: int  # 正整数分钟
    release: int  # 最早开工分钟
    deadline: int  # 最迟完工分钟


@dataclass(frozen=True)
class ActiveJob:
    """进行中箱：固定实际岸桥，预计 [cutoff, cutoff+remaining) 连续作业。"""

    no: str
    stack: str
    tier: int
    crane_no: int  # 实际岸桥（1 起，固定）
    remaining: int  # 正整数预计剩余分钟


@dataclass(frozen=True)
class Downtime:
    """岸桥停机半开区间 [start_minute, end_minute)。"""

    crane_no: int
    start_minute: int
    end_minute: int


@dataclass
class PredictedEntry:
    container_no: str
    stack: str
    tier: int
    kind: str  # in_progress | pending
    start_minute: int
    finish_minute: int
    crane_no: int


@dataclass
class ForecastResult:
    entries: list[PredictedEntry] = field(default_factory=list)
    latest_finish_minute: int = 0


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或首尾相接的闭开区间，按起点升序返回。"""
    merged: list[tuple[int, int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return merged


def _crane_gaps(
    crane: int,
    cutoff: int,
    downtimes: Sequence[Downtime],
    active: Sequence[ActiveJob],
) -> list[tuple[int, int | None]]:
    """某岸桥自截至分钟起的空闲缝隙：在制作业与停机区间之外，末尾为开放缝隙。

    返回若干 ``(起点, 终点)``，终点为 ``None`` 表示向右无限延伸。
    完全位于截至分钟之前的停机区间与预计作业无关，直接裁掉。
    """
    blocked: list[tuple[int, int]] = []
    for d in downtimes:
        if d.crane_no != crane:
            continue
        if d.end_minute > cutoff:
            blocked.append((max(d.start_minute, cutoff), d.end_minute))
    for a in active:
        if a.crane_no == crane:
            blocked.append((cutoff, cutoff + a.remaining))

    gaps: list[tuple[int, int | None]] = []
    cur = cutoff
    for s, e in _merge_intervals(blocked):
        if s > cur:
            gaps.append((cur, s))
        if e > cur:
            cur = e
    gaps.append((cur, None))
    return gaps


def _stack_members(
    pending: Sequence[PendingJob], active: Sequence[ActiveJob]
) -> dict[str, list[tuple[int, str, str]]]:
    """堆栈 -> 自底向上的 ``(tier, 箱号, 类别 active/pending)`` 列表。"""
    by_stack: dict[str, list[tuple[int, str, str]]] = {}
    for j in pending:
        by_stack.setdefault(j.stack, []).append((j.tier, j.no, "pending"))
    for j in active:
        by_stack.setdefault(j.stack, []).append((j.tier, j.no, "active"))
    for members in by_stack.values():
        members.sort()
    return by_stack


def _prechecks(
    pending: list[PendingJob],
    active: list[ActiveJob],
    downtimes: Sequence[Downtime],
    crane_count: int,
    cutoff: int,
) -> None:
    """MILP 前的快速可行性预检，命中即给出明确中文原因。"""
    # 同一桥在 cutoff 时刻至多一个进行中箱（实绩重放已保证，防御性复核）
    seen_cranes: set[int] = set()
    for a in active:
        if a.crane_no in seen_cranes:
            raise InfeasibleForecast(
                f"箱 {a.no} 与同桥另一进行中箱在截至第 {cutoff} 分钟仍同时占用岸桥 "
                f"{a.crane_no}，实绩状态不一致"
            )
        seen_cranes.add(a.crane_no)
        # 进行中箱固定预计区间不得与本桥停机重叠
        f = cutoff + a.remaining
        for d in downtimes:
            if d.crane_no != a.crane_no:
                continue
            if d.start_minute < f and cutoff < d.end_minute:
                raise InfeasibleForecast(
                    f"进行中箱 {a.no} 须保持岸桥 {a.crane_no} 自第 {cutoff} 分钟连续"
                    f"作业至第 {f} 分钟，但该桥停机半开区间 "
                    f"[{d.start_minute}, {d.end_minute}) 与其重叠，"
                    "在制作业不可改桥或中断"
                )

    # 时间窗（有效开工不早于 cutoff）预检
    for j in pending:
        eff_release = max(j.release, cutoff)
        if eff_release + j.duration > j.deadline:
            raise InfeasibleForecast(
                f"待开工箱 {j.no} 时长 {j.duration} 分钟，自截至分钟起最早第 "
                f"{eff_release} 分钟开工，第 {eff_release + j.duration} 分钟才完工，"
                f"晚于最迟完工分钟 {j.deadline}，时间窗内无法完工"
            )

    # 堆栈链上自底向上传播未完箱最早完工
    by_stack = _stack_members(pending, active)
    for stack, members in by_stack.items():
        prev_finish: int | None = None
        for _tier, no, kind in members:
            if kind == "active":
                if prev_finish is not None:
                    raise InfeasibleForecast(
                        f"箱 {no} 截至第 {cutoff} 分钟已在作业，但其同堆栈存在未完"
                        f"下层箱（预计最早第 {prev_finish} 分钟完工），"
                        "实绩状态与下层先完工约束矛盾"
                    )
                prev_finish = next(
                    cutoff + a.remaining for a in active if a.no == no
                )
            else:
                j = next(x for x in pending if x.no == no)
                est = max(j.release, cutoff, prev_finish or 0)
                if est + j.duration > j.deadline:
                    cause = (
                        f"受同堆栈未完下层箱限制最早第 {est} 分钟开工"
                        if prev_finish is not None
                        else f"最早第 {est} 分钟开工"
                    )
                    raise InfeasibleForecast(
                        f"待开工箱 {no}{cause}，第 {est + j.duration} 分钟才完工，"
                        f"晚于最迟完工分钟 {j.deadline}，无可行安排"
                    )
                prev_finish = est + j.duration


def _pending_closure(
    pending: list[PendingJob], active: list[ActiveJob]
) -> set[tuple[str, str]]:
    """待开工箱之间的堆栈先后**传递闭包**边 ``(下层箱号, 上层箱号)``。

    进行中箱无决策变量，其对同堆栈待开工箱的固定约束在模型内单独加入。
    """
    direct: list[tuple[int, int]] = []
    by_stack = _stack_members(pending, active)
    idx = {j.no: i for i, j in enumerate(pending)}
    for members in by_stack.values():
        prev_pending: int | None = None
        for _tier, no, kind in members:
            if kind != "pending":
                # active 出现在 pending 之后（上方）；同堆栈 active 之下不可能再有
                # 未完待开工箱（实绩重放保证），其后的 pending 由 active 固定约束处理
                continue
            cur = idx[no]
            if prev_pending is not None:
                direct.append((prev_pending, cur))
            prev_pending = cur

    n = len(pending)
    reach = [[False] * n for _ in range(n)]
    for a, b in direct:
        reach[a][b] = True
    for mid in range(n):
        for a in range(n):
            if not reach[a][mid]:
                continue
            for b in range(n):
                if reach[mid][b]:
                    reach[a][b] = True
    return {(pending[a].no, pending[b].no)
            for a in range(n) for b in range(n) if reach[a][b]}


def _build_model(
    pending: list[PendingJob],
    active: list[ActiveJob],
    options: dict[str, list[tuple[int, int, int | None]]],
    closure: set[tuple[str, str]],
    crane_count: int,
    cutoff: int,
    horizon: int,
    makespan_bound: int | None = None,
    fixed: dict[str, tuple[int, int]] | None = None,
    lex_block: list[str] | None = None,
) -> tuple[pulp.LpProblem, dict, dict, pulp.LpVariable]:
    """构建剩余作业预测 MILP（仅待开工箱为决策变量）。

    * options: 待开工箱号 -> 可行缝隙列表 ``(0 基岸桥序号, 缝起点, 缝终点|None)``；
    * closure: 待开工箱号间的堆栈先后传递闭包 ``(下层, 上层)``；
    * fixed: 待开工箱号 -> (开工分钟, 0 基岸桥序号)（字典序阶段已固定箱）；
    * lex_block: 目标改为该箱号序列（含进行中箱占位）的 (开工,岸桥) 基数编码和；
      传入 None 时目标为最小化最晚完工 C。
    """
    fixed = fixed or {}
    K = crane_count
    H = horizon
    M = H + 1

    prob = pulp.LpProblem("remaining_forecast", pulp.LpMinimize)

    s = {
        j.no: pulp.LpVariable(f"s_{j.no}", lowBound=cutoff, upBound=j.deadline,
                              cat="Integer")
        for j in pending
    }
    z: dict[tuple[str, int], pulp.LpVariable] = {}
    for j in pending:
        for oi in range(len(options[j.no])):
            z[j.no, oi] = pulp.LpVariable(f"z_{j.no}_{oi}", cat="Binary")
    C = pulp.LpVariable("C", lowBound=0, upBound=H, cat="Integer")

    if lex_block is not None:
        base = K * (H + 1) + 1
        terms = []
        for pos, cno in enumerate(lex_block):
            if cno not in s:
                continue  # 进行中箱的 (开工,岸桥) 为固定常量，省略不影响 argmin
            terms.append(
                (base ** (len(lex_block) - 1 - pos))
                * (
                    K * s[cno]
                    + pulp.lpSum(
                        k * z[cno, oi]
                        for oi, (k, _a, _b) in enumerate(options[cno])
                    )
                )
            )
        prob += pulp.lpSum(terms) if terms else 0, "lex_objective"
    else:
        prob += C, "min_makespan"

    for j in pending:
        prob += (
            pulp.lpSum(z[j.no, oi] for oi in range(len(options[j.no]))) == 1,
            f"onegap_{j.no}",
        )
        prob += s[j.no] >= max(j.release, cutoff), f"release_{j.no}"
        prob += s[j.no] + j.duration <= j.deadline, f"deadline_{j.no}"
        prob += s[j.no] + j.duration <= C, f"cmax_{j.no}"
        for oi, (k, a, b) in enumerate(options[j.no]):
            prob += (
                s[j.no] >= a - M * (1 - z[j.no, oi]),
                f"gaplo_{j.no}_{oi}",
            )
            if b is not None:
                prob += (
                    s[j.no] + j.duration <= b + M * (1 - z[j.no, oi]),
                    f"gaph_{j.no}_{oi}",
                )

    # 字典序阶段已固定的待开工箱
    for cno, (st, k0) in fixed.items():
        prob += s[cno] == st, f"fix_s_{cno}"
        chosen_oi = next(
            (
                oi
                for oi, (k, a, b) in enumerate(options[cno])
                if k == k0 and st >= a and (b is None or st < b)
            ),
            None,
        )
        if chosen_oi is not None:
            prob += z[cno, chosen_oi] == 1, f"fix_z_{cno}"

    # 进行中箱的固定完工计入 makespan
    for a in active:
        prob += C >= cutoff + a.remaining, f"cactive_{a.no}"

    # 待开工箱之间的堆栈先后（传递闭包）
    for lower, upper in closure:
        jl = next(j for j in pending if j.no == lower)
        prob += s[lower] + jl.duration <= s[upper], f"stack_{lower}_{upper}"

    # 进行中箱 -> 同堆栈其上方全部待开工箱
    by_stack = _stack_members(pending, active)
    for members in by_stack.values():
        for pos, (_tier, no, kind) in enumerate(members):
            if kind != "active":
                continue
            f = cutoff + next(a.remaining for a in active if a.no == no)
            for _t2, upper, k2 in members[pos + 1:]:
                if k2 == "pending":
                    prob += s[upper] >= f, f"stack_active_{no}_{upper}"

    # 同岸桥不重叠：仅对无堆栈先后关系的待开工箱对引入析取变量；
    # 缝隙已保证各作业不撞停机/在制作业。
    pnos = [j.no for j in pending]
    job_by_no = {j.no: j for j in pending}
    crane_opts: dict[tuple[str, int], list[int]] = {}
    for no in pnos:
        for k in range(K):
            idxs = [oi for oi, (kk, _a, _b) in enumerate(options[no]) if kk == k]
            if idxs:
                crane_opts[no, k] = idxs
    for ii in range(len(pnos)):
        for jj in range(ii + 1, len(pnos)):
            ni, nj = pnos[ii], pnos[jj]
            if (ni, nj) in closure or (nj, ni) in closure:
                continue
            shared = [
                k for k in range(K) if (ni, k) in crane_opts and (nj, k) in crane_opts
            ]
            if not shared:
                continue
            order = pulp.LpVariable(f"b_{ni}_{nj}", cat="Binary")
            pi, pj = job_by_no[ni].duration, job_by_no[nj].duration
            for k in shared:
                zi = pulp.lpSum(z[ni, oi] for oi in crane_opts[ni, k])
                zj = pulp.lpSum(z[nj, oi] for oi in crane_opts[nj, k])
                apart = 2 - zi - zj  # 不都在 k 桥时 >=1，两条约束同时松弛
                prob += (
                    s[ni] + pi <= s[nj] + M * (1 - order) + M * apart,
                    f"nb_{ni}_{nj}_{k}_a",
                )
                prob += (
                    s[nj] + pj <= s[ni] + M * order + M * apart,
                    f"nb_{ni}_{nj}_{k}_b",
                )

    if makespan_bound is not None:
        prob += C <= makespan_bound, "C_bound"

    return prob, s, z, C


def _solve(prob: pulp.LpProblem) -> str:
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=TIME_LIMIT, gapRel=0)
    prob.solve(solver)
    return pulp.LpStatus[prob.status]


def _verify(
    pending: Sequence[PendingJob],
    active: Sequence[ActiveJob],
    downtimes: Sequence[Downtime],
    crane_count: int,
    cutoff: int,
    chosen: dict[str, tuple[int, int, int]],
) -> int:
    """对预计安排做全硬约束 Python 复核，返回最晚完工分钟。

    已完工箱实际完工 <= cutoff，而全部未完箱预计开工 >= cutoff，故已完工下层
    相对未完上层的先后恒成立，此处只需核对未完箱之间的堆栈先后。
    """
    pred: dict[str, dict] = {}
    for a in active:
        if not 1 <= a.crane_no <= crane_count:
            raise InfeasibleForecast(
                f"进行中箱 {a.no} 的实际岸桥编号 {a.crane_no} 超出冻结基线岸桥"
                f"数量 {crane_count}"
            )
        pred[a.no] = {
            "start": cutoff, "finish": cutoff + a.remaining,
            "crane": a.crane_no, "stack": a.stack, "tier": a.tier,
        }
    for j in pending:
        st, fi, cr = chosen[j.no]
        if fi != st + j.duration:
            raise InfeasibleForecast(f"箱 {j.no} 求解结果完工分钟与时长不符")
        if st < cutoff:
            raise InfeasibleForecast(
                f"箱 {j.no} 预计开工 {st} 早于截至分钟 {cutoff}"
            )
        if st < j.release or fi > j.deadline:
            raise InfeasibleForecast(f"箱 {j.no} 预计作业超出自身时间窗")
        if not 1 <= cr <= crane_count:
            raise InfeasibleForecast(f"箱 {j.no} 预计岸桥编号 {cr} 非法")
        pred[j.no] = {"start": st, "finish": fi, "crane": cr,
                      "stack": j.stack, "tier": j.tier}

    # 停机不重叠
    for no, p in pred.items():
        for d in downtimes:
            if d.crane_no != p["crane"]:
                continue
            if d.start_minute < p["finish"] and p["start"] < d.end_minute:
                raise InfeasibleForecast(
                    f"箱 {no} 预计作业 [{p['start']}, {p['finish']}) 与岸桥 "
                    f"{d.crane_no} 停机 [{d.start_minute}, {d.end_minute}) 重叠"
                )

    # 同桥作业两两不重叠（首尾相接允许）
    by_crane: dict[int, list[tuple[int, int, str]]] = {}
    for no, p in pred.items():
        by_crane.setdefault(p["crane"], []).append((p["start"], p["finish"], no))
    for crane, ivs in by_crane.items():
        ivs.sort()
        for (s1, f1, n1), (s2, _f2, n2) in zip(ivs, ivs[1:]):
            if s2 < f1:
                raise InfeasibleForecast(
                    f"岸桥 {crane} 上箱 {n1} 与箱 {n2} 的预计作业区间重叠"
                )

    # 堆栈先后：仅核对未完箱之间（已完工下层恒满足）
    stack_members: dict[str, list[tuple[int, str]]] = {}
    for no, p in pred.items():
        stack_members.setdefault(p["stack"], []).append((p["tier"], no))
    for members in stack_members.values():
        members.sort()
        for pos, (_t, upper) in enumerate(members):
            up = pred[upper]
            for _lt, lower in members[:pos]:
                if pred[lower]["finish"] > up["start"]:
                    raise InfeasibleForecast(
                        f"箱 {upper} 第 {up['start']} 分钟开工时同堆栈下层箱 "
                        f"{lower} 第 {pred[lower]['finish']} 分钟才完工，"
                        "违反下层先完工"
                    )

    return max((p["finish"] for p in pred.values()), default=cutoff)


def solve_forecast(
    *,
    pending: Sequence[PendingJob],
    active: Sequence[ActiveJob],
    downtimes: Sequence[Downtime],
    crane_count: int,
    cutoff: int,
) -> ForecastResult:
    """求解剩余作业预测；无可行安排时抛出 :class:`InfeasibleForecast`。"""
    if crane_count <= 0:
        raise InfeasibleForecast("岸桥数量必须为正整数")
    pending = list(pending)
    active = list(active)
    downtimes = list(downtimes)

    _prechecks(pending, active, downtimes, crane_count, cutoff)

    # 每桥自 cutoff 起的空闲缝隙（已避开在制作业与停机）
    gaps = {
        k: _crane_gaps(k, cutoff, downtimes, active)
        for k in range(1, crane_count + 1)
    }

    # 每个待开工箱枚举可行缝隙
    options: dict[str, list[tuple[int, int, int | None]]] = {}
    for j in pending:
        opts: list[tuple[int, int, int | None]] = []
        for k in range(1, crane_count + 1):
            for a, b in gaps[k]:
                earliest = max(a, j.release, cutoff)
                if b is None or earliest + j.duration <= b:
                    opts.append((k - 1, a, b))
        if not opts:
            raise InfeasibleForecast(
                f"待开工箱 {j.no}（时长 {j.duration} 分钟，时间窗 "
                f"[{max(j.release, cutoff)}, {j.deadline}]）在全部 {crane_count} "
                "台岸桥上均无法避开停机区间或在制作业连续完成，无可行安排"
            )
        options[j.no] = opts

    horizon = max(
        [cutoff]
        + [cutoff + a.remaining for a in active]
        + [j.deadline for j in pending]
    )
    closure = _pending_closure(pending, active)

    chosen: dict[str, tuple[int, int, int]] = {}
    c_star: int | None = None
    if pending:
        # 第一阶段：最小化最晚完工
        prob1, _s1, _z1, cvar = _build_model(
            pending, active, options, closure, crane_count, cutoff, horizon,
        )
        status = _solve(prob1)
        if status == "Infeasible":
            raise InfeasibleForecast(
                "不存在同时满足各待开工箱时间窗、同桥作业/停机不重叠与堆栈下层"
                "先完工约束的剩余作业安排"
            )
        if status != "Optimal":
            raise InfeasibleForecast(
                f"求解器在 {TIME_LIMIT}s 内未能判定可行安排（状态：{status}），"
                "请缩小规模或重试"
            )
        c_star = int(round(pulp.value(cvar)))

        # 第二阶段：固定 C = c_star，按全部未完箱箱号升序分块求
        # (开工,岸桥) 字典序最优（进行中箱为固定常量占位）。
        ordered_all = sorted([j.no for j in pending] + [a.no for a in active])
        base = crane_count * (horizon + 1) + 1
        digits = max(1, len(str(base - 1)))
        chunk = max(1, 13 // digits)

        fixed: dict[str, tuple[int, int]] = {}
        last_s, last_z = _s1, _z1
        for start in range(0, len(ordered_all), chunk):
            block = ordered_all[start:start + chunk]
            prob, s, z, _c = _build_model(
                pending, active, options, closure, crane_count, cutoff, horizon,
                makespan_bound=c_star, fixed=fixed, lex_block=block,
            )
            bstatus = _solve(prob)
            if bstatus != "Optimal":
                raise InfeasibleForecast("字典序优化失败，请重试")
            for cno in block:
                if cno not in s:
                    continue
                st = int(round(pulp.value(s[cno])))
                k0 = max(
                    range(crane_count),
                    key=lambda k: sum(
                        pulp.value(z[cno, oi]) or 0
                        for oi, (kk, _a, _b) in enumerate(options[cno]) if kk == k
                    ),
                )
                fixed[cno] = (st, k0)
            last_s, last_z = s, z

        for j in pending:
            st = int(round(pulp.value(last_s[j.no])))
            oi_star = max(
                range(len(options[j.no])),
                key=lambda oi: pulp.value(last_z[j.no, oi]) or 0,
            )
            k0, _a, _b = options[j.no][oi_star]
            chosen[j.no] = (st, st + j.duration, k0 + 1)

    # 全硬约束 Python 复核
    latest = _verify(pending, active, downtimes, crane_count, cutoff, chosen)
    if c_star is not None:
        latest = max(latest, c_star)

    entries: list[PredictedEntry] = []
    for a in active:
        entries.append(PredictedEntry(
            container_no=a.no, stack=a.stack, tier=a.tier, kind="in_progress",
            start_minute=cutoff, finish_minute=cutoff + a.remaining,
            crane_no=a.crane_no,
        ))
    for j in pending:
        st, fi, cr = chosen[j.no]
        entries.append(PredictedEntry(
            container_no=j.no, stack=j.stack, tier=j.tier, kind="pending",
            start_minute=st, finish_minute=fi, crane_no=cr,
        ))
    entries.sort(key=lambda e: e.container_no)
    return ForecastResult(entries=entries, latest_finish_minute=latest)
