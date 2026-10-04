"""离港前封签核对与放行凭证端到端测试。"""

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

# 第二艘船（独立槽位，避免与已确认方案占用冲突）
OTHER_SLOTS = {
    "slots": [
        {"code": "C01", "stack": "C", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "D01", "stack": "D", "tier": 1, "capacity": 100, "arm": 2.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}
OTHER_MANIFEST = {
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


def _ev(seq, etype, cno, crane, minute):
    return {"seq": seq, "event_type": etype, "container_no": cno,
            "crane_no": crane, "minute": minute}


def _handover_plan(client, handover_no="H-1"):
    """已确认方案 + 装船排程 + 全部完工实绩 + 交接凭证（C001/C002 同堆栈）。"""
    plan = _confirm(client, STACK_SLOTS, STACK_MANIFEST)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/schedules", json={
        "crane_count": 2,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
            {"no": "C002", "duration": 8, "release_minute": 0,
             "deadline_minute": 100},
        ],
    })
    assert r.status_code == 201, r.text
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 11),
        _ev(4, "finish", "C002", 2, 20),
    ]})
    assert r.status_code == 201, r.text
    h = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": handover_no,
        "cutoff_minute": 30,
        "expected_actual_version": 1,
        "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    })
    assert h.status_code == 201, h.text
    return plan, h.json()


def _manifest_body(seals=("SEAL-001", "SEAL-002"), nos=("C001", "C002")):
    return {"items": [{"container_no": n, "seal_no": s}
                      for n, s in zip(nos, seals)]}


def _register_manifest(client, pid, **kwargs):
    r = client.post(f"/api/plans/{pid}/seal-manifest",
                    json=_manifest_body(**kwargs))
    assert r.status_code == 201, r.text
    return r.json()


def _scan(scan_no, cno, seal, minute):
    return {"scan_no": scan_no, "container_no": cno,
            "seal_no": seal, "check_minute": minute}


def _scan_all(client, pid, m1=100, m2=110):
    """两箱各一条匹配扫描：扫描版本推进到 2。"""
    r1 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-1", "C001", "SEAL-001", m1))
    assert r1.status_code == 201, r1.text
    r2 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-2", "C002", "SEAL-002", m2))
    assert r2.status_code == 201, r2.text
    return r1.json(), r2.json()


def _release_body(handover_id, release_no="REL-1", version=2, cutoff=200):
    return {"release_no": release_no, "handover_id": handover_id,
            "expected_scan_version": version, "cutoff_minute": cutoff}


def _ready_for_release(client):
    """完成交接、清单与两箱匹配扫描的方案；返回 (plan, handover)。"""
    plan, handover = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_all(client, pid)
    return plan, handover


# ---------- 预期封签清单：登记、冻结、幂等与冲突 ----------


def test_register_manifest_success(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-manifest", json=_manifest_body())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["plan_id"] == pid
    assert data["revision_no"] == 1
    assert data["container_count"] == 2
    assert data["scan_version"] == 0
    assert data["fingerprint_algorithm"] == "sha256"
    assert len(data["fingerprint"]) == 64
    assert data["items"] == [
        {"container_no": "C001", "expected_seal_no": "SEAL-001"},
        {"container_no": "C002", "expected_seal_no": "SEAL-002"},
    ]
    assert data["created_at"]
    # 查询一致
    g = client.get(f"/api/plans/{pid}/seal-manifest")
    assert g.status_code == 200 and g.json() == data


def test_manifest_same_content_retry_returns_original(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    body = _manifest_body()
    r1 = client.post(f"/api/plans/{pid}/seal-manifest", json=body)
    assert r1.status_code == 201
    r2 = client.post(f"/api/plans/{pid}/seal-manifest", json=body)
    assert r2.status_code == 200
    assert r2.json()["fingerprint"] == r1.json()["fingerprint"]
    # 提交顺序不同但内容相同，同样幂等
    reversed_body = {"items": list(reversed(body["items"]))}
    r3 = client.post(f"/api/plans/{pid}/seal-manifest", json=reversed_body)
    assert r3.status_code == 200
    assert r3.json()["fingerprint"] == r1.json()["fingerprint"]


def test_manifest_frozen_different_content_conflicts(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    # 改一个封签号 -> 409
    r = client.post(f"/api/plans/{pid}/seal-manifest",
                    json=_manifest_body(seals=("SEAL-001", "SEAL-999")))
    assert r.status_code == 409 and "冻结" in r.json()["detail"]
    # 原清单不变
    g = client.get(f"/api/plans/{pid}/seal-manifest").json()
    assert [i["expected_seal_no"] for i in g["items"]] == ["SEAL-001", "SEAL-002"]


def test_manifest_must_cover_all_containers(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    # 缺箱
    r1 = client.post(f"/api/plans/{pid}/seal-manifest",
                     json=_manifest_body(nos=("C001",), seals=("SEAL-001",)))
    assert r1.status_code == 422 and "缺少箱" in r1.json()["detail"]
    # 方案外箱
    r2 = client.post(f"/api/plans/{pid}/seal-manifest",
                     json=_manifest_body(nos=("C001", "C002", "C999"),
                                         seals=("S1", "S2", "S3")))
    assert r2.status_code == 422 and "方案外箱" in r2.json()["detail"]
    # 均未写入
    assert client.get(f"/api/plans/{pid}/seal-manifest").status_code == 404


def test_manifest_validation_422(client):
    plan, _ = _handover_plan(client)
    base = f"/api/plans/{plan['plan_id']}/seal-manifest"
    # 封签号互异
    r = client.post(base, json=_manifest_body(seals=("SEAL-X", "SEAL-X")))
    assert r.status_code == 422 and "互异" in str(r.json()["detail"])
    # 批内箱号重复
    r = client.post(base, json=_manifest_body(nos=("C001", "C001"),
                                              seals=("S1", "S2")))
    assert r.status_code == 422
    # 空封签号 / 空箱号
    r = client.post(base, json={"items": [
        {"container_no": "C001", "seal_no": "  "},
        {"container_no": "C002", "seal_no": "S2"}]})
    assert r.status_code == 422
    r = client.post(base, json={"items": [
        {"container_no": "", "seal_no": "S1"}]})
    assert r.status_code == 422
    # 缺字段
    assert client.post(base, json={}).status_code == 422


def test_manifest_plan_state_rejections(client):
    # 未知方案 404
    r = client.post("/api/plans/nope/seal-manifest", json=_manifest_body())
    assert r.status_code == 404
    # 草稿方案 409
    client.post("/api/slot-revisions", json=STACK_SLOTS)
    client.post("/api/manifest-revisions", json=STACK_MANIFEST)
    draft = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{draft['plan_id']}/seal-manifest",
                    json=_manifest_body())
    assert r.status_code == 409 and "确认" in r.json()["detail"]


def test_manifest_superseded_plan_rejected_but_readable(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _supersede(client, pid)
    # 异内容：清单冻结不可改写 -> 409
    r = client.post(f"/api/plans/{pid}/seal-manifest",
                    json=_manifest_body(seals=("S1", "S2")))
    assert r.status_code == 409 and "冻结" in r.json()["detail"]
    # 同内容重试仍返回原清单（200）
    r2 = client.post(f"/api/plans/{pid}/seal-manifest", json=_manifest_body())
    assert r2.status_code == 200
    # 历史清单仍可查
    g = client.get(f"/api/plans/{pid}/seal-manifest")
    assert g.status_code == 200 and g.json()["container_count"] == 2


def _supersede(client, pid):
    """以新箱清单修订产生 revision 2 并确认，使原方案被取代。"""
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200, ok.text
    return p2


# ---------- 现场封签扫描 ----------


def test_scan_success_matched_and_mismatch_traced(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r1 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r1.status_code == 201, r1.text
    d1 = r1.json()
    assert d1["matched"] is True and d1["effective"] is True
    assert d1["scan_version"] == 1
    assert d1["expected_seal_no"] == "SEAL-001"
    # 封签不符：留痕但不算通过
    r2 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-2", "C002", "SEAL-XXX", 105))
    assert r2.status_code == 201
    d2 = r2.json()
    assert d2["matched"] is False and d2["effective"] is True
    assert d2["scan_version"] == 2
    # 状态视图：C002 未通过
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert st["scan_version"] == 2
    boxes = {c["container_no"]: c for c in st["containers"]}
    assert boxes["C001"]["passed"] is True
    assert boxes["C002"]["scanned"] is True and boxes["C002"]["passed"] is False
    assert boxes["C002"]["observed_seal_no"] == "SEAL-XXX"
    # 留痕两条均可查
    assert [s["scan_no"] for s in st["scans"]] == ["SC-1", "SC-2"]
    assert st["scans"][1]["matched"] is False


def test_scan_idempotent_retry_returns_original_snapshot(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    body = _scan("SC-1", "C001", "SEAL-001", 100)
    r1 = client.post(f"/api/plans/{pid}/seal-scans", json=body)
    assert r1.status_code == 201
    # 后续扫描推进版本后，同号同内容重试仍返回首次快照（版本定格）
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-002", 110))
    r2 = client.post(f"/api/plans/{pid}/seal-scans", json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert r2.json()["scan_version"] == 1
    # 扫描版本不因重试重复推进
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert st["scan_version"] == 2
    assert len(st["scans"]) == 2


def test_scan_same_no_different_content_conflicts(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r1 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r1.status_code == 201
    for altered in [
        _scan("SC-1", "C002", "SEAL-002", 100),   # 异箱
        _scan("SC-1", "C001", "SEAL-XXX", 100),   # 异封签
        _scan("SC-1", "C001", "SEAL-001", 101),   # 异分钟
    ]:
        rr = client.post(f"/api/plans/{pid}/seal-scans", json=altered)
        assert rr.status_code == 409, rr.text
        assert "扫描号" in rr.json()["detail"]
    # 冲突不写入
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert st["scan_version"] == 1 and len(st["scans"]) == 1


def test_scan_no_is_global_across_plans(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r1 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r1.status_code == 201
    # 第二艘船独立方案，同扫描号复用 -> 409（即使内容字段相同）
    p2 = _confirm(client, OTHER_SLOTS, OTHER_MANIFEST)
    pid2 = p2["plan_id"]
    _register_manifest(client, pid2, nos=("C001", "C003"),
                       seals=("SEAL-001", "SEAL-003"))
    r2 = client.post(f"/api/plans/{pid2}/seal-scans",
                     json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r2.status_code == 409 and "另一方案" in r2.json()["detail"]
    # 新扫描号可正常登记
    r3 = client.post(f"/api/plans/{pid2}/seal-scans",
                     json=_scan("SC-2", "C001", "SEAL-001", 100))
    assert r3.status_code == 201


def test_scan_unknown_container_rejected_422(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("SC-X", "C999", "SEAL-9", 100))
    assert r.status_code == 422 and "未知箱号" in r.json()["detail"]
    # 拒绝不写入、不占用扫描号：修正后同号可登记
    assert client.get(f"/api/plans/{pid}/seal-scans").json()["scan_version"] == 0
    ok = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-X", "C001", "SEAL-001", 100))
    assert ok.status_code == 201


def test_scan_requires_manifest_and_current_plan(client):
    # 无清单 -> 409
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r.status_code == 409 and "清单" in r.json()["detail"]
    # 未知方案 -> 404
    assert client.post("/api/plans/nope/seal-scans",
                       json=_scan("SC-1", "C001", "S", 1)).status_code == 404
    # 方案被取代后只读 -> 409，历史扫描仍可查
    _register_manifest(client, pid)
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    _supersede(client, pid)
    r2 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-2", "C002", "SEAL-002", 110))
    assert r2.status_code == 409 and "取代" in r2.json()["detail"]
    st = client.get(f"/api/plans/{pid}/seal-scans")
    assert st.status_code == 200
    assert st.json()["read_only"] is True
    assert st.json()["plan_status"] == "superseded"
    # 同号同内容重试即使已取代也返回原记录
    r3 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r3.status_code == 200


def test_scan_validation_422(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    base = f"/api/plans/{pid}/seal-scans"
    assert client.post(base, json=_scan("  ", "C001", "S", 1)).status_code == 422
    assert client.post(base, json=_scan("SC", "", "S", 1)).status_code == 422
    assert client.post(base, json=_scan("SC", "C001", " ", 1)).status_code == 422
    assert client.post(base, json=_scan("SC", "C001", "S", -1)).status_code == 422
    body = _scan("SC", "C001", "S", 1)
    for key in ("scan_no", "container_no", "seal_no", "check_minute"):
        b = dict(body)
        del b[key]
        assert client.post(base, json=b).status_code == 422, key


def test_latest_scan_per_container_rules(client):
    """同箱以检查分钟较晚者为准；同分钟以登记顺序较晚者为准。"""
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    # 分钟 100 匹配，随后分钟 50 不符：较早分钟不取代 -> 仍通过
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("SC-2", "C001", "SEAL-X", 50))
    assert r.status_code == 201 and r.json()["effective"] is False
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    c1 = next(c for c in st["containers"] if c["container_no"] == "C001")
    assert c1["passed"] is True and c1["latest_scan_no"] == "SC-1"
    # 分钟 120 不符：较晚分钟取代 -> 不通过
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-3", "C001", "SEAL-X", 120))
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    c1 = next(c for c in st["containers"] if c["container_no"] == "C001")
    assert c1["passed"] is False and c1["latest_scan_no"] == "SC-3"
    # 同分钟 120 再登记匹配：同分钟登记顺序较晚者为准 -> 通过
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-4", "C001", "SEAL-001", 120))
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    c1 = next(c for c in st["containers"] if c["container_no"] == "C001")
    assert c1["passed"] is True and c1["latest_scan_no"] == "SC-4"
    # 留痕四条，effective 仅 SC-4
    eff = {s["scan_no"]: s["effective"] for s in st["scans"]}
    assert eff == {"SC-1": False, "SC-2": False, "SC-3": False, "SC-4": True}
    assert st["scan_version"] == 4


def test_scan_status_404_without_manifest(client):
    plan, _ = _handover_plan(client)
    assert client.get(f"/api/plans/{plan['plan_id']}/seal-scans") \
        .status_code == 404
    assert client.get("/api/plans/nope/seal-scans").status_code == 404


# ---------- 离港放行：签发与凭证内容 ----------


def test_issue_release_success_freezes_snapshot(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"]))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["release_id"]
    assert data["release_no"] == "REL-1"
    assert data["plan_id"] == pid
    assert data["revision_no"] == 1
    assert data["handover_id"] == handover["handover_id"]
    assert data["handover_no"] == "H-1"
    assert data["cutoff_minute"] == 200
    assert data["expected_scan_version"] == 2
    assert data["scan_version"] == 2
    assert data["container_count"] == 2
    assert data["max_check_minute"] == 110
    assert len(data["scans_fingerprint"]) == 64
    assert data["actuals_fingerprint"] == handover["actuals_fingerprint"]
    assert data["fingerprint_algorithm"] == "sha256"
    boxes = {c["container_no"]: c for c in data["containers"]}
    assert boxes["C001"] == {
        "container_no": "C001", "expected_seal_no": "SEAL-001",
        "observed_seal_no": "SEAL-001", "check_minute": 100,
        "scan_no": "SC-1", "matched": True}
    assert boxes["C002"]["check_minute"] == 110
    assert data["summary"]["max_check_minute"] == 110
    assert data["summary"]["scan_version"] == 2
    assert data["is_current"] is True
    assert data["invalid_reasons"] == []
    assert data["plan_status"] == "confirmed"
    assert data["current_scan_version"] == 2
    assert data["current_scans_fingerprint"] == data["scans_fingerprint"]
    assert data["current_actuals_fingerprint"] == data["actuals_fingerprint"]
    # 查询一致
    g = client.get(f"/api/seal-releases/{data['release_id']}")
    assert g.status_code == 200 and g.json() == data
    g2 = client.get(f"/api/plans/{pid}/seal-releases/{data['release_id']}")
    assert g2.status_code == 200
    lst = client.get(f"/api/plans/{pid}/seal-releases").json()
    assert [x["release_no"] for x in lst] == ["REL-1"]


def test_check_exactly_at_cutoff_allowed(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    # 最大检查分钟 110，截止恰为 110（允许恰在此时刻）
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], cutoff=110))
    assert r.status_code == 201, r.text


def test_release_idempotent_retry_and_conflicts(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    body = _release_body(handover["handover_id"])
    r1 = client.post(f"/api/plans/{pid}/seal-releases", json=body)
    assert r1.status_code == 201
    r2 = client.post(f"/api/plans/{pid}/seal-releases", json=body)
    assert r2.status_code == 200
    assert r2.json()["release_id"] == r1.json()["release_id"]
    # 异内容冲突：改截止/版本/交接凭证均 409
    for key, altered in [("cutoff_minute", 300), ("expected_scan_version", 1)]:
        b = _release_body(handover["handover_id"])
        b[key] = altered
        rr = client.post(f"/api/plans/{pid}/seal-releases", json=b)
        assert rr.status_code == 409, (key, rr.text)
    # 冲突不新增凭证
    assert len(client.get(f"/api/plans/{pid}/seal-releases").json()) == 1


def test_release_no_is_global_across_plans(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    r1 = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"]))
    assert r1.status_code == 201
    # 第二艘船完整走到可放行，同申请号 -> 409
    p2 = _confirm(client, OTHER_SLOTS, OTHER_MANIFEST)
    pid2 = p2["plan_id"]
    client.post(f"/api/plans/{pid2}/schedules", json={
        "crane_count": 2,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
            {"no": "C003", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
        ],
    })
    client.post(f"/api/plans/{pid2}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0), _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 0), _ev(4, "finish", "C003", 2, 10),
    ]})
    h2 = client.post(f"/api/plans/{pid2}/handovers", json={
        "handover_no": "H-2", "cutoff_minute": 30,
        "expected_actual_version": 1, "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    }).json()
    _register_manifest(client, pid2, nos=("C001", "C003"),
                       seals=("SEAL-001", "SEAL-003"))
    client.post(f"/api/plans/{pid2}/seal-scans",
                json=_scan("SC-A", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid2}/seal-scans",
                json=_scan("SC-B", "C003", "SEAL-003", 100))
    r2 = client.post(f"/api/plans/{pid2}/seal-releases",
                     json=_release_body(h2["handover_id"]))
    assert r2.status_code == 409 and "另一方案" in r2.json()["detail"]
    # 新申请号可正常签发
    r3 = client.post(f"/api/plans/{pid2}/seal-releases",
                     json=_release_body(h2["handover_id"], release_no="REL-2"))
    assert r3.status_code == 201


# ---------- 离港放行：拒绝条件 ----------


def test_release_missing_scan_rejected_422(client):
    plan, handover = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    # 只扫 C001
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], version=1))
    assert r.status_code == 422, r.text
    body = r.json()
    assert "缺扫描" in body["detail"]
    assert body["missing_scans"] == ["C002"]
    assert body["mismatched_containers"] == []
    assert body["scans_after_cutoff"] == []
    assert client.get(f"/api/plans/{pid}/seal-releases").json() == []


def test_release_mismatched_latest_rejected_422(client):
    plan, handover = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    # C002 先匹配（分钟 100）后不匹配（分钟 120，较晚者为准）
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-002", 100))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-3", "C002", "SEAL-BAD", 120))
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], version=3))
    assert r.status_code == 422, r.text
    body = r.json()
    assert "不符" in body["detail"]
    assert body["missing_scans"] == []
    assert len(body["mismatched_containers"]) == 1
    mm = body["mismatched_containers"][0]
    assert mm["container_no"] == "C002"
    assert mm["expected_seal_no"] == "SEAL-002"
    assert mm["observed_seal_no"] == "SEAL-BAD"
    assert mm["check_minute"] == 120 and mm["scan_no"] == "SC-3"
    # 不符留痕仍在
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert len(st["scans"]) == 3


def test_release_scan_after_cutoff_rejected_422(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    # C002 第 110 分检查，截止 109
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], cutoff=109))
    assert r.status_code == 422, r.text
    body = r.json()
    assert "截止" in body["detail"]
    assert body["scans_after_cutoff"] == [
        {"container_no": "C002", "check_minute": 110, "scan_no": "SC-2"}]
    assert body["cutoff_minute"] == 109


def test_release_stale_scan_version_rejected_409(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    # 客户端基于版本 1 申请，当前已为 2
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], version=1))
    assert r.status_code == 409 and "版本" in r.json()["detail"]
    # 签发后又来一条扫描：原申请号重试仍返回原凭证，新申请号按旧版本拒绝
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"]))
    assert ok.status_code == 201
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-3", "C001", "SEAL-001", 150))
    r2 = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"],
                                        release_no="REL-2", version=2))
    assert r2.status_code == 409
    r3 = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"],
                                        release_no="REL-2", version=3))
    assert r3.status_code == 201


def test_release_handover_problems(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    # 未知交接凭证 -> 404
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body("nope"))
    assert r.status_code == 404
    # 交接凭证属于另一方案 -> 422
    p2 = _confirm(client, OTHER_SLOTS, OTHER_MANIFEST)
    pid2 = p2["plan_id"]
    client.post(f"/api/plans/{pid2}/schedules", json={
        "crane_count": 2,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
            {"no": "C003", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
        ],
    })
    client.post(f"/api/plans/{pid2}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0), _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 0), _ev(4, "finish", "C003", 2, 10),
    ]})
    h2 = client.post(f"/api/plans/{pid2}/handovers", json={
        "handover_no": "H-2", "cutoff_minute": 30,
        "expected_actual_version": 1, "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    }).json()
    r2 = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(h2["handover_id"], release_no="REL-X"))
    assert r2.status_code == 422 and "不属于" in r2.json()["detail"]


def test_release_handover_stale_after_correction_rejected_409(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    # 交接后实绩被更正：交接凭证不再对应当前实绩
    cc = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 4, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 4, "event_type": "finish", "container_no": "C002",
                  "crane_no": 2, "minute": 19},
    })
    assert cc.status_code == 201
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"]))
    assert r.status_code == 409 and "实绩" in r.json()["detail"]
    # 基于版本 2 重新交接后可放行
    h2 = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": "H-2", "cutoff_minute": 30,
        "expected_actual_version": 2, "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    })
    assert h2.status_code == 201
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(h2.json()["handover_id"],
                                        release_no="REL-2"))
    assert ok.status_code == 201
    assert ok.json()["handover_no"] == "H-2"


def test_release_requires_manifest_and_confirmed_plan(client):
    # 无清单 -> 409
    plan, handover = _handover_plan(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], version=0))
    assert r.status_code == 409 and "清单" in r.json()["detail"]
    # 未知方案 -> 404
    assert client.post("/api/plans/nope/seal-releases",
                       json=_release_body("x")).status_code == 404
    # 方案被取代 -> 409
    _register_manifest(client, pid)
    _scan_all(client, pid)
    _supersede(client, pid)
    r2 = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"]))
    assert r2.status_code == 409 and "取代" in r2.json()["detail"]


def test_release_validation_422(client):
    plan, handover = _ready_for_release(client)
    base = f"/api/plans/{plan['plan_id']}/seal-releases"

    def post(mod):
        body = _release_body(handover["handover_id"])
        body.update(mod)
        return client.post(base, json=body)

    assert post({"release_no": "  "}).status_code == 422
    assert post({"handover_id": " "}).status_code == 422
    assert post({"expected_scan_version": -1}).status_code == 422
    assert post({"cutoff_minute": -1}).status_code == 422
    for key in ("release_no", "handover_id", "expected_scan_version",
                "cutoff_minute"):
        body = _release_body(handover["handover_id"])
        del body[key]
        assert client.post(base, json=body).status_code == 422, key


def test_rejected_attempt_does_not_consume_release_no(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    # 版本错误 -> 409，不占用 REL-R
    bad = _release_body(handover["handover_id"], release_no="REL-R", version=9)
    assert client.post(f"/api/plans/{pid}/seal-releases",
                       json=bad).status_code == 409
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"],
                                        release_no="REL-R"))
    assert ok.status_code == 201


def test_mismatch_then_match_latest_release_ok(client):
    """不符留痕不算通过，但同箱更晚分钟的匹配扫描可放行。"""
    plan, handover = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-BAD", 105))   # 不符留痕
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-3", "C002", "SEAL-002", 110))   # 更晚分钟匹配
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], version=3))
    assert r.status_code == 201, r.text
    c2 = next(c for c in r.json()["containers"] if c["container_no"] == "C002")
    assert c2["scan_no"] == "SC-3" and c2["check_minute"] == 110


# ---------- 凭证不可变：后续扫描/实绩更正/方案取代仅标示失效 ----------


def test_release_invalidated_by_later_scan(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"]))
    assert r.status_code == 201
    rid = r.json()["release_id"]
    frozen_fp = r.json()["scans_fingerprint"]

    # 签发后来一条新扫描（即使内容匹配）：凭证显示失效，内容不变
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-9", "C001", "SEAL-001", 150))
    g = client.get(f"/api/seal-releases/{rid}")
    assert g.status_code == 200
    data = g.json()
    assert data["is_current"] is False
    assert data["invalid_reasons"] == ["scans_changed"]
    assert data["current_scan_version"] == 3
    assert data["scan_version"] == 2
    assert data["scans_fingerprint"] == frozen_fp
    assert data["current_scans_fingerprint"] != frozen_fp
    c1 = next(c for c in data["containers"] if c["container_no"] == "C001")
    assert c1["check_minute"] == 100  # 历史内容不变

    # 同申请号同内容重试仍返回原凭证（200，不重新评估）
    rr = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"]))
    assert rr.status_code == 200
    assert rr.json()["release_id"] == rid
    assert rr.json()["is_current"] is False


def test_release_invalidated_by_actuals_correction(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"]))
    assert r.status_code == 201
    rid = r.json()["release_id"]

    cc = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 4, "expected_version": 1, "request_id": "CORR-9",
        "event": {"seq": 4, "event_type": "finish", "container_no": "C002",
                  "crane_no": 2, "minute": 19},
    })
    assert cc.status_code == 201
    g = client.get(f"/api/seal-releases/{rid}").json()
    assert g["is_current"] is False
    assert g["invalid_reasons"] == ["actuals_changed"]
    assert g["current_actuals_fingerprint"] != g["actuals_fingerprint"]
    # 固化内容不变
    assert g["scan_version"] == 2 and g["handover_no"] == "H-1"


def test_release_invalidated_by_plan_supersession(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"]))
    assert r.status_code == 201
    rid = r.json()["release_id"]

    _supersede(client, pid)
    g = client.get(f"/api/seal-releases/{rid}").json()
    assert g["is_current"] is False
    assert g["invalid_reasons"] == ["plan_not_confirmed"]
    assert g["plan_status"] == "superseded"
    # 历史凭证仍可读且内容不变
    assert g["container_count"] == 2
    lst = client.get(f"/api/plans/{pid}/seal-releases").json()
    assert len(lst) == 1 and lst[0]["is_current"] is False


def test_release_unknown_certificate_404(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    rid = client.post(f"/api/plans/{pid}/seal-releases",
                      json=_release_body(handover["handover_id"])) \
        .json()["release_id"]
    assert client.get("/api/seal-releases/nope").status_code == 404
    # 凭证不属于该方案路径 -> 404
    p2 = _confirm(client, OTHER_SLOTS, OTHER_MANIFEST)
    r = client.get(f"/api/plans/{p2['plan_id']}/seal-releases/{rid}")
    assert r.status_code == 404
    assert client.get(f"/api/seal-releases/{rid}").status_code == 200
    assert client.get(f"/api/plans/{p2['plan_id']}/seal-releases").json() == []


# ---------- 并发：同号请求串行化 ----------


def test_concurrent_same_scan_no_one_wins(client):
    plan, _ = _handover_plan(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    results = []
    barrier = threading.Barrier(2)

    def scan():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-scans",
                       json=_scan("SC-C", "C001", "SEAL-001", 100))
        results.append(r.status_code)

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 201]
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert st["scan_version"] == 1 and len(st["scans"]) == 1


def test_concurrent_same_release_no_one_wins(client):
    plan, handover = _ready_for_release(client)
    pid = plan["plan_id"]
    results = []
    barrier = threading.Barrier(2)

    def release():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-releases",
                       json=_release_body(handover["handover_id"],
                                          release_no="REL-C"))
        results.append(r.status_code)

    threads = [threading.Thread(target=release) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 201]
    assert len(client.get(f"/api/plans/{pid}/seal-releases").json()) == 1


def test_scans_fingerprint_sensitive_to_state(client):
    """扫描指纹须对版本与任一有效扫描内容变化敏感。"""
    from app.services import _scans_fingerprint

    rows = [("C001", "SC-1", "SEAL-001", 100, True),
            ("C002", "SC-2", "SEAL-002", 110, True)]
    fp1 = _scans_fingerprint(2, rows)
    fp_version = _scans_fingerprint(3, rows)
    fp_content = _scans_fingerprint(
        2, [("C001", "SC-1", "SEAL-001", 100, True),
            ("C002", "SC-3", "SEAL-002", 120, True)])
    fp_missing = _scans_fingerprint(
        2, [("C001", "SC-1", "SEAL-001", 100, True),
            ("C002", "", "", -1, False)])
    assert len({fp1, fp_version, fp_content, fp_missing}) == 4
    assert len(fp1) == 64
    int(fp1, 16)
    # 与输入顺序无关
    assert _scans_fingerprint(2, list(reversed(rows))) == fp1
