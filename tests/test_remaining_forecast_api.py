"""岸桥停机剩余作业预测 API（POST /plans/{plan_id}/remaining-forecast）。

覆盖：
* 进行中/待开工/已完工三类箱的逐箱预计、停机避让、同桥不重叠、堆栈先后；
* 全船最晚完工最早 + 按箱号升序 (开工,岸桥) 字典序优选；
* 无未完箱、无可行安排（时间窗/在制作业撞停机）；
* 404（方案/实绩不存在）、409（未确认/已取代/版本过期）、
  422（实绩晚于截至分钟、箱项不覆盖/分类不符/方案外、时间窗倒置、
  停机起点不早于终点、停机桥号越界、停机桥号越界/在制作业撞停机）；
* 只读：预测不改原排程、实绩版本与冻结基线。
"""

from __future__ import annotations

import pytest


def _setup_confirmed_with_actuals(client, *, events, crane_count=2, durations=None):
    """建 2 栈×2 层共 4 箱（C001/C002 在 A 栈，C003/C004 在 B 栈；
    重箱在底），确认方案、生成排程并登记实绩，返回 plan_id。

    实际配载（由求解器决定，两栈底层分别为重箱）通过返回的 actuals 查询确认。
    """
    base = "/api"
    slots = {
        "slots": [
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
            {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "B02", "stack": "B", "tier": 2, "capacity": 100, "arm": 3.0},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -100000,
        "max_moment": 100000,
    }
    sr = client.post(f"{base}/slot-revisions", json=slots).json()["slots_revision"]
    manifest = {
        "containers": [
            {"no": "C001", "weight": 30, "hazmat": None, "discharge_order": 1},
            {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1},
            {"no": "C003", "weight": 30, "hazmat": None, "discharge_order": 1},
            {"no": "C004", "weight": 10, "hazmat": None, "discharge_order": 1},
        ]
    }
    mr = client.post(
        f"{base}/manifest-revisions", json=manifest
    ).json()["manifest_revision"]
    plan = client.post(f"{base}/plans/compute", json={}).json()
    pid = plan["plan_id"]
    loc = {a["container_no"]: (a["stack"], a["tier"]) for a in plan["assignments"]}
    client.post(
        f"{base}/plans/{pid}/confirm",
        json={"slots_revision": sr, "manifest_revision": mr},
    )
    durations = durations or {
        "C001": 9, "C002": 6, "C003": 10, "C004": 8,
    }
    schedule = {
        "crane_count": crane_count,
        "entries": [
            {"no": no, "duration": d, "release_minute": 0, "deadline_minute": 120}
            for no, d in durations.items()
        ],
    }
    r = client.post(f"{base}/plans/{pid}/schedules", json=schedule)
    assert r.status_code == 201, r.text
    if events:
        r = client.post(f"{base}/plans/{pid}/actual-events", json={"events": events})
        assert r.status_code in (200, 201), r.text
    return pid, loc


def _events_all_finished():
    """两栈底层完工、上层也完工的事件（C001/C002 为 A 栈，C003/C004 为 B 栈时）。"""
    return [
        {"seq": 1, "event_type": "start", "container_no": "C001",
         "crane_no": 1, "minute": 0},
        {"seq": 2, "event_type": "finish", "container_no": "C001",
         "crane_no": 1, "minute": 9},
        {"seq": 3, "event_type": "start", "container_no": "C002",
         "crane_no": 1, "minute": 9},
        {"seq": 4, "event_type": "finish", "container_no": "C002",
         "crane_no": 1, "minute": 15},
        {"seq": 5, "event_type": "start", "container_no": "C003",
         "crane_no": 2, "minute": 0},
        {"seq": 6, "event_type": "finish", "container_no": "C003",
         "crane_no": 2, "minute": 10},
        {"seq": 7, "event_type": "start", "container_no": "C004",
         "crane_no": 2, "minute": 10},
        {"seq": 8, "event_type": "finish", "container_no": "C004",
         "crane_no": 2, "minute": 18},
    ]


def _events_one_active_one_pending():
    """截至第 12 分钟：C001、C003 已完工；C002 在桥 1 进行中（10 开工）；C004 待开工。"""
    return [
        {"seq": 1, "event_type": "start", "container_no": "C001",
         "crane_no": 1, "minute": 0},
        {"seq": 2, "event_type": "finish", "container_no": "C001",
         "crane_no": 1, "minute": 9},
        {"seq": 3, "event_type": "start", "container_no": "C003",
         "crane_no": 2, "minute": 0},
        {"seq": 4, "event_type": "finish", "container_no": "C003",
         "crane_no": 2, "minute": 10},
        {"seq": 5, "event_type": "start", "container_no": "C002",
         "crane_no": 1, "minute": 10},
    ]


def _forecast_payload(**over):
    payload = {
        "expected_actual_version": 1,
        "cutoff_minute": 12,
        "in_progress": [
            {"container_no": "C002", "remaining_duration": 4},
        ],
        "pending": [
            {"container_no": "C004", "duration": 6,
             "release_minute": 0, "deadline_minute": 60},
        ],
        "crane_downtimes": [],
    }
    payload.update(over)
    return payload


def test_forecast_basic_active_and_pending(client):
    pid, loc = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    assert loc["C001"] == ("A", 1) and loc["C002"] == ("A", 2)
    assert loc["C003"] == ("B", 1) and loc["C004"] == ("B", 2)

    r = client.post(
        f"/api/plans/{pid}/remaining-forecast", json=_forecast_payload()
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["actual_version"] == 1
    assert data["revision_no"] == 1
    assert data["baseline_crane_count"] == 2
    by_no = {e["container_no"]: e for e in data["entries"]}
    # 进行中箱：保持桥 1，自 12 连续至 16
    assert by_no["C002"]["kind"] == "in_progress"
    assert (by_no["C002"]["start_minute"], by_no["C002"]["finish_minute"],
            by_no["C002"]["crane_no"]) == (12, 16, 1)
    # 待开工箱 C004（独立栈 B）：桥 2 自 12 起 -> 18
    assert by_no["C004"]["kind"] == "pending"
    assert (by_no["C004"]["start_minute"], by_no["C004"]["finish_minute"],
            by_no["C004"]["crane_no"]) == (12, 18, 2)
    assert data["latest_finish_minute"] == 18
    # 按箱号升序
    assert [e["container_no"] for e in data["entries"]] == ["C002", "C004"]


def test_forecast_crane_downtime_forces_wait_and_crane_change(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    # 桥 2 在 [12,20) 停机：C004 不能上桥 2；桥 1 上 C002 作业到 16，
    # C004 独立栈可在桥 1 于 16 开工 -> 22
    payload = _forecast_payload(
        crane_downtimes=[{"crane_no": 2, "start_minute": 12, "end_minute": 20}]
    )
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    by_no = {e["container_no"]: e for e in data["entries"]}
    assert (by_no["C004"]["start_minute"], by_no["C004"]["finish_minute"],
            by_no["C004"]["crane_no"]) == (16, 22, 1)
    assert data["latest_finish_minute"] == 22
    # 停机区间原样回显（按桥、起点排序）
    assert data["crane_downtimes"] == [
        {"crane_no": 2, "start_minute": 12, "end_minute": 20}
    ]


def test_forecast_job_fits_gap_between_active_and_downtime(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    # 桥 2 [15, 40) 停机；C004 dur=3 可在桥 2 的 [12,15) 缝隙完工（12..15）
    payload = _forecast_payload(
        pending=[{"container_no": "C004", "duration": 3,
                  "release_minute": 0, "deadline_minute": 60}],
        crane_downtimes=[{"crane_no": 2, "start_minute": 15, "end_minute": 40}],
    )
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 200, r.text
    by_no = {e["container_no"]: e for e in r.json()["entries"]}
    assert (by_no["C004"]["start_minute"], by_no["C004"]["finish_minute"],
            by_no["C004"]["crane_no"]) == (12, 15, 2)
    assert r.json()["latest_finish_minute"] == 16  # C002 在桥 1 到 16


def test_forecast_stack_precedence_waits_for_active_lower(client):
    # 构造同栈：底层 C001 完工，C002 在桥 1 进行中（12->16）；
    # 另一个独立栈 C003 完工、C004 待开工——这里改为同栈上层受 active 约束，
    # 即让待开工箱位于 active 上方：直接使用 A 栈三层不可（仅两层），
    # 因此验证 active->pending 同栈：把 active 设为 A 栈底层不现实（下层已完工才
    # 能开工上层），故 active 为下层、pending 为其上方需三层栈。
    base = "/api"
    slots = {
        "slots": [
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 1.0},
            {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 1.0},
            {"code": "A03", "stack": "A", "tier": 3, "capacity": 100, "arm": 1.0},
            {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 1.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    sr = client.post(f"{base}/slot-revisions", json=slots).json()["slots_revision"]
    manifest = {"containers": [
        {"no": "C1", "weight": 30, "hazmat": None, "discharge_order": 1},
        {"no": "C2", "weight": 20, "hazmat": None, "discharge_order": 1},
        {"no": "C3", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C4", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]}
    mr = client.post(f"{base}/manifest-revisions", json=manifest).json()[
        "manifest_revision"
    ]
    plan = client.post(f"{base}/plans/compute", json={}).json()
    pid = plan["plan_id"]
    loc = {a["container_no"]: a["stack"] + str(a["tier"])
           for a in plan["assignments"]}
    client.post(f"{base}/plans/{pid}/confirm",
                json={"slots_revision": sr, "manifest_revision": mr})
    sched = {"crane_count": 2, "entries": [
        {"no": f"C{i}", "duration": 6, "release_minute": 0, "deadline_minute": 120}
        for i in range(1, 5)
    ]}
    client.post(f"{base}/plans/{pid}/schedules", json=sched)
    # 找到 A 栈底层、中层、上层箱
    tier_no = {v: k for k, v in loc.items()}
    low = tier_no["A1"]
    mid = tier_no["A2"]
    top = tier_no["A3"]
    other = tier_no["B1"]
    events = [
        {"seq": 1, "event_type": "start", "container_no": low,
         "crane_no": 1, "minute": 0},
        {"seq": 2, "event_type": "finish", "container_no": low,
         "crane_no": 1, "minute": 6},
        {"seq": 3, "event_type": "start", "container_no": mid,
         "crane_no": 1, "minute": 6},
        {"seq": 4, "event_type": "start", "container_no": other,
         "crane_no": 2, "minute": 0},
        {"seq": 5, "event_type": "finish", "container_no": other,
         "crane_no": 2, "minute": 6},
    ]
    client.post(f"{base}/plans/{pid}/actual-events", json={"events": events})
    payload = {
        "expected_actual_version": 1,
        "cutoff_minute": 10,
        "in_progress": [{"container_no": mid, "remaining_duration": 4}],  # ->14
        "pending": [
            # 同栈上层：必须等中层 14 完工；时长 4
            {"container_no": top, "duration": 4,
             "release_minute": 0, "deadline_minute": 60},
        ],
        "crane_downtimes": [],
    }
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 200, r.text
    by_no = {e["container_no"]: e for e in r.json()["entries"]}
    assert by_no[top]["start_minute"] == 14  # 不早于中层完工
    assert by_no[top]["finish_minute"] == 18
    assert r.json()["latest_finish_minute"] == 18


def test_forecast_all_finished_empty_unfinished(client):
    pid, _ = _setup_confirmed_with_actuals(client, events=_events_all_finished())
    payload = _forecast_payload(
        cutoff_minute=18, in_progress=[], pending=[]
    )
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["entries"] == []
    # 无未完箱：最晚完工取已完工箱实际最晚完工（C004 第 18 分钟）
    assert data["latest_finish_minute"] == 18


def test_forecast_all_finished_on_empty_ship_plan(client):
    base = "/api"
    slots = {"slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 1.0}],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000}
    sr = client.post(f"{base}/slot-revisions", json=slots).json()["slots_revision"]
    mr = client.post(f"{base}/manifest-revisions", json={"containers": []}).json()[
        "manifest_revision"
    ]
    pid = client.post(f"{base}/plans/compute", json={}).json()["plan_id"]
    client.post(f"{base}/plans/{pid}/confirm",
                json={"slots_revision": sr, "manifest_revision": mr})
    # 空箱方案也可生成空排程；但无实绩 -> 404（“无实绩”）
    payload = {"expected_actual_version": 1, "cutoff_minute": 0,
               "in_progress": [], "pending": [], "crane_downtimes": []}
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 404


def test_forecast_infeasible_active_overlaps_downtime(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    # C002 固定在桥 1 [12,16)；桥 1 [14,18) 停机 -> 在制作业撞停机，不可行
    payload = _forecast_payload(
        crane_downtimes=[{"crane_no": 1, "start_minute": 14, "end_minute": 18}]
    )
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 422
    assert "无可行剩余作业安排" in r.json()["detail"]
    assert "C002" in r.json()["detail"]


def test_forecast_infeasible_pending_deadline(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    # 桥 2 [12,20) 停机，桥 1 被 C002 占到 16；C004 最早 16 开工、时长 6 ->22，
    # 但 deadline=18，无解
    payload = _forecast_payload(
        pending=[{"container_no": "C004", "duration": 6,
                  "release_minute": 0, "deadline_minute": 18}],
        crane_downtimes=[{"crane_no": 2, "start_minute": 12, "end_minute": 20}],
    )
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 422
    assert "无可行剩余作业安排" in r.json()["detail"]


def test_forecast_rejects_events_after_cutoff(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    # 补登一条第 15 分钟事件后，cutoff=12 即有晚于截至分钟的事件
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        {"seq": 9, "event_type": "finish", "container_no": "C002",
         "crane_no": 1, "minute": 15}
    ]})
    payload = _forecast_payload(
        expected_actual_version=1,
        in_progress=[],
        pending=[{"container_no": "C004", "duration": 6,
                  "release_minute": 0, "deadline_minute": 60}],
    )
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert "晚于截至分钟" in body["detail"]
    assert body["cutoff_minute"] == 12
    assert body["events_after_cutoff"][0]["seq"] == 9


def test_forecast_rejects_invalid_items(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    url = f"/api/plans/{pid}/remaining-forecast"

    # 缺少待开工箱
    r = client.post(url, json=_forecast_payload(pending=[]))
    assert r.status_code == 422 and "缺少待开工箱项" in r.json()["detail"]
    # 缺少进行中箱
    r = client.post(url, json=_forecast_payload(in_progress=[]))
    assert r.status_code == 422 and "缺少进行中箱项" in r.json()["detail"]
    # 分类颠倒：把进行中箱报成待开工箱
    r = client.post(url, json=_forecast_payload(
        in_progress=[],
        pending=[
            {"container_no": "C002", "duration": 4,
             "release_minute": 0, "deadline_minute": 60},
            {"container_no": "C004", "duration": 6,
             "release_minute": 0, "deadline_minute": 60},
        ],
    ))
    assert r.status_code == 422 and "缺少进行中箱项" in r.json()["detail"]
    # 方案外箱
    r = client.post(url, json=_forecast_payload(
        pending=[{"container_no": "C999", "duration": 6,
                  "release_minute": 0, "deadline_minute": 60}],
    ))
    assert r.status_code == 422 and "C999" in r.json()["detail"]
    # 同一箱同时出现在两组（schema 级 422）
    r = client.post(url, json=_forecast_payload(
        in_progress=[{"container_no": "C002", "remaining_duration": 4}],
        pending=[
            {"container_no": "C002", "duration": 4,
             "release_minute": 0, "deadline_minute": 60},
            {"container_no": "C004", "duration": 6,
             "release_minute": 0, "deadline_minute": 60},
        ],
    ))
    assert r.status_code == 422
    assert "同时出现" in str(r.json())


def test_forecast_schema_level_422(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    url = f"/api/plans/{pid}/remaining-forecast"

    # 时间窗倒置
    r = client.post(url, json=_forecast_payload(
        pending=[{"container_no": "C004", "duration": 6,
                  "release_minute": 40, "deadline_minute": 30}],
    ))
    assert r.status_code == 422
    # 停机起点不早于终点（相等）
    r = client.post(url, json=_forecast_payload(
        crane_downtimes=[{"crane_no": 1, "start_minute": 20, "end_minute": 20}],
    ))
    assert r.status_code == 422
    # 停机起点晚于终点
    r = client.post(url, json=_forecast_payload(
        crane_downtimes=[{"crane_no": 1, "start_minute": 25, "end_minute": 20}],
    ))
    assert r.status_code == 422
    # 剩余时长非正
    r = client.post(url, json=_forecast_payload(
        in_progress=[{"container_no": "C002", "remaining_duration": 0}],
    ))
    assert r.status_code == 422
    # 负的截至分钟
    r = client.post(url, json=_forecast_payload(cutoff_minute=-1))
    assert r.status_code == 422
    # 期望版本非正
    r = client.post(url, json=_forecast_payload(expected_actual_version=0))
    assert r.status_code == 422


def test_forecast_rejects_downtime_crane_out_of_range(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    r = client.post(
        f"/api/plans/{pid}/remaining-forecast",
        json=_forecast_payload(
            crane_downtimes=[{"crane_no": 3, "start_minute": 20, "end_minute": 30}]
        ),
    )
    assert r.status_code == 422 and "超出冻结基线岸桥数量" in r.json()["detail"]


def test_forecast_version_conflict_and_no_actuals_and_superseded(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    url = f"/api/plans/{pid}/remaining-forecast"
    # 版本过期
    r = client.post(url, json=_forecast_payload(expected_actual_version=2))
    assert r.status_code == 409
    # 推进版本（更正一条事件）后再用旧版本
    client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 5, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 5, "event_type": "start", "container_no": "C002",
                  "crane_no": 1, "minute": 9},
    })
    r = client.post(url, json=_forecast_payload(expected_actual_version=1))
    assert r.status_code == 409
    r = client.post(url, json=_forecast_payload(expected_actual_version=2))
    assert r.status_code == 200

    # 方案不存在
    r = client.post(
        "/api/plans/does-not-exist/remaining-forecast", json=_forecast_payload()
    )
    assert r.status_code == 404


def test_forecast_no_actuals_404(client):
    # 已确认且有排程、但从未登记实绩
    base = "/api"
    slots = {"slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 1.0}],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000}
    sr = client.post(f"{base}/slot-revisions", json=slots).json()["slots_revision"]
    mr = client.post(f"{base}/manifest-revisions", json={"containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1}]}
    ).json()["manifest_revision"]
    pid = client.post(f"{base}/plans/compute", json={}).json()["plan_id"]
    client.post(f"{base}/plans/{pid}/confirm",
                json={"slots_revision": sr, "manifest_revision": mr})
    client.post(f"{base}/plans/{pid}/schedules", json={
        "crane_count": 1, "entries": [
            {"no": "C001", "duration": 5, "release_minute": 0,
             "deadline_minute": 60}]})
    r = client.post(f"{base}/plans/{pid}/remaining-forecast", json={
        "expected_actual_version": 1, "cutoff_minute": 0,
        "in_progress": [],
        "pending": [{"container_no": "C001", "duration": 5,
                     "release_minute": 0, "deadline_minute": 60}],
        "crane_downtimes": [],
    })
    assert r.status_code == 404
    assert "尚无装船实绩" in r.json()["detail"]


def test_forecast_draft_plan_rejected(client):
    # draft 方案无实绩头 -> 服务层先取实绩，得到 404；
    # 但 draft 方案若先确认再 amend 出 draft 链，旧 confirmed 仍有实绩。
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    draft = client.post(f"/api/plans/{pid}/amend", json={}).json()
    did = draft["plan_id"]
    # 新草稿无实绩
    r = client.post(
        f"/api/plans/{did}/remaining-forecast", json=_forecast_payload()
    )
    assert r.status_code == 404


def test_forecast_superseded_read_only(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    # 资料更新 -> amend -> confirm 新修订，旧修订 superseded
    base = "/api"
    slots = {
        "slots": [
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
            {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "B02", "stack": "B", "tier": 2, "capacity": 100, "arm": 3.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    sr2 = client.post(f"{base}/slot-revisions", json=slots).json()["slots_revision"]
    manifest = {"containers": [
        {"no": "C001", "weight": 30, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C003", "weight": 30, "hazmat": None, "discharge_order": 1},
        {"no": "C004", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]}
    mr2 = client.post(f"{base}/manifest-revisions", json=manifest).json()[
        "manifest_revision"
    ]
    did = client.post(f"/api/plans/{pid}/amend", json={
        "slots_revision": sr2, "manifest_revision": mr2}).json()["plan_id"]
    r = client.post(f"/api/plans/{did}/confirm",
                    json={"slots_revision": sr2, "manifest_revision": mr2})
    assert r.status_code == 200
    # 旧方案实绩头仍在，但方案已 superseded -> 409
    r = client.post(
        f"/api/plans/{pid}/remaining-forecast", json=_forecast_payload()
    )
    assert r.status_code == 409
    assert "只读" in r.json()["detail"]


def test_forecast_is_read_only(client):
    pid, _ = _setup_confirmed_with_actuals(
        client, events=_events_one_active_one_pending()
    )
    before = client.get(f"/api/plans/{pid}/actuals").json()
    sched_before = client.get(f"/api/plans/{pid}/schedules").json()
    for _ in range(2):
        r = client.post(
            f"/api/plans/{pid}/remaining-forecast",
            json=_forecast_payload(crane_downtimes=[
                {"crane_no": 2, "start_minute": 12, "end_minute": 20}]),
        )
        assert r.status_code == 200
    after = client.get(f"/api/plans/{pid}/actuals").json()
    sched_after = client.get(f"/api/plans/{pid}/schedules").json()
    assert after["version"] == before["version"] == 1
    assert after["unfinished_containers"] == before["unfinished_containers"]
    assert after["events"] == before["events"]
    assert sched_after["makespan"] == sched_before["makespan"]
    assert sched_after["entries"] == sched_before["entries"]
    # 冻结基线不变
    assert after["baseline"] == before["baseline"]


@pytest.mark.parametrize("cutoff", [12, 15, 20])
def test_forecast_events_exactly_at_cutoff_allowed(client, cutoff):
    # 恰在截至分钟的事件允许（“不得晚于”含等于）
    pid, _ = _setup_confirmed_with_actuals(client, events=[
        {"seq": 1, "event_type": "start", "container_no": "C001",
         "crane_no": 1, "minute": 0},
        {"seq": 2, "event_type": "finish", "container_no": "C001",
         "crane_no": 1, "minute": 9},
        {"seq": 3, "event_type": "start", "container_no": "C003",
         "crane_no": 2, "minute": 0},
        {"seq": 4, "event_type": "finish", "container_no": "C003",
         "crane_no": 2, "minute": 10},
        {"seq": 5, "event_type": "start", "container_no": "C002",
         "crane_no": 1, "minute": 10},
        {"seq": 6, "event_type": "finish", "container_no": "C002",
         "crane_no": 1, "minute": cutoff},
    ])
    payload = {
        "expected_actual_version": 1,
        "cutoff_minute": cutoff,
        "in_progress": [],
        "pending": [{"container_no": "C004", "duration": 6,
                     "release_minute": 0, "deadline_minute": 60}],
        "crane_downtimes": [],
    }
    r = client.post(f"/api/plans/{pid}/remaining-forecast", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["latest_finish_minute"] == cutoff + 6
