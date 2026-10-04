"""装船实绩重放引擎单元测试。"""

import pytest

from app.actuals import ActualEvent, ActualsReplayError, replay

# (箱号, 堆栈, 层)：A 栈两层 C001(底)/C002(顶)；B 栈单层 C003
PLAN_ITEMS = [
    ("C001", "A", 1),
    ("C002", "A", 2),
    ("C003", "B", 1),
]


def E(seq, etype, cno, crane, minute, arrival=0):
    return ActualEvent(seq=seq, event_type=etype, container_no=cno,
                       crane_no=crane, minute=minute, arrival_id=arrival or seq)


def test_replay_ordering_finish_before_start_same_minute():
    # C003 已在桥1开工@0；同分钟（10）内 C003 完工、C001 开工：
    # 完工须先重放（否则开工先行会撞桥）
    events = [
        ActualEvent(1, "start", "C003", 1, 0, arrival_id=1),
        ActualEvent(3, "start", "C001", 1, 10, arrival_id=2),
        ActualEvent(2, "finish", "C003", 1, 10, arrival_id=3),
    ]
    result = replay(events, PLAN_ITEMS, 2)
    assert result["order"] == [1, 2, 3]
    assert result["boxes"]["C003"].status == "finished"
    assert result["boxes"]["C001"].status == "in_progress"


def test_start_then_finish_happy_path_and_state():
    events = [
        ActualEvent(1, "start", "C001", 1, 0, 1),
        ActualEvent(2, "finish", "C001", 1, 8, 2),
    ]
    result = replay(events, PLAN_ITEMS, 2)
    box = result["boxes"]["C001"]
    assert box.status == "finished"
    assert (box.start_minute, box.finish_minute, box.crane_no) == (0, 8, 1)
    assert result["unfinished"] == ["C002", "C003"]


def test_duplicate_start_error():
    with pytest.raises(ActualsReplayError, match="重复开工"):
        replay([
            ActualEvent(1, "start", "C001", 1, 0, 1),
            ActualEvent(2, "start", "C001", 2, 5, 2),
        ], PLAN_ITEMS, 2)


def test_finish_without_start_error():
    with pytest.raises(ActualsReplayError, match="严格晚于开工"):
        replay([ActualEvent(1, "finish", "C001", 1, 5, 1)], PLAN_ITEMS, 2)


def test_finish_wrong_crane_error():
    with pytest.raises(ActualsReplayError, match="不一致"):
        replay([
            ActualEvent(1, "start", "C001", 1, 0, 1),
            ActualEvent(2, "finish", "C001", 2, 5, 2),
        ], PLAN_ITEMS, 2)


def test_finish_not_strictly_later_defensive_branch():
    """晚到完工分钟不晚于已登记开工分钟（同分钟或更早）：按分钟重排时完工先于
    开工生效，箱仍处于 pending，按“完工必须严格晚于开工”拒绝。"""
    with pytest.raises(ActualsReplayError, match="严格晚于开工"):
        replay([
            ActualEvent(1, "start", "C001", 1, 5, 1),
            ActualEvent(2, "finish", "C001", 1, 5, 2),
        ], PLAN_ITEMS, 2)
    with pytest.raises(ActualsReplayError, match="严格晚于开工"):
        replay([
            ActualEvent(1, "start", "C001", 1, 5, 1),
            ActualEvent(2, "finish", "C001", 1, 3, 2),
        ], PLAN_ITEMS, 2)


def test_crane_overlap_error():
    with pytest.raises(ActualsReplayError, match="不得"):
        replay([
            ActualEvent(1, "start", "C001", 1, 0, 1),
            ActualEvent(2, "start", "C003", 1, 2, 2),
        ], PLAN_ITEMS, 2)


def test_adjacent_intervals_allowed():
    result = replay([
        ActualEvent(1, "start", "C001", 1, 0, 1),
        ActualEvent(2, "finish", "C001", 1, 5, 2),
        ActualEvent(3, "start", "C003", 1, 5, 3),
    ], PLAN_ITEMS, 2)
    assert result["boxes"]["C003"].status == "in_progress"


def test_parallel_on_different_cranes_allowed():
    result = replay([
        ActualEvent(1, "start", "C001", 1, 0, 1),
        ActualEvent(2, "start", "C003", 2, 0, 2),
    ], PLAN_ITEMS, 2)
    assert result["boxes"]["C001"].status == "in_progress"
    assert result["boxes"]["C003"].status == "in_progress"


def test_lower_tier_must_be_finished():
    with pytest.raises(ActualsReplayError, match="下层"):
        replay([
            ActualEvent(1, "start", "C001", 1, 0, 1),
            ActualEvent(2, "start", "C002", 2, 3, 2),
        ], PLAN_ITEMS, 2)


def test_upper_can_start_once_lower_finished():
    result = replay([
        ActualEvent(1, "start", "C001", 1, 0, 1),
        ActualEvent(2, "finish", "C001", 1, 5, 2),
        ActualEvent(3, "start", "C002", 1, 5, 3),
        ActualEvent(4, "finish", "C002", 1, 9, 4),
    ], PLAN_ITEMS, 2)
    assert result["boxes"]["C002"].status == "finished"
    assert result["unfinished"] == ["C003"]


def test_container_not_in_plan_error():
    with pytest.raises(ActualsReplayError, match="不在方案"):
        replay([ActualEvent(1, "start", "C999", 1, 0, 1)], PLAN_ITEMS, 2)


def test_crane_out_of_range_error():
    with pytest.raises(ActualsReplayError, match="岸桥"):
        replay([ActualEvent(1, "start", "C001", 3, 0, 1)], PLAN_ITEMS, 2)


def test_duplicate_finish_error():
    with pytest.raises(ActualsReplayError, match="重复完工"):
        replay([
            ActualEvent(1, "start", "C001", 1, 0, 1),
            ActualEvent(2, "finish", "C001", 1, 5, 2),
            ActualEvent(3, "finish", "C001", 1, 9, 3),
        ], PLAN_ITEMS, 2)


def test_late_finish_before_existing_start_restores_pending_rejection():
    # 已登记 start@10；晚到 finish@3 重排到最前 -> 无早于完工的开工 -> 拒绝
    with pytest.raises(ActualsReplayError, match="严格晚于开工"):
        replay([
            ActualEvent(1, "start", "C001", 1, 10, 1),
            ActualEvent(9, "finish", "C001", 1, 3, 2),
        ], PLAN_ITEMS, 2)
