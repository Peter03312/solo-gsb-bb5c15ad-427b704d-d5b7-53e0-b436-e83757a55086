"""求解器单元测试。"""

import pytest

from app.solver import (
    Container,
    InfeasibleStowage,
    Slot,
    solve_stowage,
)


def make_slots(stack_tiers=("A", "B"), tiers=3, cap=100.0, arms=None):
    arms = arms or {1: 2.0, 2: 3.0, 3: 4.0}
    return [
        Slot(f"{s}{t:02d}", s, t, cap, arms[t])
        for s in stack_tiers
        for t in range(1, tiers + 1)
    ]


def test_min_max_tier_and_lex_smallest_sequence():
    slots = make_slots()
    # 2 箱 -> 最优只占第 1 层；箱号升序对应槽位序列字典序最小
    containers = [Container("C02", 10, None, 1), Container("C01", 10, None, 1)]
    r = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    assert r.max_occupied_tier == 1
    pairs = [(a.container_no, a.slot_code) for a in r.assignments]
    assert pairs == [("C01", "A01"), ("C02", "B01")]


def test_lex_tie_break_with_four_boxes():
    slots = make_slots()
    containers = [Container(f"C0{i}", w, None, 1) for i, w in
                  enumerate([25, 20, 15, 10], start=1)]
    r = solve_stowage(containers, slots, [], [], -1e9, 1e9)
    assert r.max_occupied_tier == 2
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    # 逐箱贪心：C01 占 A01；C02 占可行最小编号 A02；C03 占 B01；C04 占 B02
    assert mapping == {"C01": "A01", "C02": "A02", "C03": "B01", "C04": "B02"}
    # 下层不轻于上层
    by = {a.slot_code: a.weight for a in r.assignments}
    assert by["A01"] >= by["A02"] and by["B01"] >= by["B02"]


def test_support_required_upper_without_lower():
    # 只有一个堆栈两层、两箱：必占两层，合法
    slots = make_slots(stack_tiers=("A",), tiers=2)
    r = solve_stowage(
        [Container("X", 50, None, 1), Container("Y", 10, None, 1)],
        slots, [], [], -1e9, 1e9,
    )
    assert {a.slot_code for a in r.assignments} == {"A01", "A02"}
    lower = next(a for a in r.assignments if a.slot_code == "A01")
    upper = next(a for a in r.assignments if a.slot_code == "A02")
    assert lower.weight >= upper.weight


def test_capacity_violation_infeasible():
    slots = [Slot("A01", "A", 1, 5, 2.0)]
    with pytest.raises(InfeasibleStowage):
        solve_stowage([Container("X", 10, None, 1)], slots, [], [], -1e9, 1e9)


def test_discharge_order_infeasible_when_lighter_is_late():
    # 早卸港重箱 E 必须在下，轻箱 L（晚卸）在上：重量规则要求轻者在上，
    # 但晚卸箱在上又压早卸箱 -> 冲突，无解
    slots = make_slots(stack_tiers=("A",), tiers=2)
    with pytest.raises(InfeasibleStowage):
        solve_stowage(
            [Container("E", 20, None, 1), Container("L", 10, None, 2)],
            slots, [], [], -1e9, 1e9,
        )


def test_discharge_order_feasible_when_early_on_top():
    slots = make_slots(stack_tiers=("A",), tiers=2)
    r = solve_stowage(
        [Container("E", 10, None, 1), Container("L", 20, None, 2)],
        slots, [], [], -1e9, 1e9,
    )
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert mapping == {"E": "A02", "L": "A01"}


def test_incompatible_hazmat_adjacency():
    slots = [Slot("A01", "A", 1, 100, 2), Slot("B01", "B", 1, 100, 2)]
    adj = [("A01", "B01")]
    with pytest.raises(InfeasibleStowage):
        solve_stowage(
            [Container("X", 10, "1", 1), Container("Y", 10, "5", 1)],
            slots, adj, [("1", "5")], -1e9, 1e9,
        )


def test_moment_window_enforced():
    slots = make_slots()
    with pytest.raises(InfeasibleStowage):
        solve_stowage([Container("X", 10, None, 1)], slots, [], [], 1000, 2000)
    r = solve_stowage([Container("X", 10, None, 1)], slots, [], [], 20, 20)
    assert r.total_moment == 20
    assert r.assignments[0].slot_code == "A01"


def test_more_boxes_than_slots():
    slots = [Slot("A01", "A", 1, 100, 1)]
    with pytest.raises(InfeasibleStowage):
        solve_stowage(
            [Container("X", 1, None, 1), Container("Y", 1, None, 1)],
            slots, [], [], -1e9, 1e9,
        )


def test_empty_manifest():
    slots = make_slots()
    r = solve_stowage([], slots, [], [], -1, 1)
    assert r.assignments == [] and r.total_moment == 0 and r.max_occupied_tier == 0
    with pytest.raises(InfeasibleStowage):
        solve_stowage([], slots, [], [], 1, 2)
