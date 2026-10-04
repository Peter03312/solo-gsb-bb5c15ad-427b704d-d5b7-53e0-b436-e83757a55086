"""逐港卸箱顺序求解器单元测试（含随机小规模穷举交叉验证）。"""

from __future__ import annotations

import itertools
import random

import pytest

from app.unloader import (
    InfeasibleUnloading,
    PlacedContainer,
    PortWindow,
    solve_unloading,
)


def box(no, port, stack, tier, weight=10.0, arm=2.0):
    return PlacedContainer(no, weight, arm, stack, tier, port)


def _brute_force(boxes, windows, tol=1e-6):
    """穷举全部排列，返回满足全部硬约束的箱号序列集合（字典序升序）。"""
    by_no = {b.no: b for b in boxes}
    win = {w.discharge_order: (w.min_remaining_moment, w.max_remaining_moment)
           for w in windows}
    orders = sorted({b.discharge_order for b in boxes})
    expected_orders = sorted(win)
    if orders != expected_orders:
        return set()
    feasible = set()
    m0 = sum(b.weight * b.arm for b in boxes)
    for perm in itertools.permutations([b.no for b in boxes]):
        # 港口顺序：港序非降（等价：按港序号从小到大，每港只卸该港箱）
        port_seq = [by_no[no].discharge_order for no in perm]
        if sorted(port_seq) != port_seq:
            continue
        # 同堆栈上层先卸：任一同栈更高层箱必须更早出现
        pos = {no: i for i, no in enumerate(perm)}
        ok = True
        for b in boxes:
            for c in boxes:
                if c.stack == b.stack and c.tier > b.tier and pos[c.no] > pos[b.no]:
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue
        remaining = m0
        for no in perm:
            b = by_no[no]
            remaining -= b.weight * b.arm
            lo, hi = win[b.discharge_order]
            if not (lo - tol <= remaining <= hi + tol):
                ok = False
                break
        if ok:
            feasible.add(perm)
    return feasible


def test_empty_plan():
    r = solve_unloading([], [])
    assert r.steps == []
    assert r.initial_moment == 0 and r.final_moment == 0


def test_single_box_port_order_and_fields():
    r = solve_unloading(
        [box("C01", 1, "A", 1, weight=10, arm=2.5)],
        [PortWindow(1, 0, 100)],
    )
    assert len(r.steps) == 1
    s = r.steps[0]
    assert (s.step_no, s.discharge_order, s.container_no) == (1, 1, "C01")
    assert s.remaining_moment == 0
    assert r.initial_moment == 25


def test_two_ports_stack_order_enforced():
    # A 栈：上层 E（港1）、下层 L（港2）；B 箱 X（港1）
    boxes = [
        box("E", 1, "A", 2, weight=10, arm=2),
        box("L", 2, "A", 1, weight=20, arm=2),
        box("X", 1, "B", 1, weight=10, arm=2),
    ]
    r = solve_unloading(
        boxes, [PortWindow(1, -1000, 1000), PortWindow(2, -1000, 1000)]
    )
    seq = [s.container_no for s in r.steps]
    # 港1 两箱先卸（E/X 字典序 E 在前），最后 L；E 必在 L 之前
    assert seq[:2] == ["E", "X"] and seq[2] == "L"
    assert [s.discharge_order for s in r.steps] == [1, 1, 2]
    assert r.steps[-1].remaining_moment == 0


def test_lex_min_within_port():
    # 两独立箱同港，宽区间 -> 字典序 C001 先
    boxes = [box("C002", 1, "B", 1), box("C001", 1, "A", 1)]
    r = solve_unloading(boxes, [PortWindow(1, -1000, 1000)])
    assert [s.container_no for s in r.steps] == ["C001", "C002"]


def test_moment_window_can_force_non_greedy_first_pick():
    # M0 = -30：C001 卸后 -10；C003 卸后 -80。
    # 港1 区间 [-100,-30] 排除字典序首选 C001 的第一步 -> 必须 C003 先
    boxes = [
        box("C001", 1, "A", 2, weight=10, arm=-2),
        box("C002", 2, "A", 1, weight=30, arm=-2),
        box("C003", 1, "B", 1, weight=10, arm=5),
    ]
    r = solve_unloading(
        boxes, [PortWindow(1, -100, -30), PortWindow(2, -100, 100)]
    )
    assert [s.container_no for s in r.steps] == ["C003", "C001", "C002"]
    assert [s.remaining_moment for s in r.steps] == [-80, -60, 0]


def test_stack_precedence_infeasible_detected():
    # 上层港1、下层港2：港1区间只允许 -5（恰好在两港1箱之一卸完），
    # 但上层 E 必须在 X 之前或反之；构造仅 E 单独卸完可达、X 单独不可达：
    # M0: E=-10, L=-40, X=50 -> 0；只卸 E -> 10，只卸 X -> -50，
    # 区间 [-5,-5] 两个第一步都落不进，且 stack 要求 E 在 L 前 -> 无解
    boxes = [
        box("E", 1, "A", 2, weight=10, arm=-1),
        box("L", 2, "A", 1, weight=20, arm=-2),
        box("X", 1, "B", 1, weight=10, arm=5),
    ]
    with pytest.raises(InfeasibleUnloading):
        solve_unloading(
            boxes, [PortWindow(1, -5, -5), PortWindow(2, -1000, 1000)]
        )


def test_last_port_window_must_contain_zero():
    boxes = [box("C01", 1, "A", 1)]
    with pytest.raises(InfeasibleUnloading) as ei:
        solve_unloading(boxes, [PortWindow(1, 1, 10)])
    assert "末港" in ei.value.reason


def test_port_set_must_match_manifest_exactly():
    boxes = [box("C01", 1, "A", 1), box("C02", 2, "B", 1)]
    with pytest.raises(InfeasibleUnloading) as ei:
        solve_unloading(boxes, [PortWindow(1, -100, 100)])
    assert "完全一致" in ei.value.reason and "缺少" in ei.value.reason
    with pytest.raises(InfeasibleUnloading) as ei:
        solve_unloading(
            boxes,
            [PortWindow(1, -100, 100), PortWindow(2, -100, 100),
             PortWindow(3, -100, 100)],
        )
    assert "多余" in ei.value.reason
    with pytest.raises(InfeasibleUnloading):
        solve_unloading(
            [box("C01", 1, "A", 1)],
            [PortWindow(1, -100, 100), PortWindow(1, -50, 50)],
        )


def test_inverted_window_rejected():
    with pytest.raises(InfeasibleUnloading):
        solve_unloading([box("C01", 1, "A", 1)], [PortWindow(1, 10, 0)])


def _random_case(rng):
    """生成一个“由可行配载产出”的箱位布局：

    * 每栈自底向上连续布箱，不悬空；
    * 下层重量 >= 上层重量；
    * 上层卸港序号 <= 下层（先卸港在上）。
    """
    n_ports = rng.randint(1, 3)
    boxes = []
    for s in ["A", "B", "C"]:
        height = rng.randint(0, 3)
        chain = []
        for t in range(1, height + 1):
            if t == 1:
                port = rng.randint(1, n_ports)
                weight = float(rng.randint(10, 40))
            else:
                lower = chain[-1]
                port = rng.randint(1, lower[2])
                weight = float(rng.randint(5, int(lower[1])))
            arm = float(rng.randint(-4, 5))
            chain.append((t, weight, port, arm))
        for t, weight, port, arm in chain:
            boxes.append(PlacedContainer(f"{s}{t}", weight, arm, s, t, port))
    return boxes


def test_random_brute_force_cross_validation():
    rng = random.Random(20261001)
    checked = 0  # 求解器成功给出顺序且与穷举最优一致的可行案例
    examined = 0  # 实际处理的非空案例总数
    for _ in range(300):
        boxes = _random_case(rng)
        if not boxes:
            continue
        examined += 1
        orders = sorted({b.discharge_order for b in boxes})
        m0 = sum(b.weight * b.arm for b in boxes)
        # 随机生成区间：围绕若干关键剩余力矩取宽/窄窗口
        windows = []
        for d in orders:
            lo = rng.choice([-1e9, -200, -50, -5, 0])
            hi = rng.choice([0, 5, 50, 200, 1e9])
            if lo > hi:
                lo, hi = hi, lo
            windows.append(PortWindow(d, float(lo), float(hi)))
        # 末港窗口必须含 0，否则按设计必无解
        windows[-1] = PortWindow(orders[-1], min(windows[-1].min_remaining_moment, 0),
                                 max(windows[-1].max_remaining_moment, 0))
        feasible = _brute_force(boxes, windows)
        try:
            r = solve_unloading(boxes, windows)
        except InfeasibleUnloading:
            assert feasible == set(), f"求解器漏解: {boxes}, {windows}"
            continue
        expected = min(feasible) if feasible else None
        assert feasible, f"求解器给出不可行解: {boxes}, {windows}"
        got = tuple(s.container_no for s in r.steps)
        assert got == expected, (
            f"字典序不一致: got={got} expected={expected}; "
            f"feasible={sorted(feasible)}; boxes={boxes}; windows={windows}"
        )
        # 逐步港序与剩余力矩复核
        by_no = {b.no: b for b in boxes}
        win = {w.discharge_order: (w.min_remaining_moment, w.max_remaining_moment)
               for w in windows}
        remaining = m0
        for s in r.steps:
            b = by_no[s.container_no]
            remaining -= b.weight * b.arm
            assert abs(remaining - s.remaining_moment) < 1e-5
            lo, hi = win[s.discharge_order]
            assert lo - 1e-6 <= s.remaining_moment <= hi + 1e-6
        checked += 1
    assert examined > 200 and checked > 80, (examined, checked)
