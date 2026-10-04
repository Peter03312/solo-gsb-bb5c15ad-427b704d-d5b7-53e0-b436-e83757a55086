"""装船时间表求解器单元测试。"""

import pytest

from app.scheduler import InfeasibleSchedule, Job, solve_loading


def j(no, stack="A", tier=1, duration=10, release=0, deadline=100):
    return Job(no, stack, tier, duration, release, deadline)


def test_empty_returns_makespan_zero():
    r = solve_loading([], 3)
    assert r.jobs == [] and r.makespan == 0


def test_single_crane_respects_stack_order_and_lex():
    # A 栈两层：C1 在下、C2 在上，单桥 -> 必须 C1[0,10) 后 C2[10,20)
    jobs = [j("C2", "A", 2), j("C1", "A", 1)]
    r = solve_loading(jobs, 1)
    assert r.makespan == 20
    m = {x.container_no: x for x in r.jobs}
    assert (m["C1"].start_minute, m["C1"].finish_minute, m["C1"].crane_no) == (0, 10, 1)
    assert (m["C2"].start_minute, m["C2"].finish_minute, m["C2"].crane_no) == (10, 20, 1)


def test_two_cranes_independent_boxes_get_parallel_and_small_crane_ids():
    jobs = [j("X", "B"), j("A", "A")]
    r = solve_loading(jobs, 2)
    m = {x.container_no: x for x in r.jobs}
    assert r.makespan == 10
    assert (m["A"].start_minute, m["A"].crane_no) == (0, 1)
    assert (m["X"].start_minute, m["X"].crane_no) == (0, 2)


def test_makespan_minimized_before_lex():
    # 3 个相互独立的箱，2 台桥，时长均 10 -> 最优 makespan 20（2+1），
    # 两台桥都要用上；箱号序字典序：C001、C002 同 0 时刻，桥 1/2
    jobs = [j(f"C00{i}", stack=chr(ord("A") + i - 1)) for i in range(1, 4)]
    r = solve_loading(jobs, 2)
    assert r.makespan == 20
    m = {x.container_no: x for x in r.jobs}
    assert (m["C001"].start_minute, m["C001"].crane_no) == (0, 1)
    assert (m["C002"].start_minute, m["C002"].crane_no) == (0, 2)
    assert m["C003"].start_minute == 10


def test_release_time_honored():
    # C1 最早 5 开工；C2 独立、0 可开工。单桥：
    # 最短 makespan=20 由 C2[0,10)、C1[10,20) 达成（释放 5 的 C1 无法 0 开工）
    jobs = [j("C1", "A", release=5), j("C2", "B")]
    r = solve_loading(jobs, 1)
    assert r.makespan == 20
    m = {x.container_no: x for x in r.jobs}
    assert m["C1"].start_minute == 10 and m["C1"].start_minute >= 5
    assert m["C2"].start_minute == 0

    # 两台桥时 C1 可在 5 开工
    r2 = solve_loading(jobs, 2)
    assert r2.makespan == 15
    assert {x.container_no: x.start_minute for x in r2.jobs}["C1"] == 5


def test_finish_may_equal_deadline():
    r = solve_loading([j("C1", deadline=10)], 1)
    assert r.makespan == 10
    assert r.jobs[0].finish_minute == 10  # 恰在最迟完工时刻结束，允许


def test_window_too_tight_infeasible():
    with pytest.raises(InfeasibleSchedule):
        solve_loading([j("C1", duration=10, release=5, deadline=14)], 1)


def test_stack_chain_deadline_infeasible():
    # 同栈两层各 10 分钟，单桥顺序作业至少 20 分钟，上层最迟完工 15 -> 无解
    jobs = [j("UP", "A", 2, deadline=15), j("LO", "A", 1, deadline=100)]
    with pytest.raises(InfeasibleSchedule):
        solve_loading(jobs, 1)


def test_capacity_overload_infeasible():
    # 单桥两箱各需 10，两者最迟完工都是 10（无堆栈关系）-> 无法同时满足 -> 无解
    jobs = [j("X", "A", deadline=10), j("Y", "B", deadline=10)]
    with pytest.raises(InfeasibleSchedule):
        solve_loading(jobs, 1)


def test_no_crane_overlap_and_schedule_consistency():
    # 4 箱（两栈各两层），2 桥，随机窗口；校验所有硬性约束
    jobs = [
        j("A1", "A", 1, duration=7),
        j("A2", "A", 2, duration=5),
        j("B1", "B", 1, duration=6),
        j("B2", "B", 2, duration=8),
    ]
    r = solve_loading(jobs, 2)
    by = {x.container_no: x for x in r.jobs}
    # 堆栈先后
    assert by["A1"].finish_minute <= by["A2"].start_minute
    assert by["B1"].finish_minute <= by["B2"].start_minute
    # 时长一致
    src = {x.no: x for x in jobs}
    for x in r.jobs:
        assert x.finish_minute - x.start_minute == src[x.container_no].duration
        assert 1 <= x.crane_no <= 2
        assert x.start_minute >= src[x.container_no].release
        assert x.finish_minute <= src[x.container_no].deadline
    # 同桥不重叠（[s,f)）
    for c in (1, 2):
        iv = sorted((x.start_minute, x.finish_minute)
                    for x in r.jobs if x.crane_no == c)
        for (s1, f1), (s2, f2) in zip(iv, iv[1:]):
            assert f1 <= s2
    assert r.makespan == max(x.finish_minute for x in r.jobs)


def test_lex_prefers_earlier_start_for_first_box():
    # C001 释放时间为 8，C002 释放 0；2 桥。
    # makespan 最优下 C001 不可能早于 8 开工；C002 应取 0 时刻桥 1？
    # 字典序第一箱是 C001：其最早可开工 8，桥 1；随后 C002 取剩余最优。
    jobs = [j("C001", "A", release=8), j("C002", "B", release=0)]
    r = solve_loading(jobs, 2)
    m = {x.container_no: x for x in r.jobs}
    assert (m["C001"].start_minute, m["C001"].crane_no) == (8, 1)
    assert m["C002"].start_minute == 0
