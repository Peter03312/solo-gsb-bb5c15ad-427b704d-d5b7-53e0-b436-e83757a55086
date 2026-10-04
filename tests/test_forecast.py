"""岸桥停机剩余作业预测求解器（app.forecast）单元测试。"""

from __future__ import annotations

import pytest

from app.forecast import (
    ActiveJob,
    Downtime,
    InfeasibleForecast,
    PendingJob,
    solve_forecast,
)


def _entry_map(result):
    return {e.container_no: e for e in result.entries}


def test_no_unfinished_returns_cutoff():
    r = solve_forecast(
        pending=[], active=[], downtimes=[], crane_count=2, cutoff=30
    )
    assert r.entries == []
    assert r.latest_finish_minute == 30


def test_active_only_keeps_crane_and_continuous():
    r = solve_forecast(
        pending=[],
        active=[ActiveJob("C001", "A", 1, crane_no=2, remaining=7)],
        downtimes=[],
        crane_count=2,
        cutoff=10,
    )
    e = _entry_map(r)["C001"]
    assert (e.start_minute, e.finish_minute, e.crane_no, e.kind) == (10, 17, 2,
                                                                      "in_progress")
    assert r.latest_finish_minute == 17


def test_active_overlapping_downtime_is_infeasible():
    with pytest.raises(InfeasibleForecast) as ei:
        solve_forecast(
            pending=[],
            active=[ActiveJob("C001", "A", 1, crane_no=1, remaining=7)],
            downtimes=[Downtime(1, 12, 20)],
            crane_count=1,
            cutoff=10,
        )
    assert "连续" in ei.value.reason or "停机" in ei.value.reason


def test_active_finish_exactly_at_downtime_start_allowed():
    # active 10..15，停机 [15,20) 半开区间首尾相接，允许
    r = solve_forecast(
        pending=[],
        active=[ActiveJob("C001", "A", 1, crane_no=1, remaining=5)],
        downtimes=[Downtime(1, 15, 20)],
        crane_count=1,
        cutoff=10,
    )
    assert r.latest_finish_minute == 15


def test_pending_uses_free_crane_and_downtime_gap():
    # 单桥：停机 [15,18)；active 10..15；pending dur4 -> 只能 18..22
    r = solve_forecast(
        pending=[PendingJob("C009", "B", 1, duration=4, release=0, deadline=60)],
        active=[ActiveJob("C001", "A", 1, crane_no=1, remaining=5)],
        downtimes=[Downtime(1, 15, 18)],
        crane_count=1,
        cutoff=10,
    )
    e = _entry_map(r)["C009"]
    assert (e.start_minute, e.finish_minute, e.crane_no) == (18, 22, 1)
    assert r.latest_finish_minute == 22


def test_pending_job_fits_in_gap_before_downtime():
    # 桥2 停机 [15,40)；待开工 dur3 可在 [12,15) 缝隙恰好完工
    r = solve_forecast(
        pending=[PendingJob("C004", "B", 2, duration=3, release=0, deadline=60)],
        active=[ActiveJob("C002", "A", 2, crane_no=1, remaining=4)],
        downtimes=[Downtime(2, 15, 40)],
        crane_count=2,
        cutoff=12,
    )
    e = _entry_map(r)["C004"]
    assert (e.start_minute, e.finish_minute, e.crane_no) == (12, 15, 2)


def test_stack_precedence_pending_chain():
    # 同栈三层：底层待开工 dur2、上层待开工 dur2、再上层 active（不可能），
    # 这里只构造 pending->pending：A1 dur2、A2 dur2，单桥
    # -> A1 0..2, A2 2..4
    r = solve_forecast(
        pending=[
            PendingJob("TOP", "A", 2, duration=2, release=0, deadline=60),
            PendingJob("BOT", "A", 1, duration=2, release=0, deadline=60),
        ],
        active=[],
        downtimes=[],
        crane_count=1,
        cutoff=0,
    )
    m = _entry_map(r)
    assert (m["BOT"].start_minute, m["BOT"].finish_minute) == (0, 2)
    assert (m["TOP"].start_minute, m["TOP"].finish_minute) == (2, 4)


def test_stack_precedence_active_blocks_pending_above():
    # active 中层 10..14；同栈上层 pending dur4 -> 不早于 14
    r = solve_forecast(
        pending=[PendingJob("TOP", "A", 3, duration=4, release=0, deadline=60)],
        active=[ActiveJob("MID", "A", 2, crane_no=2, remaining=4)],
        downtimes=[],
        crane_count=2,
        cutoff=10,
    )
    e = _entry_map(r)["TOP"]
    assert e.start_minute == 14 and e.finish_minute == 18


def test_deadline_window_too_tight_after_cutoff():
    # release 0 但 cutoff=10，dur5，deadline=13 -> 最早 10+5=15 > 13
    with pytest.raises(InfeasibleForecast):
        solve_forecast(
            pending=[PendingJob("C001", "A", 1, duration=5, release=0, deadline=13)],
            active=[],
            downtimes=[],
            crane_count=1,
            cutoff=10,
        )


def test_no_gap_long_enough_is_infeasible():
    # 单桥，cutoff=0：停机 [0,100)；任何待开工箱都无法在 deadline 内完成
    with pytest.raises(InfeasibleForecast):
        solve_forecast(
            pending=[PendingJob("C001", "A", 1, duration=3, release=0, deadline=50)],
            active=[],
            downtimes=[Downtime(1, 0, 100)],
            crane_count=1,
            cutoff=0,
        )


def test_makespan_and_lex_tiebreak_two_cranes():
    # 两个独立栈等时长箱，两台空闲桥：makespan=5；
    # 平局按箱号升序 (开工,岸桥) -> C001=(0,桥1), C002=(0,桥2)
    r = solve_forecast(
        pending=[
            PendingJob("C002", "B", 1, duration=5, release=0, deadline=60),
            PendingJob("C001", "A", 1, duration=5, release=0, deadline=60),
        ],
        active=[],
        downtimes=[],
        crane_count=2,
        cutoff=0,
    )
    m = _entry_map(r)
    assert r.latest_finish_minute == 5
    assert (m["C001"].start_minute, m["C001"].crane_no) == (0, 1)
    assert (m["C002"].start_minute, m["C002"].crane_no) == (0, 2)


def test_lex_tiebreak_single_crane_order():
    r = solve_forecast(
        pending=[
            PendingJob("C002", "B", 1, duration=5, release=0, deadline=60),
            PendingJob("C001", "A", 1, duration=5, release=0, deadline=60),
        ],
        active=[],
        downtimes=[],
        crane_count=1,
        cutoff=0,
    )
    m = _entry_map(r)
    assert (m["C001"].start_minute, m["C001"].finish_minute) == (0, 5)
    assert (m["C002"].start_minute, m["C002"].finish_minute) == (5, 10)


def test_overlapping_downtimes_merged():
    # 桥1 [5,8) 与 [7,12) 停机，合并为 [5,12)；cutoff=0，dur3 箱：
    # 0..3 可行（缝 [0,5) 恰好）；另一桥不存在 -> 只能 0 开工
    r = solve_forecast(
        pending=[PendingJob("C001", "A", 1, duration=3, release=0, deadline=60)],
        active=[],
        downtimes=[Downtime(1, 5, 8), Downtime(1, 7, 12)],
        crane_count=1,
        cutoff=0,
    )
    e = _entry_map(r)["C001"]
    assert (e.start_minute, e.finish_minute) == (0, 3)
