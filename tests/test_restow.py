"""应急改配（临时封锁槽位）单元测试。"""

import pytest

from app.solver import (
    Container,
    InfeasibleStowage,
    Slot,
    solve_restow,
)


def make_slots(stack_tiers=("A", "B", "C"), tiers=3, cap=100.0,
               arms=None, codes_by_tier=None):
    arms = arms or {1: 2.0, 2: 3.0, 3: 4.0}
    return [
        Slot(f"{s}{t:02d}", s, t, cap, arms[t])
        for s in stack_tiers
        for t in range(1, tiers + 1)
    ]


def test_no_move_when_blocked_slot_is_empty():
    slots = make_slots()
    cs = [Container("C01", 10, None, 1), Container("C02", 10, None, 1)]
    orig = {"C01": "A01", "C02": "B01"}
    r = solve_restow(cs, slots, [], [], -1e9, 1e9, original=orig, blocked=["C01"])
    assert r.moved_boxes == 0
    assert {a.slot_code for a in r.assignments} == {"A01", "B01"}
    assert r.max_occupied_tier == 1


def test_minimize_moved_boxes_first():
    # 封锁 C02 所在 B01：可只移动 C02 到 C01（同层），无需动其他箱
    slots = make_slots()
    cs = [Container("C01", 30, None, 2),
          Container("C02", 20, None, 1),
          Container("C03", 10, None, 1)]
    orig = {"C01": "A01", "C02": "B01", "C03": "C01"}
    r = solve_restow(cs, slots, [], [], -1e9, 1e9, original=orig, blocked=["B01"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert r.moved_boxes == 1
    assert mapping["C01"] == "A01" and mapping["C03"] == "C01"
    assert mapping["C02"] not in {"A01", "B01", "C01"}
    # 阻塞槽不得占用、全部箱保留
    assert "B01" not in mapping.values()
    assert len(mapping) == 3


def test_minimize_tier_second_lex_third():
    # 两个候选空位：C01（一层，优先）与 A02（二层）。
    # 最少换槽数同为 1 时应选一层；同层多个候选时按箱号升序槽位序列字典序最小。
    slots = make_slots(stack_tiers=("A", "B", "C", "D"), tiers=2)
    cs = [Container("C01", 30, None, 2),
          Container("C02", 20, None, 1),
          Container("C03", 10, None, 1)]
    orig = {"C01": "A01", "C02": "B01", "C03": "B02"}
    r = solve_restow(cs, slots, [], [], -1e9, 1e9, original=orig, blocked=["B02"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert r.moved_boxes == 1
    assert r.max_occupied_tier == 1
    # 可去的一层空槽为 C01/D01；字典序最小槽位序列 -> C03 应占 C01
    assert mapping["C03"] == "C01"


def test_support_chain_preserved_when_lower_blocked():
    # 下层槽被封锁时，原压在其上的箱也必须换槽（不悬空/不违反重量次序）
    slots = make_slots(stack_tiers=("A", "B"), tiers=3)
    cs = [Container("C01", 30, None, 2),
          Container("C02", 20, None, 1),
          Container("C03", 10, None, 1)]
    orig = {"C01": "A01", "C02": "A02", "C03": "B01"}
    r = solve_restow(cs, slots, [], [], -1e9, 1e9, original=orig, blocked=["A01"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert "A01" not in mapping.values()
    # A02 失去支撑，C02 必须离开 A02
    assert mapping["C02"] != "A02"
    weights = {a.slot_code: a.weight for a in r.assignments}
    # 若新方案仍有两层堆叠，下层不轻于上层（复核已在求解器内完成）
    assert r.moved_boxes >= 2


def test_infeasible_when_all_slots_blocked():
    slots = make_slots(stack_tiers=("A",), tiers=1)
    cs = [Container("C01", 10, None, 1)]
    with pytest.raises(InfeasibleStowage):
        solve_restow(cs, slots, [], [], -1e9, 1e9,
                     original={"C01": "A01"}, blocked=["A01"])


def test_hazmat_adjacency_still_enforced():
    slots = [
        Slot("A01", "A", 1, 100, 2), Slot("A02", "A", 2, 100, 3),
        Slot("B01", "B", 1, 100, 2), Slot("B02", "B", 2, 100, 3),
        Slot("C01", "C", 1, 100, 2), Slot("C02", "C", 2, 100, 3),
    ]
    adj = [("A01", "B01"), ("B01", "C01")]
    cs = [Container("X", 10, "1", 1), Container("Y", 10, "5", 1)]
    # 原方案 X@A01, Y@C01（不相邻）；封锁 C01 后：
    # B01 与 X 相邻（危险类别 1/5 不相容）不可取，B02/C02 悬空不可取，
    # 唯一可行是 Y 叠到 A02（A01 有支撑、等重同港），X 留 A01 -> 仅 Y 换槽
    orig = {"X": "A01", "Y": "C01"}
    r = solve_restow(cs, slots, adj, [("1", "5")], -1e9, 1e9,
                     original=orig, blocked=["C01"])
    mapping = {a.container_no: a.slot_code for a in r.assignments}
    assert mapping == {"X": "A01", "Y": "A02"}
    assert r.moved_boxes == 1

    # 再封 A02 与 B02：仅剩一层 A01/B01 相邻位与悬空的 C02，
    # 不相容两箱无法相邻、二层槽无支撑 -> 无解
    with pytest.raises(InfeasibleStowage):
        solve_restow(cs, slots, adj, [("1", "5")], -1e9, 1e9,
                     original=orig, blocked=["C01", "A02", "B02"])


def test_moment_window_still_enforced():
    slots = make_slots()
    cs = [Container("C01", 10, None, 1)]
    # 一槽力臂 2 => 力矩 20；封锁 A01 后仍有 B01(arm2)，窗口 [30,40] 无解
    with pytest.raises(InfeasibleStowage):
        solve_restow(cs, slots, [], [], 30, 40,
                     original={"C01": "A01"}, blocked=["A01"])


def test_empty_manifest_restow():
    slots = make_slots()
    r = solve_restow([], slots, [], [], -1, 1, original={}, blocked=["A01"])
    assert r.assignments == [] and r.moved_boxes == 0 and r.max_occupied_tier == 0
