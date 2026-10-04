"""装船完结交接（不可变凭证）端到端测试。"""

import threading

from fastapi.testclient import TestClient

from app.main import app


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


def _ev(seq, etype, cno, crane, minute):
    return {"seq": seq, "event_type": etype, "container_no": cno,
            "crane_no": crane, "minute": minute}


def _finished_stack(client):
    """同堆栈两箱全部完工：基线 C001[0,10)@1、C002[10,18)@1。

    实绩：C001 恰按基线；C002 第 11 分开工（桥2）、第 20 分完工（完工延误 2）。
    """
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    sch = _schedule(client, pid, crane_count=2)
    assert {e["container_no"]: (e["start_minute"], e["finish_minute"], e["crane_no"])
            for e in sch["entries"]} == {
        "C001": (0, 10, 1), "C002": (10, 18, 1)}
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 11),
        _ev(4, "finish", "C002", 2, 20),
    ]})
    assert r.status_code == 201, r.text
    return plan


def _handover_body(handover_no="H-1", cutoff=30, version=1,
                   seqs=(1, 2, 3, 4), threshold=5):
    return {
        "handover_no": handover_no,
        "cutoff_minute": cutoff,
        "expected_actual_version": version,
        "valid_seqs": list(seqs),
        "max_finish_delay_minutes": threshold,
    }


# ---------- 成功签发与凭证内容 ----------


def test_issue_handover_success_freezes_snapshot(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/handovers", json=_handover_body())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["handover_id"]
    assert data["handover_no"] == "H-1"
    assert data["plan_id"] == pid
    assert data["revision_no"] == 1
    assert data["cutoff_minute"] == 30
    assert data["expected_actual_version"] == 1
    assert data["actual_version"] == 1
    assert data["max_finish_delay_minutes"] == 5
    assert data["max_observed_finish_delay"] == 2
    assert data["valid_seqs"] == [1, 2, 3, 4]
    assert data["fingerprint_algorithm"] == "sha256"
    assert len(data["actuals_fingerprint"]) == 64
    assert data["matches_current_actuals"] is True
    assert data["current_actual_version"] == 1
    assert data["current_fingerprint"] == data["actuals_fingerprint"]
    boxes = {c["container_no"]: c for c in data["containers"]}
    assert boxes["C001"]["actual_start_minute"] == 0
    assert boxes["C001"]["actual_finish_minute"] == 10
    assert boxes["C001"]["actual_crane_no"] == 1
    assert boxes["C001"]["baseline_finish_minute"] == 10
    assert boxes["C001"]["finish_delta_minutes"] == 0
    assert boxes["C001"]["crane_changed"] is False
    c2 = boxes["C002"]
    assert (c2["actual_start_minute"], c2["actual_finish_minute"],
            c2["actual_crane_no"]) == (11, 20, 2)
    assert (c2["baseline_start_minute"], c2["baseline_finish_minute"],
            c2["baseline_crane_no"]) == (10, 18, 1)
    assert c2["start_delta_minutes"] == 1
    assert c2["finish_delta_minutes"] == 2
    assert c2["crane_changed"] is True
    assert data["summary"]["container_count"] == 2
    assert data["summary"]["max_observed_finish_delay"] == 2
    assert data["created_at"]


def test_finish_exactly_at_cutoff_allowed(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    # 最后一箱第 20 分完工，截止恰为 20（允许恰在此时刻）
    r = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body("H-cut", cutoff=20))
    assert r.status_code == 201, r.text


def test_early_finish_has_no_negative_delay(client):
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    sch = _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    assert {e["container_no"]: e["finish_minute"] for e in sch["entries"]} == {
        "C001": 10, "C003": 10}
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 8),   # 提前 2 分钟完工
        _ev(3, "start", "C003", 2, 0),
        _ev(4, "finish", "C003", 2, 10),
    ]})
    assert r.status_code == 201
    h = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body("H-early", seqs=(1, 2, 3, 4)))
    assert h.status_code == 201, h.text
    boxes = {c["container_no"]: c for c in h.json()["containers"]}
    assert boxes["C001"]["finish_delta_minutes"] == -2
    # 提前不计延误：最大观测延误为 0，阈值 0 也可签发
    assert h.json()["max_observed_finish_delay"] == 0
    h0 = client.post(f"/api/plans/{pid}/handovers",
                     json=_handover_body("H-zero", seqs=(1, 2, 3, 4), threshold=0))
    assert h0.status_code == 201


# ---------- 幂等与冲突 ----------


def test_same_handover_no_same_request_returns_original(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    body = _handover_body()
    r1 = client.post(f"/api/plans/{pid}/handovers", json=body)
    assert r1.status_code == 201
    r2 = client.post(f"/api/plans/{pid}/handovers", json=body)
    assert r2.status_code == 200
    assert r2.json()["handover_id"] == r1.json()["handover_id"]
    # 仅一条凭证
    lst = client.get(f"/api/plans/{pid}/handovers")
    assert len(lst.json()) == 1


def test_same_handover_no_different_content_conflicts(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    r1 = client.post(f"/api/plans/{pid}/handovers", json=_handover_body())
    assert r1.status_code == 201
    for key, altered in [
        ("cutoff_minute", 40),
        ("expected_actual_version", 2),
        ("max_finish_delay_minutes", 9),
    ]:
        body = _handover_body()
        body[key] = altered
        rr = client.post(f"/api/plans/{pid}/handovers", json=body)
        assert rr.status_code == 409, (key, rr.text)
        assert "交接号" in rr.json()["detail"]
    # valid_seqs 内容不同（排序差异不算）
    body = _handover_body(seqs=(4, 3, 2, 1))
    rr = client.post(f"/api/plans/{pid}/handovers", json=body)
    assert rr.status_code == 200  # 集合相同（顺序归一）视为同内容
    body = _handover_body(seqs=(1, 2, 3))
    rr = client.post(f"/api/plans/{pid}/handovers", json=body)
    assert rr.status_code == 409
    # 冲突不新增凭证
    assert len(client.get(f"/api/plans/{pid}/handovers").json()) == 1


def test_handover_no_is_global_across_plans(client):
    p1 = _finished_stack(client)
    r1 = client.post(f"/api/plans/{p1['plan_id']}/handovers", json=_handover_body())
    assert r1.status_code == 201

    # 第二艘船（独立槽位 C 堆栈，避开已占用槽位），独立方案链
    other_slots = {
        "slots": [
            {"code": "C01", "stack": "C", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "D01", "stack": "D", "tier": 1, "capacity": 100, "arm": 2.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    p2 = _confirm(client, other_slots, TWIN_MANIFEST)
    pid2 = p2["plan_id"]
    _schedule(client, pid2, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid2}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 0),
        _ev(4, "finish", "C003", 2, 10),
    ]})
    # 同交接号用于另一方案 -> 冲突，即使其它字段恰好相同
    r2 = client.post(f"/api/plans/{pid2}/handovers",
                     json=_handover_body(seqs=(1, 2, 3, 4)))
    assert r2.status_code == 409
    # 新交接号可正常签发
    r3 = client.post(f"/api/plans/{pid2}/handovers",
                     json=_handover_body("H-2", seqs=(1, 2, 3, 4)))
    assert r3.status_code == 201


# ---------- 拒绝条件 ----------


def test_unfinished_box_rejected_422_with_detail(client):
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 11),  # 未完工
    ]})
    r = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body(seqs=(1, 2, 3)))
    assert r.status_code == 422, r.text
    body = r.json()
    assert "截止" in body["detail"]
    assert body["unfinished_containers"] == ["C002"]
    assert body["finished_after_cutoff"] == []
    assert body["events_after_cutoff"] == []
    assert body["cutoff_minute"] == 30
    assert client.get(f"/api/plans/{pid}/handovers").json() == []


def test_finish_after_cutoff_rejected_422(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    # C002 第 20 分完工，截止 19 -> 该箱超截止，且其完工事件位于截止后
    r = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body("H-late", cutoff=19))
    assert r.status_code == 422
    body = r.json()
    assert [x["container_no"] for x in body["finished_after_cutoff"]] == ["C002"]
    assert body["finished_after_cutoff"][0]["actual_finish_minute"] == 20
    assert {e["seq"] for e in body["events_after_cutoff"]} == {4}


def test_start_or_finish_event_after_cutoff_rejected(client):
    """截止后存在任何有效事件（开工或完工）即拒绝；本用例开工与完工均晚于截止。"""
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 35),
        _ev(4, "finish", "C003", 2, 40),
    ]})
    r = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body("H-a", cutoff=30, seqs=(1, 2, 3, 4)))
    assert r.status_code == 422
    body = r.json()
    assert body["unfinished_containers"] == []
    assert [x["container_no"] for x in body["finished_after_cutoff"]] == ["C003"]
    assert {e["seq"] for e in body["events_after_cutoff"]} == {3, 4}


def test_finish_delay_exceeds_threshold_returns_boxes_and_deviation(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body("H-t", threshold=1))
    assert r.status_code == 422
    body = r.json()
    assert "延误" in body["detail"]
    assert body["allowed_max_delay_minutes"] == 1
    assert len(body["exceeded_containers"]) == 1
    ex = body["exceeded_containers"][0]
    assert ex["container_no"] == "C002"
    assert ex["baseline_finish_minute"] == 18
    assert ex["actual_finish_minute"] == 20
    assert ex["finish_delta_minutes"] == 2
    assert ex["delay_minutes"] == 2
    # 不生成凭证
    assert client.get(f"/api/plans/{pid}/handovers").json() == []


def test_delay_threshold_boundary_equal_allowed(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    # 延误 2，阈值恰为 2：允许
    r = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body("H-eq", threshold=2))
    assert r.status_code == 201


def test_no_actuals_rejected_409(client):
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    _schedule(client, plan["plan_id"], crane_count=2)
    r = client.post(f"/api/plans/{plan['plan_id']}/handovers", json=_handover_body())
    assert r.status_code == 409 and "实绩" in r.json()["detail"]


def test_draft_plan_rejected_409(client):
    sr = client.post("/api/slot-revisions", json=STACK_SLOTS).json()["slots_revision"]
    client.post("/api/manifest-revisions", json=STACK_MANIFEST)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/handovers", json=_handover_body())
    assert r.status_code == 409 and "确认" in r.json()["detail"]


def test_superseded_plan_rejected_409_but_existing_cert_readable(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    h = client.post(f"/api/plans/{pid}/handovers", json=_handover_body())
    assert h.status_code == 201
    hid = h.json()["handover_id"]

    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    r = client.post(f"/api/plans/{pid}/handovers", json=_handover_body("H-x"))
    assert r.status_code == 409 and "取代" in r.json()["detail"]
    # 原凭证仍可读
    g = client.get(f"/api/handovers/{hid}")
    assert g.status_code == 200
    assert g.json()["handover_id"] == hid


def test_expected_version_mismatch_conflict(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    body = _handover_body(version=2)  # 当前为 1
    r = client.post(f"/api/plans/{pid}/handovers", json=body)
    assert r.status_code == 409 and "版本" in r.json()["detail"]


def test_valid_seqs_unknown_or_missing_conflict(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    r1 = client.post(f"/api/plans/{pid}/handovers",
                     json=_handover_body("H-u", seqs=(1, 2, 3, 4, 99)))
    assert r1.status_code == 409 and "未登记" in r1.json()["detail"]
    r2 = client.post(f"/api/plans/{pid}/handovers",
                     json=_handover_body("H-m", seqs=(1, 2, 4)))
    assert r2.status_code == 409 and "遗漏" in r2.json()["detail"]
    assert client.get(f"/api/plans/{pid}/handovers").json() == []


def test_unknown_plan_or_certificate_404(client):
    plan = _finished_stack(client)
    assert client.post("/api/plans/nope/handovers",
                       json=_handover_body()).status_code == 404
    hid = client.post(f"/api/plans/{plan['plan_id']}/handovers",
                      json=_handover_body()).json()["handover_id"]
    assert client.get("/api/handovers/nope").status_code == 404
    # 凭证不属于该方案路径 -> 404（另建一个草稿方案，无需确认）
    client.post("/api/slot-revisions", json={
        "slots": [{"code": "Z01", "stack": "Z", "tier": 1,
                   "capacity": 100, "arm": 2.0}],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    })
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "ZZ1", "weight": 10, "hazmat": None,
                        "discharge_order": 1}]
    })
    other = client.post("/api/plans/compute", json={}).json()
    r = client.get(f"/api/plans/{other['plan_id']}/handovers/{hid}")
    assert r.status_code == 404
    assert client.get(f"/api/handovers/{hid}").status_code == 200


def test_payload_validation_422(client):
    plan = _finished_stack(client)
    base = f"/api/plans/{plan['plan_id']}/handovers"

    def post(mod):
        body = _handover_body()
        body.update(mod)
        return client.post(base, json=body)

    assert post({"handover_no": "   "}).status_code == 422
    assert post({"cutoff_minute": -1}).status_code == 422
    assert post({"expected_actual_version": 0}).status_code == 422
    assert post({"max_finish_delay_minutes": -1}).status_code == 422
    assert post({"valid_seqs": []}).status_code == 422
    assert post({"valid_seqs": [1, 1, 2]}).status_code == 422
    assert post({"valid_seqs": [0, 1]}).status_code == 422
    for key in ("handover_no", "cutoff_minute", "expected_actual_version",
                "valid_seqs", "max_finish_delay_minutes"):
        body = _handover_body()
        del body[key]
        assert client.post(base, json=body).status_code == 422, key


# ---------- 凭证不可变 & 后续实绩变化 ----------


def test_certificate_immutable_after_correction_but_marked_stale(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    h1 = client.post(f"/api/plans/{pid}/handovers", json=_handover_body())
    assert h1.status_code == 201
    hid = h1.json()["handover_id"]
    frozen_fingerprint = h1.json()["actuals_fingerprint"]

    # 更正 C002 完工分钟 20 -> 19（版本 2）
    cc = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 4, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 4, "event_type": "finish", "container_no": "C002",
                  "crane_no": 2, "minute": 19},
    })
    assert cc.status_code == 201 and cc.json()["version"] == 2

    g = client.get(f"/api/handovers/{hid}")
    assert g.status_code == 200
    data = g.json()
    # 凭证内容不变
    c2 = next(c for c in data["containers"] if c["container_no"] == "C002")
    assert c2["actual_finish_minute"] == 20
    assert data["actual_version"] == 1
    assert data["actuals_fingerprint"] == frozen_fingerprint
    # 标示已不对应
    assert data["matches_current_actuals"] is False
    assert data["current_actual_version"] == 2
    assert data["current_fingerprint"] != frozen_fingerprint

    # 同交接号同内容重试仍返回原凭证（200，不重新评估）
    r = client.post(f"/api/plans/{pid}/handovers", json=_handover_body())
    assert r.status_code == 200
    assert r.json()["handover_id"] == hid

    # 基于版本 2 可再签发一张凭证（新交接号），标示为对应当前
    h2 = client.post(f"/api/plans/{pid}/handovers",
                     json=_handover_body("H-2", cutoff=30, version=2))
    assert h2.status_code == 201
    assert h2.json()["matches_current_actuals"] is True
    listed = client.get(f"/api/plans/{pid}/handovers").json()
    assert [x["handover_no"] for x in listed] == ["H-1", "H-2"]


def test_certificate_marked_stale_after_correction_alt(client):
    """凭证签发后再发生更正，凭证查询标示不再对应当前实绩（双栈方案版）。"""
    plan = _confirm(client, TWIN_SLOTS, TWIN_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2, nos=("C001", "C003"), d1=10, d2=10)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 0),
        _ev(4, "finish", "C003", 2, 10),
    ]})
    h = client.post(f"/api/plans/{pid}/handovers",
                    json=_handover_body(seqs=(1, 2, 3, 4)))
    assert h.status_code == 201
    cc = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 2, "expected_version": 1, "request_id": "CORR-9",
        "event": {"seq": 2, "event_type": "finish", "container_no": "C001",
                  "crane_no": 1, "minute": 9},
    })
    assert cc.status_code == 201
    g = client.get(f"/api/handovers/{h.json()['handover_id']}").json()
    assert g["matches_current_actuals"] is False
    assert g["current_actual_version"] == 2
    # 固化内容不变
    c1 = next(c for c in g["containers"] if c["container_no"] == "C001")
    assert c1["actual_finish_minute"] == 10


# ---------- 并发：签发前复核，不留旧快照 ----------


def test_concurrent_correction_and_handover_leaves_no_stale_snapshot(client):
    """签发写锁内复核：与更正并发时，凭证不得固化更正前的旧指纹。

    两种结果均合法：凭证按旧版本失败 409；或凭证按更正后版本签发（本用例中
    更正发生在锁前预检之后时，申请期望版本 1 -> 锁内版本已为 2 -> 409）。
    关键不变量：凡 201 凭证的指纹必等于其提交时的当前实绩指纹，且绝无旧快照。
    """
    plan = _finished_stack(client)
    pid = plan["plan_id"]

    results = {}
    barrier = threading.Barrier(2)

    def issue():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/handovers",
                       json=_handover_body("H-C"))
        results["handover"] = r.status_code

    def correct():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/actual-events/correction", json={
            "seq": 4, "expected_version": 1, "request_id": "CORR-C",
            "event": {"seq": 4, "event_type": "finish", "container_no": "C002",
                      "crane_no": 2, "minute": 19},
        })
        results["correction"] = r.status_code

    t1 = threading.Thread(target=issue)
    t2 = threading.Thread(target=correct)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results["correction"] == 201
    assert results["handover"] in (201, 409)
    listed = client.get(f"/api/plans/{pid}/handovers").json()
    actuals = client.get(f"/api/plans/{pid}/actuals").json()
    current_version = actuals["version"]
    for cert in listed:
        # 不变量：凭证内容与“是否对应当前实绩”标示必须自洽——
        # 若标示匹配，则版本与指纹必然是当前值；否则必为旧版本。绝不允许
        # 版本号是新值而指纹是旧值（或反之）的混合旧快照。
        if cert["matches_current_actuals"]:
            assert cert["actual_version"] == current_version
            assert current_version == 2
        else:
            assert cert["actual_version"] == 1


def test_concurrent_same_handover_no_same_request_one_wins(client):
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    results = []
    barrier = threading.Barrier(2)

    def issue():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/handovers", json=_handover_body("H-D"))
        results.append(r.status_code)

    threads = [threading.Thread(target=issue) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 201]
    listed = client.get(f"/api/plans/{pid}/handovers").json()
    assert len(listed) == 1


def test_rejected_attempt_does_not_consume_handover_no(client):
    """失败的申请不占用交接号：修正内容后同号可成功签发。"""
    plan = _finished_stack(client)
    pid = plan["plan_id"]
    # 版本错误 -> 409，不占用 H-R
    bad = _handover_body("H-R", version=2)
    assert client.post(f"/api/plans/{pid}/handovers", json=bad).status_code == 409
    ok = client.post(f"/api/plans/{pid}/handovers",
                     json=_handover_body("H-R"))
    assert ok.status_code == 201


def test_supplementary_registration_after_precheck_then_handover_rejected(client):
    """“预检后、签发前被补登”的等价串行场景：

    客户端先看到版本 1、4 条流水号；随后并发方补登新事件（本模型中补登新
    流水号只能发生在尚有未完箱时——先登记 3 条，申请准备期间补登第 4 条）。
    以旧的流水号集合申请必须被锁内复核拒绝，绝不固化缺事件的旧快照。
    """
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    _schedule(client, pid, crane_count=2)
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 11),
    ]})
    # 客户端此刻掌握的有效集合是 {1,2,3}；并发补登第 4 条（C002 完工）
    late = client.post(f"/api/plans/{pid}/actual-events",
                       json={"events": [_ev(4, "finish", "C002", 2, 20)]})
    assert late.status_code == 201

    stale = client.post(f"/api/plans/{pid}/handovers",
                        json=_handover_body("H-S", seqs=(1, 2, 3)))
    assert stale.status_code == 409 and "遗漏" in stale.json()["detail"]
    # 以最新集合申请成功，凭证包含 4 条流水号
    ok = client.post(f"/api/plans/{pid}/handovers",
                     json=_handover_body("H-S2", seqs=(1, 2, 3, 4)))
    assert ok.status_code == 201
    assert ok.json()["valid_seqs"] == [1, 2, 3, 4]


def test_actuals_fingerprint_sensitive_to_events_and_version(client):
    """指纹须对版本与任一事件现行内容（流水号集合/分钟/岸桥）增改变化敏感。"""
    from app.services import _actuals_fingerprint

    base_events = [(1, "start", "C001", 1, 0), (2, "finish", "C001", 1, 10)]
    fp1 = _actuals_fingerprint(1, base_events)
    fp_added = _actuals_fingerprint(1, base_events + [(3, "start", "C002", 2, 10)])
    fp_version = _actuals_fingerprint(2, base_events)
    fp_corrected = _actuals_fingerprint(1, [(1, "start", "C001", 1, 0),
                                            (2, "finish", "C001", 1, 9)])
    assert len({fp1, fp_added, fp_version, fp_corrected}) == 4
    assert len(fp1) == 64
    int(fp1, 16)  # 十六进制
    # 与输入顺序无关（规范化按流水号排序）
    assert _actuals_fingerprint(1, list(reversed(base_events))) == fp1
