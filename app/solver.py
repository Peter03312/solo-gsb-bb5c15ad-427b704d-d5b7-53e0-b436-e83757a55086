"""配载求解器。

以 0-1 整数规划建模，变量 ``x[c, s]`` 表示箱 ``c`` 是否装入槽 ``s``：

* 每箱恰好占一个槽、每槽至多一箱；
* 槽位承重约束；
* 同堆栈相邻层：下层无箱则上层不得有箱，且下层箱不轻于上层箱；
* 早卸港箱不得位于晚卸港箱上方（先卸港在上、后卸港在下）；
* 不相容危险类别箱不得占据相邻槽位；
* 全船总力矩须落在允许区间内。

完整配载优化目标：

1. 最小化最大占用层数 ``T``；
2. 在最优 ``T`` 下，按箱号升序使槽位编号序列字典序最小
   （分块基数编码：每块的槽位序号以 B=槽位数+1 为基数编入单个整数目标，
   顺序固定各块；数学上与完整字典序最小严格等价，同时把系数控制在
   双精度可精确表示的范围内）。

应急改配（``solve_restow``）在上述硬约束之上追加：封锁槽位禁止占用，
并按三阶段优选：①最小化相对原方案换槽箱数；②最小化最高占用层；
③按箱号升序取槽位编号序列字典序最小。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pulp

# 浮点比较/边界容差
EPS = 1e-7
# 单次 CBC 求解时间上限（秒）
TIME_LIMIT = 30


class InfeasibleStowage(Exception):
    """不存在完整可行配载方案。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Slot:
    code: str
    stack: str
    tier: int
    capacity: float
    arm: float  # 纵向力臂


@dataclass(frozen=True)
class Container:
    no: str
    weight: float
    hazmat: str | None
    discharge_order: int  # 数值小者先卸


@dataclass
class Assignment:
    container_no: str
    slot_code: str
    stack: str
    tier: int
    weight: float
    arm: float


@dataclass
class SolveResult:
    assignments: list[Assignment]
    total_moment: float
    max_occupied_tier: int


@dataclass
class RestowResult:
    assignments: list[Assignment]
    total_moment: float
    max_occupied_tier: int
    moved_boxes: int


def _prechecks(containers: Sequence[Container], slots: Sequence[Slot]) -> None:
    if len(containers) > len(slots):
        raise InfeasibleStowage(
            f"箱数（{len(containers)}）超过可用槽位数（{len(slots)}），无法完整配载"
        )
    capacities = {s.code: s.capacity for s in slots}
    for c in containers:
        if c.weight > max(capacities.values(), default=0):
            raise InfeasibleStowage(
                f"箱 {c.no} 重量 {c.weight} 超过全船任一槽位承重，无法配载"
            )


def _build_model(
    containers: Sequence[Container],
    slots: Sequence[Slot],
    adjacency: set[tuple[str, str]],
    incompat: set[tuple[str, str]],
    min_moment: float,
    max_moment: float,
    fixed: Mapping[str, str] | None = None,
    t_bound: int | None = None,
    lex_boxes: Sequence[str] | None = None,
    blocked: frozenset[str] | set[str] | None = None,
    original: Mapping[str, str] | None = None,
    minimize_moves: bool = False,
    moves_bound: int | None = None,
) -> tuple[pulp.LpProblem, dict, pulp.LpVariable, dict[str, Slot],
           pulp.LpAffineExpression, pulp.LpAffineExpression]:
    """构建 MILP。

    * fixed: 箱号 -> 槽位编号（已固定分配）；
    * t_bound: 约束 T <= t_bound；
    * lex_boxes: 目标改为该箱号序列（按重要性从高到低）的槽位序号字典序加权和；
    * blocked: 临时封锁、禁止占用的槽位编号集合；
    * original / minimize_moves: 以原方案箱->槽映射计数换槽箱数 M，
      目标改为最小化 M；
    * moves_bound: 约束 M <= moves_bound（在前一阶段最优换槽数下优化层级）。
    """
    fixed = fixed or {}
    blocked = frozenset(blocked or ())
    slot_by_code = {s.code: s for s in slots}
    ordered_codes = sorted(slot_by_code)
    rank_of_slot = {code: j for j, code in enumerate(ordered_codes)}

    prob = pulp.LpProblem("stowage", pulp.LpMinimize)
    x: dict[tuple[str, str], pulp.LpVariable] = {
        (c.no, s.code): pulp.LpVariable(f"x_{i}_{j}", cat="Binary")
        for i, c in enumerate(containers)
        for j, s in enumerate(slots)
    }
    y = {s.code: pulp.lpSum(x[c.no, s.code] for c in containers) for s in slots}

    max_tier = max((s.tier for s in slots), default=0)
    T = pulp.LpVariable("T", lowBound=0, upBound=max_tier, cat="Integer")
    # 换槽计数：只要提供原方案映射就构建 m 变量（目标/约束按需使用）。
    # m 为“箱 c 是否离开原槽”的精确 0-1 指示：留槽 x=1 时 m 必为 0，
    # 离槽 x=0 时 m 必为 1（加齐上/下界，避免在不以 m 为目标的阶段中
    # 求解器把 m 自由置 1 而让 moves_bound 误收紧可行域）。
    m_vars: dict[str, pulp.LpVariable] = {}
    if original:
        for c in containers:
            orig = original.get(c.no)
            if orig is not None and orig in slot_by_code:
                mc = pulp.LpVariable(f"m_{c.no}", cat="Binary")
                prob += mc >= 1 - x[c.no, orig], f"mlo_{c.no}"
                prob += mc <= 1 - x[c.no, orig], f"mhi_{c.no}"
                m_vars[c.no] = mc
    moves_expr: pulp.LpAffineExpression = pulp.lpSum(m_vars.values())
    if lex_boxes:
        # 基数编码字典序；调用方保证系数在双精度精确整数范围内
        base = len(slots) + 1
        k = len(lex_boxes)
        prob += pulp.lpSum(
            (base ** (k - 1 - i))
            * pulp.lpSum(rank_of_slot[s.code] * x[cno, s.code] for s in slots)
            for i, cno in enumerate(lex_boxes)
        ), "lex_objective"
    elif minimize_moves:
        prob += moves_expr, "min_moves"
    else:
        prob += T, "min_max_tier"

    # 每箱恰好一个槽；每槽至多一箱
    for c in containers:
        prob += pulp.lpSum(x[c.no, s.code] for s in slots) == 1, f"once_{c.no}"
    for s in slots:
        prob += y[s.code] <= 1, f"onebox_{s.code}"

    # 固定分配
    for cno, scode in fixed.items():
        prob += x[cno, scode] == 1, f"fix_{cno}"

    # 临时封锁槽位禁止占用
    for code in sorted(blocked):
        if code in slot_by_code:
            prob += y[code] == 0, f"blocked_{code}"

    # 换槽箱数上限（前一阶段最优解下优化层级/字典序）
    if moves_bound is not None:
        prob += moves_expr <= moves_bound, "moves_bound"

    # 承重
    for s in slots:
        prob += (
            pulp.lpSum(c.weight * x[c.no, s.code] for c in containers) <= s.capacity,
            f"cap_{s.code}",
        )

    # 同堆栈：按层排序，相邻（按已录入槽位）上下层对
    by_stack: dict[str, list[Slot]] = {}
    for s in slots:
        by_stack.setdefault(s.stack, []).append(s)
    vertical_pairs: list[tuple[Slot, Slot]] = []  # (upper, lower)
    for stack_slots in by_stack.values():
        stack_slots.sort(key=lambda s: s.tier)
        for lower, upper in zip(stack_slots, stack_slots[1:]):
            vertical_pairs.append((upper, lower))

    for upper, lower in vertical_pairs:
        # 上层有箱则下层必有箱
        prob += y[upper.code] - y[lower.code] <= 0, f"support_{upper.code}_{lower.code}"
        for cu in containers:  # 固定为上层箱时，下层箱必须满足条件
            # 下层箱轻于上层箱 -> 禁止（求和式，避免箱对笛卡尔积）
            lighter = [
                cl for cl in containers
                if cl.no != cu.no and cl.weight < cu.weight - EPS
            ]
            if lighter:
                prob += (
                    x[cu.no, upper.code]
                    + pulp.lpSum(x[cl.no, lower.code] for cl in lighter)
                    <= 1,
                    f"w_{cu.no}_{upper.code}_{lower.code}",
                )
            # 上层箱卸港晚于下层箱 -> 禁止（先卸港序号小，须在上）
            earlier = [
                cl for cl in containers
                if cl.no != cu.no and cl.discharge_order < cu.discharge_order
            ]
            if earlier:
                prob += (
                    x[cu.no, upper.code]
                    + pulp.lpSum(x[cl.no, lower.code] for cl in earlier)
                    <= 1,
                    f"d_{cu.no}_{upper.code}_{lower.code}",
                )

    # 危险类别不相容 -> 不得相邻（相邻关系无向；按类聚合，避免箱对笛卡尔积）
    if incompat:
        for a_code, b_code in adjacency:
            for ca in containers:
                if not ca.hazmat:
                    continue
                bad_nos = [
                    cb.no for cb in containers
                    if cb.no != ca.no
                    and cb.hazmat
                    and tuple(sorted((ca.hazmat, cb.hazmat))) in incompat
                ]
                if not bad_nos:
                    continue
                prob += (
                    x[ca.no, a_code]
                    + pulp.lpSum(x[cno, b_code] for cno in bad_nos)
                    <= 1,
                    f"hz_{ca.no}_{a_code}_{b_code}",
                )
                prob += (
                    x[ca.no, b_code]
                    + pulp.lpSum(x[cno, a_code] for cno in bad_nos)
                    <= 1,
                    f"hz_{ca.no}_{b_code}_{a_code}",
                )

    # 总力矩
    moment_expr = pulp.lpSum(
        c.weight * slot_by_code[scode].arm * x[c.no, scode]
        for c in containers
        for scode in slot_by_code
    )
    scale = max(1.0, abs(min_moment), abs(max_moment))
    prob += moment_expr >= min_moment - EPS * scale, "moment_min"
    prob += moment_expr <= max_moment + EPS * scale, "moment_max"

    # 最大占用层数
    for s in slots:
        prob += T >= s.tier * y[s.code], f"T_{s.code}"
    if t_bound is not None:
        prob += T <= t_bound, "T_bound"

    return prob, x, T, slot_by_code, moment_expr, moves_expr


def _solve(prob: pulp.LpProblem) -> str:
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=TIME_LIMIT, gapRel=0)
    prob.solve(solver)
    return pulp.LpStatus[prob.status]


def _extract(
    containers: Sequence[Container],
    slot_by_code: Mapping[str, Slot],
    x: dict,
    moment_value: float,
    t_value: int,
) -> SolveResult:
    assignments: list[Assignment] = []
    for c in containers:
        for scode, s in slot_by_code.items():
            if pulp.value(x[c.no, scode]) is not None and pulp.value(x[c.no, scode]) > 0.5:
                assignments.append(
                    Assignment(c.no, scode, s.stack, s.tier, c.weight, s.arm)
                )
                break
    return SolveResult(
        assignments=sorted(assignments, key=lambda a: a.container_no),
        total_moment=round(moment_value, 6),
        max_occupied_tier=int(round(t_value)),
    )


def solve_stowage(
    containers: Sequence[Container],
    slots: Sequence[Slot],
    adjacency: Sequence[tuple[str, str]] | set[tuple[str, str]],
    incompatible_classes: Sequence[tuple[str, str]],
    min_moment: float,
    max_moment: float,
) -> SolveResult:
    """求解完整可行配载方案；无方案时抛出 :class:`InfeasibleStowage`。"""
    _prechecks(containers, slots)

    # 相邻关系归一化为无向对（以槽位编号排序）
    adj_set = {tuple(sorted(p)) for p in adjacency}
    incompat = {tuple(sorted(p)) for p in incompatible_classes}

    if not containers:
        if min_moment <= 0 <= max_moment:
            return SolveResult([], 0.0, 0)
        raise InfeasibleStowage(
            f"无箱时总力矩为 0，不在允许区间 [{min_moment}, {max_moment}] 内"
        )

    # 第一阶段：最小化最大占用层数
    prob1, x1, T1, slot_by_code, _, _ = _build_model(
        containers, slots, adj_set, incompat, min_moment, max_moment
    )
    status = _solve(prob1)
    if status == "Infeasible":
        raise InfeasibleStowage(
            "不存在满足承重、承重次序、卸港顺序、危险类别相邻限制且总力矩处于允许区间的完整配载方案"
        )
    if status != "Optimal":
        raise InfeasibleStowage(
            f"求解器在 {TIME_LIMIT}s 内未能判定可行方案（状态：{status}），请缩小规模或重试"
        )
    t_star = int(round(pulp.value(T1)))

    # 第二阶段：固定 T = t_star，按箱号升序分块求字典序最优。
    # 每块把 CHUNK 个箱的槽位序号以基数 B=槽位数+1 编码进单个整数目标，
    # 其最小化与该块（前缀固定后）槽位序列字典序最小严格等价；
    # 分块是为了把目标系数控制在双精度可精确表示的整数范围内。
    ordered_cnos = [c.no for c in sorted(containers, key=lambda c: c.no)]
    n_slots = len(slots)
    # (n_slots+1)^chunk 控制在约 1e13：双精度下系数为精确整数，CBC 不会因舍入出错
    digits = max(1, len(str(n_slots)))
    chunk = max(1, 13 // digits)
    fixed: dict[str, str] = {}
    last_x = x1
    last_sbc = slot_by_code

    for start in range(0, len(ordered_cnos), chunk):
        block = ordered_cnos[start:start + chunk]
        prob, x, _, sbc, _, _ = _build_model(
            containers, slots, adj_set, incompat, min_moment, max_moment,
            fixed=fixed, t_bound=t_star, lex_boxes=block,
        )
        status = _solve(prob)
        if status != "Optimal":
            raise InfeasibleStowage("字典序优化失败，请重试")
        for cno in block:
            for scode in sorted(sbc):
                if pulp.value(x[cno, scode]) is not None and pulp.value(x[cno, scode]) > 0.5:
                    fixed[cno] = scode
                    break
        last_x, last_sbc = x, sbc

    m2 = pulp.lpSum(
        c.weight * last_sbc[scode].arm * last_x[c.no, scode]
        for c in containers
        for scode in last_sbc
    )
    return _extract(containers, last_sbc, last_x, pulp.value(m2), t_star)


# ---------- 应急改配 ----------


def verify_stowage(
    assignments: Mapping[str, str],
    containers: Sequence[Container],
    slots: Sequence[Slot],
    adjacency: set[tuple[str, str]],
    incompat: set[tuple[str, str]],
    min_moment: float,
    max_moment: float,
) -> tuple[float, int]:
    """在 Python 侧对一份“箱号 -> 槽位编号”配载做全部硬约束完整复核。

    不盲信求解器（含其数值容差）：任一约束不满足即抛
    :class:`InfeasibleStowage`。返回 ``(总力矩, 最高占用层)``。
    """
    slot_by_code = {s.code: s for s in slots}
    cont_by_no = {c.no: c for c in containers}

    if set(assignments) != set(cont_by_no):
        raise InfeasibleStowage("求解结果未让每个箱恰好占一个槽，请重试")
    used = list(assignments.values())
    if len(set(used)) != len(used):
        raise InfeasibleStowage("求解结果存在两个箱占据同一槽位，请重试")

    placed: dict[str, tuple[Container, Slot]] = {}
    for cno, scode in assignments.items():
        s = slot_by_code.get(scode)
        if s is None:
            raise InfeasibleStowage(f"求解结果引用了不存在的槽位 {scode}，请重试")
        c = cont_by_no[cno]
        if c.weight > s.capacity + EPS:
            raise InfeasibleStowage(
                f"槽位 {scode} 承重 {s.capacity} 不足以承载箱 {cno}（重 {c.weight}），请重试"
            )
        placed[cno] = (c, s)

    # 同堆栈：不悬空、下层不轻于上层、早卸港箱不在晚卸港箱下方
    by_stack: dict[str, list[tuple[Slot, Container]]] = {}
    for c, s in placed.values():
        by_stack.setdefault(s.stack, []).append((s, c))
    for stack, members in by_stack.items():
        members.sort(key=lambda t: t[0].tier)
        # 已占用层必须自第 1 层起逐层连续（最低层不为 1 即悬空）
        if members[0][0].tier != 1:
            raise InfeasibleStowage(
                f"堆栈 {stack} 最低占用槽位 {members[0][0].code} 悬空（第 1 层无箱），"
                "请重试"
            )
        for idx, ((lower_s, lower_c), (upper_s, upper_c)) in enumerate(
            zip(members, members[1:])
        ):
            if upper_s.tier != lower_s.tier + 1:
                raise InfeasibleStowage(
                    f"堆栈 {stack} 槽位 {upper_s.code} 悬空（其下层无箱），请重试"
                )
            if lower_c.weight < upper_c.weight - EPS:
                raise InfeasibleStowage(
                    f"堆栈 {stack} 下层箱 {lower_c.no}（重 {lower_c.weight}）轻于"
                    f"上层箱 {upper_c.no}（重 {upper_c.weight}），请重试"
                )
            if lower_c.discharge_order < upper_c.discharge_order:
                raise InfeasibleStowage(
                    f"堆栈 {stack} 早卸港箱 {lower_c.no} 被压在晚卸港箱 "
                    f"{upper_c.no} 下方，请重试"
                )

    # 危险类别不相容 -> 不得相邻
    for a_code, b_code in adjacency:
        ca = slot_by_code[a_code]
        cb = slot_by_code[b_code]
        box_a = next((c for c, s in placed.values() if s is ca), None)
        box_b = next((c for c, s in placed.values() if s is cb), None)
        if box_a is not None and box_b is not None:
            if box_a.hazmat and box_b.hazmat and tuple(
                sorted((box_a.hazmat, box_b.hazmat))
            ) in incompat:
                raise InfeasibleStowage(
                    f"不相容危险类别箱 {box_a.no}（{box_a.hazmat}）与 {box_b.no}"
                    f"（{box_b.hazmat}）占据相邻槽位 {a_code}/{b_code}，请重试"
                )

    total_moment = sum(c.weight * s.arm for c, s in placed.values())
    scale = max(1.0, abs(min_moment), abs(max_moment))
    if not (min_moment - EPS * scale <= total_moment <= max_moment + EPS * scale):
        raise InfeasibleStowage(
            f"总力矩 {round(total_moment, 6)} 不在允许区间 "
            f"[{min_moment}, {max_moment}] 内，请重试"
        )
    max_tier = max((s.tier for _, s in placed.values()), default=0)
    return round(total_moment, 6), int(max_tier)


def solve_restow(
    containers: Sequence[Container],
    slots: Sequence[Slot],
    adjacency: Sequence[tuple[str, str]] | set[tuple[str, str]],
    incompatible_classes: Sequence[tuple[str, str]],
    min_moment: float,
    max_moment: float,
    original: Mapping[str, str],
    blocked: Sequence[str] | set[str],
) -> RestowResult:
    """求解应急改配草稿。

    保持全部箱（``containers``，即当前最新箱清单）参与配载；``blocked``
    槽位禁止占用；``original`` 为当前已确认方案的“箱号 -> 槽位编号”。
    三阶段优选：①最小化换槽箱数；②最小化最高占用层；③按箱号升序取槽位
    编号序列字典序最小。无可行改配时抛 :class:`InfeasibleStowage`。
    """
    adj_set = {tuple(sorted(p)) for p in adjacency}
    incompat = {tuple(sorted(p)) for p in incompatible_classes}
    blocked_set = frozenset(blocked)
    available = [s for s in slots if s.code not in blocked_set]

    if len(containers) > len(available):
        raise InfeasibleStowage(
            f"封锁 {len(blocked_set)} 个槽位后，可用槽位 {len(available)} 个，"
            f"少于箱数 {len(containers)}，无法完整改配"
        )
    capacities = {s.code: s.capacity for s in available}
    for c in containers:
        if c.weight > max(capacities.values(), default=0):
            raise InfeasibleStowage(
                f"箱 {c.no} 重量 {c.weight} 超过封锁后任一可用槽位承重，无法改配"
            )
    # 原槽被封的箱必须换槽
    forced = {cno for cno, scode in original.items() if scode in blocked_set}

    if not containers:
        if min_moment <= 0 <= max_moment:
            return RestowResult([], 0.0, 0, 0)
        raise InfeasibleStowage(
            f"无箱时总力矩为 0，不在允许区间 [{min_moment}, {max_moment}] 内"
        )

    # 第一阶段：最小化换槽箱数
    prob1, x1, _, sbc1, _, m1 = _build_model(
        containers, slots, adj_set, incompat, min_moment, max_moment,
        blocked=blocked_set, original=original, minimize_moves=True,
    )
    status = _solve(prob1)
    if status == "Infeasible":
        raise InfeasibleStowage(
            "封锁槽位后，不存在满足承重、堆叠、重量次序、卸港先后、危险类别"
            "相邻限制且总力矩处于允许区间的完整改配方案"
        )
    if status != "Optimal":
        raise InfeasibleStowage(
            f"求解器在 {TIME_LIMIT}s 内未能判定可行改配（阶段一，状态：{status}），"
            "请缩小规模或重试"
        )
    moves_star = int(round(pulp.value(m1)))

    # 第二阶段：换槽箱数固定为最优，最小化最高占用层
    prob2, x2, T2, sbc2, _, _ = _build_model(
        containers, slots, adj_set, incompat, min_moment, max_moment,
        blocked=blocked_set, original=original, moves_bound=moves_star,
    )
    status = _solve(prob2)
    if status != "Optimal":
        raise InfeasibleStowage(
            f"求解器在 {TIME_LIMIT}s 内未能判定可行改配（阶段二，状态：{status}），"
            "请缩小规模或重试"
        )
    t_star = int(round(pulp.value(T2)))

    # 第三阶段：换槽数与最高层均固定，按箱号升序分块求槽位序列字典序最优
    ordered_cnos = [c.no for c in sorted(containers, key=lambda c: c.no)]
    n_slots = len(slots)
    digits = max(1, len(str(n_slots)))
    chunk = max(1, 13 // digits)
    fixed: dict[str, str] = {}
    last_x = x2
    last_sbc = sbc2
    for start in range(0, len(ordered_cnos), chunk):
        block = ordered_cnos[start:start + chunk]
        prob, x, _, sbc, _, _ = _build_model(
            containers, slots, adj_set, incompat, min_moment, max_moment,
            fixed=fixed, t_bound=t_star, lex_boxes=block,
            blocked=blocked_set, original=original, moves_bound=moves_star,
        )
        bstatus = _solve(prob)
        if bstatus != "Optimal":
            raise InfeasibleStowage(f"字典序优化失败（状态：{bstatus}），请重试")
        for cno in block:
            for scode in sorted(sbc):
                if pulp.value(x[cno, scode]) is not None and pulp.value(x[cno, scode]) > 0.5:
                    fixed[cno] = scode
                    break
        last_x, last_sbc = x, sbc

    mapping: dict[str, str] = {}
    assignments: list[Assignment] = []
    for c in containers:
        chosen = None
        for scode, s in last_sbc.items():
            if pulp.value(last_x[c.no, scode]) is not None and pulp.value(last_x[c.no, scode]) > 0.5:
                chosen = scode
                assignments.append(
                    Assignment(c.no, scode, s.stack, s.tier, c.weight, s.arm)
                )
                break
        if chosen is None:
            raise InfeasibleStowage(f"箱 {c.no} 的改配槽位求解器未能确定，请重试")
        mapping[c.no] = chosen

    # Python 侧完整复核：不盲信求解器数值容差，任何不确定都拒绝而非保存
    total_moment, max_tier = verify_stowage(
        mapping, containers, slots, adj_set, incompat, min_moment, max_moment
    )
    for scode in blocked_set:
        if scode in mapping.values():
            raise InfeasibleStowage(f"封锁槽位 {scode} 仍被占用，请重试")
    moved = sorted(cno for cno, scode in mapping.items() if original.get(cno) != scode)
    if not set(forced).issubset(set(moved)):
        raise InfeasibleStowage("原槽被封锁的箱未全部换槽，请重试")
    return RestowResult(
        assignments=sorted(assignments, key=lambda a: a.container_no),
        total_moment=total_moment,
        max_occupied_tier=max_tier,
        moved_boxes=len(moved),
    )
