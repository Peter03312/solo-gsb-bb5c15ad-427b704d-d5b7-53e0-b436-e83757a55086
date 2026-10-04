"""装船实绩事件重放引擎。

事件可晚到：登记时不按到达顺序生效，而是把已确认事件与新事件合并后，按
**实际分钟**重放，同一分钟内**先完工、后开工**（同一分钟两者分钟相等，
先放行完工再放行开工，允许同桥首尾相接）；分钟与类型都相同时按到达先后
（自增 id）打破平局。

开工合法性（按重放时刻）：

* 该箱尚无开工记录（不允许重复开工）；
* 所用岸桥空闲（桥上一作业区间已结束，区间 ``[开工, 完工)`` 不得重叠）；
* 同堆栈自底向上位于其下的箱均已完工。

完工合法性：

* 该箱已开工且尚未完工；
* 与该箱开工时使用同一台岸桥；
* 完工分钟严格晚于开工分钟（实际分钟为非负整数，不接受零分钟作业）。

任一事件使整条链非法，:func:`replay` 抛出 :class:`ActualsReplayError`，
由服务层整次拒绝、记录保持不变。
"""

from __future__ import annotations

from dataclasses import dataclass


class ActualsReplayError(Exception):
    """重放链不合法；message 为中文拒绝原因。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ActualEvent:
    seq: int
    event_type: str  # 'start' | 'finish'
    container_no: str
    crane_no: int
    minute: int
    arrival_id: int = 0  # 落库后的自增 id；未落库的新事件按提交顺序补排


@dataclass
class BoxState:
    container_no: str
    stack: str
    tier: int
    status: str = "pending"  # pending | in_progress | finished
    start_minute: int | None = None
    start_seq: int | None = None
    finish_minute: int | None = None
    finish_seq: int | None = None
    crane_no: int | None = None


def ordered_events(events) -> list[ActualEvent]:
    """重放排序：分钟升序；同分钟 finish(0) 先于 start(1)；再按到达 id。"""
    return sorted(
        events,
        key=lambda e: (e.minute, 0 if e.event_type == "finish" else 1, e.arrival_id),
    )


def replay(
    events,
    plan_items,
    crane_count: int,
) -> dict:
    """对一条方案的全部事件（含待登记新事件）执行重放校验。

    :param events: :class:`ActualEvent` 可迭代对象（新旧事件混合）。
    :param plan_items: ``(container_no, stack, tier)`` 可迭代对象，
        来自方案修订本身，用于“箱在方案中”与同堆栈下层关系。
    :param crane_count: 冻结基线排程的岸桥数量（crane_no 越界拒绝）。
    :return: ``boxes``（箱号 -> :class:`BoxState`）、``order``（重放顺序流水号）
        与 ``unfinished``（已开工未完工箱号，按箱号升序）。
    """
    boxes: dict[str, BoxState] = {}
    lower_than: dict[str, list[str]] = {}
    by_stack: dict[str, list[tuple[int, str]]] = {}
    for no, stack, tier in plan_items:
        if no in boxes:
            raise ActualsReplayError(f"方案中箱号 {no} 不唯一，数据不一致")
        boxes[no] = BoxState(container_no=no, stack=stack, tier=tier)
        by_stack.setdefault(stack, []).append((tier, no))
    for stack, members in by_stack.items():
        members.sort()
        for pos, (_tier, no) in enumerate(members):
            lower_than[no] = [m[1] for m in members[:pos]]

    # 桥状态：当前在桥上作业的箱号（None=空闲）
    crane_active: dict[int, str | None] = {k: None for k in range(1, crane_count + 1)}

    order: list[int] = []
    for ev in ordered_events(list(events)):
        if ev.crane_no < 1 or ev.crane_no > crane_count:
            raise ActualsReplayError(
                f"事件流水号 {ev.seq}：岸桥编号 {ev.crane_no} 超出冻结排程岸桥"
                f"数量 {crane_count}（合法编号 1..{crane_count}）"
            )
        box = boxes.get(ev.container_no)
        if box is None:
            raise ActualsReplayError(
                f"事件流水号 {ev.seq}：箱 {ev.container_no} 不在方案中，拒绝登记"
            )

        if ev.event_type == "start":
            if box.status != "pending":
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：箱 {ev.container_no} 已有开工记录"
                    f"（流水号 {box.start_seq}，第 {box.start_minute} 分钟），"
                    "不得重复开工"
                )
            if crane_active[ev.crane_no] is not None:
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：第 {ev.minute} 分钟岸桥 {ev.crane_no} "
                    f"正在为箱 {crane_active[ev.crane_no]} 作业，同桥作业区间不得"
                    "重叠（箱 " + ev.container_no + " 开工被拒绝）"
                )
            blocking = [
                lb
                for lb in lower_than[box.container_no]
                if boxes[lb].status != "finished"
            ]
            if blocking:
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：箱 {ev.container_no} 开工前，同堆栈下层箱 "
                    f"{', '.join(sorted(blocking))} 尚未全部完工"
                )
            box.status = "in_progress"
            box.start_minute = ev.minute
            box.start_seq = ev.seq
            box.crane_no = ev.crane_no
            crane_active[ev.crane_no] = box.container_no

        elif ev.event_type == "finish":
            if box.status == "pending":
                # 按分钟重排后仍无开工：未开工，或晚到完工分钟不晚于开工分钟
                # （同分钟完工先于开工重放；更早分钟的晚到完工同样排到开工之前）
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：箱 {ev.container_no} 在第 {ev.minute} "
                    "分钟之前尚无晚于该分钟的开工记录，完工必须严格晚于开工"
                )
            if box.status == "finished":
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：箱 {ev.container_no} 已完工"
                    f"（流水号 {box.finish_seq}，第 {box.finish_minute} 分钟），"
                    "不得重复完工"
                )
            if box.crane_no != ev.crane_no:
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：箱 {ev.container_no} 开工岸桥为 "
                    f"{box.crane_no}，完工岸桥 {ev.crane_no} 与其不一致"
                )
            if ev.minute <= box.start_minute:
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：箱 {ev.container_no} 完工分钟 {ev.minute} "
                    f"必须严格晚于开工分钟 {box.start_minute}"
                )
            if crane_active[ev.crane_no] != box.container_no:
                # 理论上不应发生（区间重叠已在开工侧拦截），防御性兜底
                raise ActualsReplayError(
                    f"事件流水号 {ev.seq}：岸桥 {ev.crane_no} 当前作业箱与 "
                    f"{ev.container_no} 不一致，重放链失效"
                )
            box.status = "finished"
            box.finish_minute = ev.minute
            box.finish_seq = ev.seq
            crane_active[ev.crane_no] = None
        else:  # pragma: no cover - 入库 CHECK 已限制
            raise ActualsReplayError(f"未知事件类型 {ev.event_type!r}")

        order.append(ev.seq)

    unfinished = sorted(
        no for no, b in boxes.items() if b.status in ("in_progress", "pending")
    )
    return {"boxes": boxes, "order": order, "unfinished": unfinished}
