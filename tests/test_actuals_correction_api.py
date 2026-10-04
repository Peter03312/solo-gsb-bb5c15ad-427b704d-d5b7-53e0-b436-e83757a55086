"""装船实绩更正（只改分钟或岸桥）端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app


# 同堆栈两层：C001 底（重、晚卸）、C002 顶（轻、早卸）
STACK_SLOTS = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}
STACK_MANIFEST = {
    "containers": [
        {"no": "C001", "weight": 30, "hazmat": None, "discharge_order": 2},
        {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}

# 两个互相独立的单箱堆栈
TWIN_SLOTS = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}
TWIN_MANIFEST = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C003", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}

# 另一套槽位编号（已确认槽位全局唯一，同一用例内并存两条方案须避开 A01/A02）
STACK2_SLOTS = {
    "slots": [
        {"code": "D01", "stack": "D", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "D02", "stack": "D", "tier": 2, "capacity": 100, "arm": 3.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}
STACK2_MANIFEST = STACK_MANIFEST


def _confirm(client, slots, manifest):
    sr = client.post("/api/slot-revisions", json=slots).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=manifest) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 200, r.text
    return plan


def _schedule(client, plan_id, crane_count=2, d1=10, d2=8, deadline=100,
              nos=("C001", "C002")):
    durations = dict(zip(nos, (d1, d2)))
    body = {
        "crane_count": crane_count,
        "entries": [
            {"no": no, "duration": durations[no], "release_minute": 0,
             "deadline_minute": deadline}
            for no in nos
        ],
    }
    r = client.post(f"/api/plans/{plan_id}/schedules", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _stack_plan_with_events(client):
    """确认方案+排程+登记完整合法链：C001[0,10)桥1、C002[10,18)桥1。"""
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
        _ev(4, "finish", "C002", 1, 18),
    ]})
    assert r.status_code == 201, r.text
    return pid


def _ev(seq, etype, cno, crane, minute):
    return {"seq": seq, "event_type": etype, "container_no": cno,
            "crane_no": crane, "minute": minute}


def _corr(seq, version, request_id, event):
    return {"seq": seq, "expected_version": version,
            "request_id": request_id, "event": event}


# ---------- 成功更正：版本递增、原值留档、更正历史 ----------


def test_correct_minute_increments_version_and_records_history(client):
    pid = _stack_plan_with_events(client)
    # 把 C002 开工从第 10 分钟更正为第 11 分钟（仍合法）
    body = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 2
    e3 = next(e for e in data["events"] if e["seq"] == 3)
    assert e3["minute"] == 11 and e3["orig_minute"] == 10
    assert e3["crane_no"] == 1 and e3["orig_crane_no"] == 1
    assert e3["corrected"] is True
    # 未被更正的事件 corrected=false、原值=现值
    e1 = next(e for e in data["events"] if e["seq"] == 1)
    assert e1["corrected"] is False and e1["orig_minute"] == e1["minute"] == 0
    # 偏差随更正重算
    c2 = next(c for c in data["containers"] if c["container_no"] == "C002")
    assert c2["actual_start_minute"] == 11
    assert c2["start_delta_minutes"] == 1  # 基线开工 10
    # 更正历史
    assert len(data["corrections"]) == 1
    h = data["corrections"][0]
    assert (h["request_id"], h["seq"], h["from_version"], h["to_version"],
            h["old_minute"], h["new_minute"], h["old_crane_no"],
            h["new_crane_no"]) == ("R-001", 3, 1, 2, 10, 11, 1, 1)
    assert h["expected_version"] == 1 and h["event_type"] == "start"


def test_correct_crane_number(client):
    # 独立双箱堆栈：C001 全程桥1[0,10)；把已开工未完工的 C003 开工由桥2改到桥1
    # （第10分钟桥1恰空闲，可首尾相接）
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 10),
    ]})
    body = _corr(3, 1, "RC-1", _ev(3, "start", "C003", 1, 10))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    e3 = next(e for e in data["events"] if e["seq"] == 3)
    assert e3["crane_no"] == 1 and e3["orig_crane_no"] == 2  # 现行改桥1、原始留档桥2
    c3 = next(c for c in data["containers"] if c["container_no"] == "C003")
    assert c3["actual_crane_no"] == 1 and c3["crane_changed"] is True


def test_correction_must_keep_start_finish_crane_consistent(client):
    # 已完成的 C002 作业：开工、完工都在桥1。只改开工桥或只改完工桥都会使
    # “完工岸桥=开工岸桥”失效 -> 整次更正拒绝，版本与历史不变。
    plan = _confirm(client, STACK2_SLOTS, STACK2_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
        _ev(4, "finish", "C002", 1, 18),
    ]})
    only_start = _corr(3, 1, "RC-2", _ev(3, "start", "C002", 2, 10))
    rb = client.post(f"/api/plans/{pid}/actual-events/correction", json=only_start)
    assert rb.status_code == 422 and "不一致" in rb.json()["detail"]
    only_finish = _corr(4, 1, "RC-3", _ev(4, "finish", "C002", 2, 18))
    rf = client.post(f"/api/plans/{pid}/actual-events/correction", json=only_finish)
    assert rf.status_code == 422 and "不一致" in rf.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 1 and g["corrections"] == []
    assert next(e for e in g["events"] if e["seq"] == 3)["crane_no"] == 1


def test_correct_start_crane_before_finish_then_register_finish(client):
    # C002 已开工未完工：开工桥可单独更正到桥2，随后以桥2登记完工即合法。
    plan = _confirm(client, STACK2_SLOTS, STACK2_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
    ]})
    rs = client.post(f"/api/plans/{pid}/actual-events/correction",
                     json=_corr(3, 1, "RC-4", _ev(3, "start", "C002", 2, 10)))
    assert rs.status_code == 201, rs.text and rs.json()["version"] == 2
    rf = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(4, "finish", "C002", 2, 18)]})
    assert rf.status_code == 201, rf.text
    assert rf.json()["unfinished_containers"] == []
    c2 = next(c for c in rf.json()["containers"] if c["container_no"] == "C002")
    assert c2["actual_crane_no"] == 2 and c2["crane_changed"] is True


def test_correction_response_equals_following_get(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 201
    g = client.get(f"/api/plans/{pid}/actuals")
    assert g.status_code == 200
    assert g.json() == r.json()


def test_correction_changes_replay_order(client):
    """更正到更晚分钟后，按分钟重放顺序相应变化。"""
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 5),
        _ev(2, "start", "C003", 2, 3),
    ]})
    g0 = client.get(f"/api/plans/{pid}/actuals").json()
    assert g0["replay_order"] == [2, 1]  # 分钟 3 先于 5
    # 把 seq2 由第 3 分钟更正到第 8 分钟 -> 重放顺序变为 [1, 2]
    body = _corr(2, 1, "RO-1", _ev(2, "start", "C003", 2, 8))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["replay_order"] == [1, 2]


# ---------- 幂等重试 ----------


def test_same_request_id_same_content_retry_returns_first_result(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    r1 = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r1.status_code == 201
    r2 = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert len(g["corrections"]) == 1 and g["version"] == 2


def test_retry_after_version_advanced_returns_first_snapshot(client):
    pid = _stack_plan_with_events(client)
    first = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    r1 = client.post(f"/api/plans/{pid}/actual-events/correction", json=first)
    assert r1.status_code == 201 and r1.json()["version"] == 2
    # 另一请求把版本推进到 3
    second = _corr(3, 2, "R-002", _ev(3, "start", "C002", 1, 12))
    r2 = client.post(f"/api/plans/{pid}/actual-events/correction", json=second)
    assert r2.status_code == 201 and r2.json()["version"] == 3
    # 用旧请求号 R-001、旧期望版本重试：仍返回首次更正快照（版本 2），不重复生效
    r3 = client.post(f"/api/plans/{pid}/actual-events/correction", json=first)
    assert r3.status_code == 200
    assert r3.json() == r1.json()
    assert r3.json()["version"] == 2
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 3 and len(g["corrections"]) == 2
    assert next(e for e in g["events"] if e["seq"] == 3)["minute"] == 12


def test_different_request_id_same_change_is_empty_correction(client):
    pid = _stack_plan_with_events(client)
    first = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    assert client.post(f"/api/plans/{pid}/actual-events/correction",
                       json=first).status_code == 201
    # 另一请求号提交完全相同的替代内容 -> 与现行值相同，空更正 422
    dup = _corr(3, 2, "R-OTHER", _ev(3, "start", "C002", 1, 11))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=dup)
    assert r.status_code == 422 and "相同" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert len(g["corrections"]) == 1


# ---------- 冲突：版本过期 / 同号异内容 ----------


def test_expected_version_stale_conflict_unchanged(client):
    pid = _stack_plan_with_events(client)
    first = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    client.post(f"/api/plans/{pid}/actual-events/correction", json=first)
    # 仍以版本 1 提交新请求 -> 409，实绩不变
    stale = _corr(4, 1, "R-002", _ev(4, "finish", "C002", 1, 19))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=stale)
    assert r.status_code == 409 and "过期" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 2 and len(g["corrections"]) == 1


def test_same_request_id_different_content_conflict(client):
    pid = _stack_plan_with_events(client)
    first = _corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11))
    client.post(f"/api/plans/{pid}/actual-events/correction", json=first)
    # 同请求号、不同替代分钟（即便期望版本正确）-> 409
    other = _corr(3, 2, "R-001", _ev(3, "start", "C002", 1, 12))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=other)
    assert r.status_code == 409 and "不同内容" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert next(e for e in g["events"] if e["seq"] == 3)["minute"] == 11


# ---------- 更正内容限制：只改分钟或岸桥 ----------


def test_cannot_change_event_type(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "T-1", _ev(3, "finish", "C002", 1, 12))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "事件类型" in r.json()["detail"]
    assert client.get(f"/api/plans/{pid}/actuals").json()["version"] == 1


def test_cannot_change_container(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "T-2", _ev(3, "start", "C001", 1, 12))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "箱号" in r.json()["detail"]


def test_container_not_in_plan_rejected_422(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "T-3", _ev(3, "start", "C999", 1, 12))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "不在方案" in r.json()["detail"]


def test_nested_seq_must_match_target_seq(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "T-4", _ev(9, "start", "C002", 1, 12))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422  # Pydantic 模型校验


def test_crane_out_of_baseline_range_rejected(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "T-5", _ev(3, "start", "C002", 3, 10))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "岸桥" in r.json()["detail"]


def test_noop_correction_rejected(client):
    pid = _stack_plan_with_events(client)
    body = _corr(3, 1, "T-6", _ev(3, "start", "C002", 1, 10))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "完全相同" in r.json()["detail"]
    assert client.get(f"/api/plans/{pid}/actuals").json()["version"] == 1


# ---------- 重放非法：整次拒绝、版本/历史/实绩不变 ----------


def test_illegal_replay_rejects_whole_correction_unchanged(client):
    pid = _stack_plan_with_events(client)
    # 顶箱 C002 开工改到第 5 分钟：下层 C001 第 10 分钟才完工 -> 链非法
    body = _corr(3, 1, "X-1", _ev(3, "start", "C002", 2, 5))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "下层" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    # 版本、更正历史、现行值全部不变
    assert g["version"] == 1
    assert g["corrections"] == []
    assert next(e for e in g["events"] if e["seq"] == 3)["minute"] == 10


def test_correction_to_crane_overlap_rejected(client):
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=1, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 1, 10),
    ]})
    # 单桥：C003 开工从 10 提前到 5，与 C001 [0,10) 撞桥 -> 非法
    body = _corr(3, 1, "X-2", _ev(3, "start", "C003", 1, 5))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 422 and "不得" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 1 and g["corrections"] == []


# ---------- 更正后与原登记接口的交互 ----------


def test_register_after_correction_uses_current_values_and_orig_idempotency(client):
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    # 仅登记到 C002 开工（未完工），便于后面追加全新流水号
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
    ]})
    # 把 seq3 开工由 10 更正为 11
    client.post(f"/api/plans/{pid}/actual-events/correction",
                json=_corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11)))
    # 原登记接口用原始内容（第 10 分钟）重试 -> 仍按首次登记识别为重试（200），不改回
    r1 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(3, "start", "C002", 1, 10)]})
    assert r1.status_code == 200
    assert next(e for e in r1.json()["events"] if e["seq"] == 3)["minute"] == 11
    # 原登记接口用现行（更正后）内容提交 -> 与首次登记内容不同 -> 冲突 409
    r2 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(3, "start", "C002", 1, 11)]})
    assert r2.status_code == 409 and "冲突" in r2.json()["detail"]
    # 混合批次：seq3 用原始内容（幂等跳过）+ 全新流水号（C002 完工）-> 仅追加新事件
    r3 = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(3, "start", "C002", 1, 10),
        _ev(5, "finish", "C002", 1, 19),
    ]})
    assert r3.status_code == 201, r3.text
    assert [e["seq"] for e in r3.json()["events"]] == [1, 2, 3, 5]
    assert r3.json()["unfinished_containers"] == []


# ---------- 前置条件 / 未找到 / 只读 ----------


def test_original_event_not_found_404(client):
    pid = _stack_plan_with_events(client)
    body = _corr(99, 1, "N-1", _ev(99, "start", "C002", 1, 11))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 404 and "未找到" in r.json()["detail"]


def test_no_actuals_head_404(client):
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    body = _corr(1, 1, "N-2", _ev(1, "start", "C001", 1, 1))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 404 and "未找到" in r.json()["detail"]


def test_plan_not_found_404(client):
    body = _corr(1, 1, "N-3", _ev(1, "start", "C001", 1, 1))
    r = client.post("/api/plans/nope/actual-events/correction", json=body)
    assert r.status_code == 404


def test_draft_plan_correction_409(client):
    client.post("/api/slot-revisions", json=STACK_SLOTS)
    client.post("/api/manifest-revisions", json=STACK_MANIFEST)
    plan = client.post("/api/plans/compute", json={}).json()
    body = _corr(1, 1, "N-4", _ev(1, "start", "C001", 1, 1))
    r = client.post(f"/api/plans/{plan['plan_id']}/actual-events/correction", json=body)
    assert r.status_code == 409 and "确认" in r.json()["detail"]


def test_superseded_plan_correction_read_only(client):
    pid = _stack_plan_with_events(client)
    # 新箱清单修订 -> 修订并确认 p2，p1 变 superseded
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200
    body = _corr(3, 1, "RO-9", _ev(3, "start", "C002", 1, 11))
    r = client.post(f"/api/plans/{pid}/actual-events/correction", json=body)
    assert r.status_code == 409 and "只读" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["read_only"] is True and g["version"] == 1


# ---------- 查询呈现与入参校验 ----------


def test_get_actuals_presents_version_history_and_originals(client):
    pid = _stack_plan_with_events(client)
    assert client.get(f"/api/plans/{pid}/actuals").json()["version"] == 1
    client.post(f"/api/plans/{pid}/actual-events/correction",
                json=_corr(3, 1, "R-001", _ev(3, "start", "C002", 1, 11)))
    data = client.get(f"/api/plans/{pid}/actuals").json()
    assert data["version"] == 2
    assert [h["request_id"] for h in data["corrections"]] == ["R-001"]
    assert all({"orig_minute", "orig_crane_no", "corrected"} <= set(e)
               for e in data["events"])


def test_correction_payload_validation(client):
    pid = _stack_plan_with_events(client)
    base = f"/api/plans/{pid}/actual-events/correction"
    # 流水号非正
    assert client.post(base, json=_corr(0, 1, "V", _ev(0, "start", "C002", 1, 11))
                       ).status_code == 422
    # 期望版本非正
    assert client.post(base, json=_corr(3, 0, "V", _ev(3, "start", "C002", 1, 11))
                       ).status_code == 422
    # 空请求号
    bad = _corr(3, 1, "  ", _ev(3, "start", "C002", 1, 11))
    assert client.post(base, json=bad).status_code == 422
    # 替代分钟为负
    assert client.post(base, json=_corr(3, 1, "V", _ev(3, "start", "C002", 1, -1))
                       ).status_code == 422
    # 替代桥号非正
    assert client.post(base, json=_corr(3, 1, "V", _ev(3, "start", "C002", 0, 11))
                       ).status_code == 422
    # 事件类型非法
    assert client.post(base, json=_corr(3, 1, "V", _ev(3, "begin", "C002", 1, 11))
                       ).status_code == 422
    # 缺少 event 字段
    assert client.post(base, json={"seq": 3, "expected_version": 1,
                                   "request_id": "V"}).status_code == 422


# ---------- 并发：更正写锁串行化（版本不冲突） ----------


def test_concurrent_corrections_serialize(client):
    """两个线程并发更正不同分钟：一个生效，另一个因版本过期被拒（无脏覆盖）。"""
    pid = _stack_plan_with_events(client)

    results = []

    def correct(minute, key):
        local = TestClient(app)
        # 两者都基于版本 1；写锁串行后其一成功，其二版本过期
        body = _corr(3, 1, f"CC-{key}", _ev(3, "start", "C002", 1, minute))
        r = local.post(f"/api/plans/{pid}/actual-events/correction", json=body)
        results.append(r.status_code)

    t1 = __import__("threading").Thread(target=correct, args=(11, "a"))
    t2 = __import__("threading").Thread(target=correct, args=(12, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [201, 409]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert g["version"] == 2 and len(g["corrections"]) == 1
