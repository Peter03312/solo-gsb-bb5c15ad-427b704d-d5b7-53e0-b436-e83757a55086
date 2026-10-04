"""装船实绩登记（事件重放）端到端测试。"""

import threading

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


def _stack_plan(client):
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    sch = _schedule(client, plan["plan_id"], crane_count=2)
    # 基线排程：C001[0,10) 桥1 -> C002[10,18) 桥1（堆栈先后）
    assert {e["container_no"]: (e["start_minute"], e["finish_minute"], e["crane_no"])
            for e in sch["entries"]} == {
        "C001": (0, 10, 1), "C002": (10, 18, 1)}
    return plan


def _ev(seq, etype, cno, crane, minute):
    return {"seq": seq, "event_type": etype, "container_no": cno,
            "crane_no": crane, "minute": minute}


# ---------- 基线冻结与查询 ----------


def test_first_registration_freezes_current_schedule_as_baseline(client):
    plan = _stack_plan(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 1, 0)]})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["plan_id"] == plan["plan_id"]
    assert data["revision_no"] == 1
    assert data["plan_status"] == "confirmed"
    assert data["read_only"] is False
    assert data["baseline_crane_count"] == 2
    assert data["baseline_frozen_at"] is not None
    base = {b["container_no"]: b for b in data["baseline"]}
    assert (base["C001"]["start_minute"], base["C001"]["finish_minute"],
            base["C001"]["crane_no"]) == (0, 10, 1)
    assert (base["C002"]["start_minute"], base["C002"]["finish_minute"],
            base["C002"]["crane_no"]) == (10, 18, 1)
    assert data["replay_order"] == [1]


def test_reschedule_does_not_rewrite_baseline_or_actuals(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})

    # 用 1 台岸桥重排 -> 原子替换旧排程（新排程 C002 仍在桥1）
    sch2 = _schedule(client, pid, crane_count=1)
    assert sch2["crane_count"] == 1

    # 实绩基线仍是首次登记时的 2 台岸桥版本；桥2 仍合法
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 10),
    ]})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["baseline_crane_count"] == 2
    c2 = next(c for c in data["containers"] if c["container_no"] == "C002")
    assert c2["actual_crane_no"] == 2 and c2["crane_changed"] is True

    g = client.get(f"/api/plans/{pid}/actuals")
    assert g.status_code == 200
    assert {b["container_no"]: (b["start_minute"], b["finish_minute"])
            for b in g.json()["baseline"]} == {"C001": (0, 10), "C002": (10, 18)}


# ---------- 重放：状态、偏差、未完箱 ----------


def test_full_chain_status_deltas_and_unfinished(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 11),   # 比基线晚 1 分钟
        _ev(4, "finish", "C002", 1, 19),  # 比基线晚 1 分钟
    ]})
    assert r.status_code == 201, r.text
    data = r.json()
    boxes = {c["container_no"]: c for c in data["containers"]}
    assert boxes["C001"]["status"] == "finished"
    assert (boxes["C001"]["actual_start_minute"],
            boxes["C001"]["actual_finish_minute"]) == (0, 10)
    assert boxes["C001"]["start_delta_minutes"] == 0
    assert boxes["C001"]["finish_delta_minutes"] == 0
    assert boxes["C001"]["crane_changed"] is False
    assert boxes["C002"]["status"] == "finished"
    assert boxes["C002"]["start_delta_minutes"] == 1
    assert boxes["C002"]["finish_delta_minutes"] == 1
    assert boxes["C002"]["crane_changed"] is False
    assert data["unfinished_containers"] == []
    assert data["replay_order"] == [1, 2, 3, 4]


def test_unfinished_covers_pending_and_in_progress(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 1, 3)]})
    assert r.status_code == 201
    data = r.json()
    boxes = {c["container_no"]: c for c in data["containers"]}
    assert boxes["C001"]["status"] == "in_progress"
    assert boxes["C001"]["actual_finish_minute"] is None
    assert boxes["C001"]["start_delta_minutes"] == 3
    assert boxes["C001"]["finish_delta_minutes"] is None
    assert boxes["C002"]["status"] == "pending"
    assert data["unfinished_containers"] == ["C001", "C002"]


def test_same_minute_finish_replayed_before_start_with_late_arrival(client):
    """事件晚到：开工先登记，同分钟完工后到；重放时完工先于开工，同桥可首尾相接。"""
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    sch = _schedule(client, pid, crane_count=1, nos=("C001", "C003"), d1=10, d2=10)
    assert {e["crane_no"] for e in sch["entries"]} == {1}

    # 先登记 C001 开工@0
    r1 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(1, "start", "C001", 1, 0)]})
    assert r1.status_code == 201
    # 晚到的一批：C003 开工@10（流水号小，先到语义）与 C001 完工@10（流水号大、晚到）
    # 按到达顺序先处理开工行会撞桥；按分钟重放（同分钟完工先行）应通过
    r2 = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(2, "start", "C003", 1, 10),
        _ev(3, "finish", "C001", 1, 10),
    ]})
    assert r2.status_code == 201, r2.text
    data = r2.json()
    # 重放顺序：分钟0 -> 分钟10 的 finish(seq3) -> 分钟10 的 start(seq2)
    assert data["replay_order"] == [1, 3, 2]
    boxes = {c["container_no"]: c for c in data["containers"]}
    assert boxes["C001"]["status"] == "finished"
    assert boxes["C003"]["status"] == "in_progress"
    assert data["unfinished_containers"] == ["C003"]


# ---------- 开工/完工合法性 ----------


def test_duplicate_start_rejected(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "start", "C001", 2, 5),
    ]})
    assert r.status_code == 422 and "重复开工" in r.json()["detail"]
    # 整次拒绝：无事件落库
    assert client.get(f"/api/plans/{pid}/actuals").status_code == 404


def test_finish_without_start_rejected(client):
    plan = _stack_plan(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/actual-events",
                    json={"events": [_ev(1, "finish", "C001", 1, 5)]})
    assert r.status_code == 422 and "严格晚于开工" in r.json()["detail"]


def test_finish_wrong_crane_rejected(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(2, "finish", "C001", 2, 5)]})
    assert r.status_code == 422 and "不一致" in r.json()["detail"]


def test_finish_must_be_strictly_later_than_start(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    # 先登记开工@5
    r0 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(1, "start", "C001", 1, 5)]})
    assert r0.status_code == 201
    # 晚到的完工声称第 3 分钟（早于开工）：重放按分钟重排后 finish@3 先于 start@5，
    # 此时箱尚未开工 -> 拒绝
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(2, "finish", "C001", 1, 3)]})
    assert r.status_code == 422 and "严格晚于开工" in r.json()["detail"]
    # 晚到的完工与开工同分钟（零分钟作业）：同分钟完工先重放，同样尚未开工
    r2 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(3, "finish", "C001", 1, 5)]})
    assert r2.status_code == 422 and "严格晚于开工" in r2.json()["detail"]
    # 合法晚到完工（第 8 分钟）成功
    ok = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(4, "finish", "C001", 1, 8)]})
    assert ok.status_code == 201
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert [e["seq"] for e in g["events"]] == [1, 4]


def test_same_crane_overlap_rejected(client):
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "start", "C003", 1, 0),
    ]})
    assert r.status_code == 422 and "不得" in r.json()["detail"]
    # 不同桥并行允许
    ok = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "start", "C003", 2, 0),
    ]})
    assert ok.status_code == 201, ok.text


def test_lower_tier_must_finish_before_upper_start(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    # 顶箱先开工（用空闲的桥2，避免撞桥，专门验证堆栈约束）：下层未完工
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "start", "C002", 2, 5),
    ]})
    assert r.status_code == 422 and "下层" in r.json()["detail"]
    # 底箱完工后顶箱可开工（同分钟完工先重放）
    ok = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 10),
    ]})
    assert ok.status_code == 201, ok.text


def test_crane_no_out_of_baseline_range_rejected(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 3, 0)]})
    assert r.status_code == 422 and "岸桥" in r.json()["detail"]


# ---------- 晚到 / 整批原子 / 幂等冲突 ----------


def test_late_event_breaking_chain_rejects_whole_batch_unchanged(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    r1 = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
        _ev(4, "finish", "C002", 1, 18),
    ]})
    assert r1.status_code == 201
    assert r1.json()["unfinished_containers"] == []

    # 晚到的重复完工使整条重放链非法 -> 整次拒绝
    r2 = client.post(f"/api/plans/{pid}/actual-events",
                     json={"events": [_ev(9, "finish", "C001", 1, 25)]})
    assert r2.status_code == 422 and "重复完工" in r2.json()["detail"]

    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert [e["seq"] for e in g["events"]] == [1, 2, 3, 4]
    assert g["unfinished_containers"] == []


def test_batch_is_atomic(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
    ]})
    # 一批中 s3 合法、s4 非法（顶箱完工早于其开工）-> 全部不入库
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(3, "start", "C002", 1, 10),
        _ev(4, "finish", "C002", 1, 5),
    ]})
    assert r.status_code == 422
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert [e["seq"] for e in g["events"]] == [1, 2]
    assert g["unfinished_containers"] == ["C002"]


def test_valid_late_event_is_accepted(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 1, 10),
    ]})
    # 晚到的 C002 完工
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(99, "finish", "C002", 1, 17)]})
    assert r.status_code == 201
    data = r.json()
    assert data["replay_order"] == [1, 2, 3, 99]
    assert data["unfinished_containers"] == []


def test_same_seq_same_content_idempotent_returns_original(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    body = {"events": [_ev(1, "start", "C001", 1, 0)]}
    r1 = client.post(f"/api/plans/{pid}/actual-events", json=body)
    assert r1.status_code == 201
    r2 = client.post(f"/api/plans/{pid}/actual-events", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert len(g["events"]) == 1

    # 混合批次：旧流水号同内容 + 新流水号 -> 仅新增新事件
    r3 = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
    ]})
    assert r3.status_code == 201
    assert [e["seq"] for e in r3.json()["events"]] == [1, 2]


def test_same_seq_different_content_conflict_unchanged(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events",
                json={"events": [_ev(1, "start", "C001", 1, 0)]})
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 1, 3)]})
    assert r.status_code == 409 and "冲突" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/actuals").json()
    assert len(g["events"]) == 1 and g["events"][0]["minute"] == 0


# ---------- 前置条件 ----------


def test_no_schedule_rejected_409(client):
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    r = client.post(f"/api/plans/{plan['plan_id']}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 1, 0)]})
    assert r.status_code == 409 and "排程" in r.json()["detail"]


def test_draft_plan_rejected_409(client):
    sr = client.post("/api/slot-revisions", json=STACK_SLOTS).json()["slots_revision"]
    client.post("/api/manifest-revisions", json=STACK_MANIFEST)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/actual-events",
                    json={"events": [_ev(1, "start", "C001", 1, 0)]})
    assert r.status_code == 409 and "确认" in r.json()["detail"]


def test_container_not_in_plan_rejected_422(client):
    plan = _stack_plan(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/actual-events",
                    json={"events": [_ev(1, "start", "C999", 1, 0)]})
    assert r.status_code == 422 and "不在方案" in r.json()["detail"]


def test_get_actuals_404(client):
    plan = _stack_plan(client)
    assert client.get(f"/api/plans/{plan['plan_id']}/actuals").status_code == 404
    assert client.get("/api/plans/nope/actuals").status_code == 404


def test_payload_validation(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    base = "/api/plans/%s/actual-events" % pid
    assert client.post(base, json={"events": []}).status_code == 422
    assert client.post(base, json={"events": [
        _ev(0, "start", "C001", 1, 0)]}).status_code == 422  # 流水号非正
    assert client.post(base, json={"events": [
        _ev(1, "start", "C001", 1, -1)]}).status_code == 422  # 负分钟
    assert client.post(base, json={"events": [
        _ev(1, "start", "C001", 0, 0)]}).status_code == 422  # 桥号非正
    assert client.post(base, json={"events": [
        _ev(1, "begin", "C001", 1, 0)]}).status_code == 422  # 类型非法
    assert client.post(base, json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(1, "finish", "C001", 1, 5)]}).status_code == 422  # 批内流水号重复


# ---------- 方案被取代后只读 ----------


def test_superseded_plan_actuals_are_read_only(client):
    plan = _stack_plan(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
    ]})

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

    # 再登记被拒（只读）
    r = client.post(f"/api/plans/{pid}/actual-events",
                    json={"events": [_ev(3, "start", "C002", 1, 10)]})
    assert r.status_code == 409 and "只读" in r.json()["detail"]

    # 查询仍返回冻结基线与实绩，read_only=true
    g = client.get(f"/api/plans/{pid}/actuals")
    assert g.status_code == 200
    data = g.json()
    assert data["read_only"] is True and data["plan_status"] == "superseded"
    assert [e["seq"] for e in data["events"]] == [1, 2]
    assert data["baseline_crane_count"] == 2
    # 历史排程仍在基线快照中
    assert {b["container_no"] for b in data["baseline"]} == {"C001", "C002"}


# ---------- 并发：首次绑定与登记串行化 ----------


def test_concurrent_first_binding_and_writes_serialize(client):
    """两个线程同时首次登记：基线只冻结一次，合法事件合并成功。"""
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)

    results = {}

    def register(seq, cno, crane, key):
        local = TestClient(app)
        r = local.post(f"/api/plans/{pid}/actual-events", json={"events": [
            _ev(seq, "start", cno, crane, 0)]})
        results[key] = r.status_code

    t1 = threading.Thread(target=register, args=(1, "C001", 1, "a"))
    t2 = threading.Thread(target=register, args=(2, "C003", 2, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results == {"a": 201, "b": 201}
    data = client.get(f"/api/plans/{pid}/actuals").json()
    assert sorted(e["seq"] for e in data["events"]) == [1, 2]
    assert len(data["baseline"]) == 2


def test_concurrent_same_seq_same_content_both_succeed(client):
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)

    results = []

    def register():
        local = TestClient(app)
        r = local.post(f"/api/plans/{pid}/actual-events",
                       json={"events": [_ev(1, "start", "C001", 1, 0)]})
        results.append(r.status_code)

    threads = [threading.Thread(target=register) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 201]
    data = client.get(f"/api/plans/{pid}/actuals").json()
    assert len(data["events"]) == 1
