"""封签损坏换签与放行联动端到端测试。"""

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


def _confirm(client):
    sr = client.post("/api/slot-revisions", json=STACK_SLOTS).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=STACK_MANIFEST) \
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


def _ready(client, cutoff=30):
    """已确认 + 排程 + 完工实绩 + 交接凭证 + 封签清单（返回 plan/handover）。"""
    plan = _confirm(client)
    pid = plan["plan_id"]
    assert client.post(f"/api/plans/{pid}/schedules", json={
        "crane_count": 2,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
            {"no": "C002", "duration": 8, "release_minute": 0,
             "deadline_minute": 100},
        ],
    }).status_code == 201
    assert client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 11),
        _ev(4, "finish", "C002", 2, 20),
    ]}).status_code == 201
    h = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": "H-1",
        "cutoff_minute": 30,
        "expected_actual_version": 1,
        "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    })
    assert h.status_code == 201, h.text
    r = client.post(f"/api/plans/{pid}/seal-manifest", json={"items": [
        {"container_no": "C001", "seal_no": "SEAL-001"},
        {"container_no": "C002", "seal_no": "SEAL-002"},
    ]})
    assert r.status_code == 201, r.text
    return plan, h.json()


def _scan(scan_no, cno, seal, minute):
    return {"scan_no": scan_no, "container_no": cno,
            "seal_no": seal, "check_minute": minute}


def _repl(repl_no, cno, old, new, minute, reason="封签损坏", version=0):
    return {"replacement_no": repl_no, "container_no": cno,
            "old_seal_no": old, "new_seal_no": new,
            "replace_minute": minute, "reason": reason,
            "expected_seal_version": version}


def _release_body(handover_id, release_no, version, cutoff=200):
    return {"release_no": release_no, "handover_id": handover_id,
            "expected_scan_version": version, "cutoff_minute": cutoff}


def _container(client, pid, cno):
    st = client.get(f"/api/plans/{pid}/seal-scans").json()
    return next(c for c in st["containers"] if c["container_no"] == cno), st


# ---------- 登记与校验 ----------

def test_replacement_success_advances_seal_version(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seal_version"] == 1
    assert body["expected_seal_version"] == 0
    assert body["old_seal_no"] == "SEAL-001"
    assert body["new_seal_no"] == "SEAL-901"
    assert body["scan_id_at_replace"] == 0
    assert body["previous_replacement_id"] is None

    manifest = client.get(f"/api/plans/{pid}/seal-manifest").json()
    # 原清单内容不变，仅新增换签版本元数据
    assert manifest["seal_version"] == 1
    items = {i["container_no"]: i["expected_seal_no"] for i in manifest["items"]}
    assert items == {"C001": "SEAL-001", "C002": "SEAL-002"}

    chain = client.get(f"/api/plans/{pid}/seal-replacements").json()
    assert chain["seal_version"] == 1
    assert len(chain["replacements"]) == 1
    c001 = next(c for c in chain["containers"] if c["container_no"] == "C001")
    assert c001["current_expected_seal_no"] == "SEAL-901"
    assert c001["expected_seal_no"] == "SEAL-001"
    assert c001["replaced"] is True
    assert c001["last_replace_minute"] == 120


def test_replacement_rejects_missing_manifest(client: TestClient):
    plan = _confirm(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    assert r.status_code == 409
    assert "尚未登记预期封签清单" in r.json()["detail"]


def test_replacement_rejects_unknown_container(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C999", "SEAL-001", "SEAL-901", 120))
    assert r.status_code == 422
    assert "未知箱号" in r.json()["detail"]


def test_replacement_rejects_wrong_old_seal(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-XXX", "SEAL-901", 120))
    assert r.status_code == 422
    assert "原号" in r.json()["detail"] or "不符" in r.json()["detail"]


def test_replacement_rejects_empty_reason_and_negative_minute(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120,
                               reason="   "))
    assert r.status_code == 422
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", -1))
    assert r.status_code == 422


def test_replacement_rejects_same_as_old(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-001", 120))
    assert r.status_code == 422
    assert "新号不得等于旧号" in r.json()["detail"]


def test_replacement_rejects_duplicate_of_other_current_seal(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-002", 120))
    assert r.status_code == 422
    assert "其他箱" in r.json()["detail"]


def test_replacement_rejects_stale_expected_version(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    r1 = client.post(f"/api/plans/{pid}/seal-replacements",
                     json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    assert r1.status_code == 201
    r2 = client.post(f"/api/plans/{pid}/seal-replacements",
                     json=_repl("REPL-2", "C002", "SEAL-002", "SEAL-902", 121,
                                version=0))
    assert r2.status_code == 409
    assert "换签版本" in r2.json()["detail"]
    # 版本纠正后成功
    r3 = client.post(f"/api/plans/{pid}/seal-replacements",
                     json=_repl("REPL-2", "C002", "SEAL-002", "SEAL-902", 121,
                                version=1))
    assert r3.status_code == 201
    assert r3.json()["seal_version"] == 2


def test_replacement_rejects_minute_before_last_for_same_container(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-2", "C001", "SEAL-901", "SEAL-902", 119,
                               version=1))
    assert r.status_code == 422
    assert "早于该箱上次换签" in r.json()["detail"]
    # 同分钟允许（不早于）
    r2 = client.post(f"/api/plans/{pid}/seal-replacements",
                     json=_repl("REPL-2", "C001", "SEAL-901", "SEAL-902", 120,
                                version=1))
    assert r2.status_code == 201
    assert r2.json()["previous_replacement_id"] == r2.json()["replacement_id"] - 1


def test_replacement_chain_per_container(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-2", "C001", "SEAL-901", "SEAL-902", 130,
                           version=1))
    r = client.get(f"/api/plans/{pid}/seal-replacements",
                   params={"container_no": "C001"})
    assert r.status_code == 200
    body = r.json()
    assert [x["replacement_no"] for x in body["replacements"]] == [
        "REPL-1", "REPL-2"
    ]
    first, second = body["replacements"]
    assert first["previous_replacement_id"] is None
    assert second["previous_replacement_id"] == first["replacement_id"]
    c001 = next(c for c in body["containers"] if c["container_no"] == "C001")
    assert c001["current_expected_seal_no"] == "SEAL-902"
    # 未知箱过滤参数 -> 404
    r2 = client.get(f"/api/plans/{pid}/seal-replacements",
                    params={"container_no": "NOPE"})
    assert r2.status_code == 404


def test_replacement_rejected_on_superseded_plan(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    # 新修订确认后取代当前方案（同槽位修订，改箱清单）
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1},
            {"no": "C002", "weight": 10, "hazmat": None, "discharge_order": 1},
        ]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    assert client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    }).status_code == 200
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    assert r.status_code == 409
    assert "取代" in r.json()["detail"]


# ---------- 幂等 ----------

def test_replacement_idempotent_same_content(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    body = _repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120)
    r1 = client.post(f"/api/plans/{pid}/seal-replacements", json=body)
    r2 = client.post(f"/api/plans/{pid}/seal-replacements", json=body)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    chain = client.get(f"/api/plans/{pid}/seal-replacements").json()
    assert len(chain["replacements"]) == 1
    assert chain["seal_version"] == 1


def test_replacement_different_content_conflict(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    # 同申请号异内容（新号不同）
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C002", "SEAL-002", "SEAL-902", 121,
                               version=1))
    assert r.status_code == 409
    chain = client.get(f"/api/plans/{pid}/seal-replacements").json()
    assert len(chain["replacements"]) == 1  # 失败不留记录
    assert chain["seal_version"] == 1


# ---------- 与扫描、放行的联动 ----------

def test_old_scan_after_replacement_not_passing(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    # 换签前扫描 C001（匹配旧号）
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("SC-1", "C001", "SEAL-001", 100))
    assert r.status_code == 201
    assert r.json()["matched"] is True
    # 换签
    r = client.post(f"/api/plans/{pid}/seal-replacements",
                    json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    assert r.status_code == 201
    assert r.json()["scan_id_at_replace"] == 1
    # 旧扫描仍定格 matched=true，但不再是放行有效扫描、该箱不通过
    c001, st = _container(client, pid, "C001")
    assert c001["passed"] is False
    assert c001["scanned"] is False
    old = next(s for s in st["scans"] if s["scan_no"] == "SC-1")
    assert old["matched"] is True
    assert old["effective"] is True
    assert old["eligible"] is False
    assert old["current_expected_seal_no"] == "SEAL-901"


def test_new_scan_before_replace_minute_not_eligible(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    # 先有一条旧扫描（换签点 = 1）
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 150))
    # 新登记（id>换签点）但检查分钟早于换签分钟 -> 不合格
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("SC-2", "C001", "SEAL-901", 140))
    assert r.status_code == 201
    assert r.json()["matched"] is True
    c001, st = _container(client, pid, "C001")
    assert c001["passed"] is False
    sc2 = next(s for s in st["scans"] if s["scan_no"] == "SC-2")
    assert sc2["eligible"] is False


def test_release_requires_new_scan_after_replacement(client: TestClient):
    plan, handover = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-002", 110))
    # 换签 C001；此时扫描版本仍为 2，但 C001 只有旧扫描
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], "REL-1", 2))
    assert r.status_code == 422
    body = r.json()
    assert body["missing_scans"] == ["C001"]
    assert "换签后" in body["detail"]

    # 换签后新扫描，检查分钟不早于换签分钟，匹配新号 -> 通过
    assert client.post(f"/api/plans/{pid}/seal-scans",
                       json=_scan("SC-3", "C001", "SEAL-901", 130)).status_code == 201
    r2 = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(handover["handover_id"], "REL-1", 3))
    assert r2.status_code == 201, r2.text
    out = r2.json()
    assert out["seal_version"] == 1
    c001 = next(c for c in out["containers"] if c["container_no"] == "C001")
    assert c001["expected_seal_no"] == "SEAL-901"
    assert c001["scan_no"] == "SC-3"
    assert out["is_current"] is True


def test_release_invalidated_by_subsequent_replacement(client: TestClient):
    plan, handover = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-002", 110))
    rel = client.post(
        f"/api/plans/{pid}/seal-releases",
        json=_release_body(handover["handover_id"], "REL-1", 2),
    )
    assert rel.status_code == 201
    release_id = rel.json()["release_id"]

    # 签发后换签 C001（即使未产生新扫描）
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    got = client.get(f"/api/seal-releases/{release_id}").json()
    # 签发快照内容保留
    assert got["seal_version"] == 0
    assert got["current_seal_version"] == 1
    assert got["is_current"] is False
    assert "seals_replaced" in got["invalid_reasons"]
    assert got["containers"][0]["expected_seal_no"] == "SEAL-001"


def test_release_uses_sorting_rule_among_post_replacement_scans(client: TestClient):
    plan, handover = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-0", "C001", "SEAL-001", 90))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-002", 110))
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    # 两条换签后新扫描：较早一条不符（留痕），较晚一条匹配 -> 排序胜出者匹配
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-3", "C001", "SEAL-TAMPER", 125))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-4", "C001", "SEAL-901", 130))
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(handover["handover_id"], "REL-1", 4))
    assert r.status_code == 201, r.text
    c001 = next(c for c in r.json()["containers"] if c["container_no"] == "C001")
    assert c001["scan_no"] == "SC-4"
    # 若较晚扫描不符，则即便较早匹配也拒绝（排序规则：较晚者胜）
    plan2, h2 = _plan2_ready(client)
    pid2 = plan2["plan_id"]
    client.post(f"/api/plans/{pid2}/seal-scans",
                json=_scan("T-0", "C001", "SEAL-001", 90))
    client.post(f"/api/plans/{pid2}/seal-scans",
                json=_scan("T-2", "C002", "SEAL-002", 110))
    client.post(f"/api/plans/{pid2}/seal-replacements",
                json=_repl("RX-1", "C001", "SEAL-001", "SEAL-901", 120))
    client.post(f"/api/plans/{pid2}/seal-scans",
                json=_scan("T-3", "C001", "SEAL-901", 125))
    client.post(f"/api/plans/{pid2}/seal-scans",
                json=_scan("T-4", "C001", "SEAL-TAMPER", 130))
    r2 = client.post(f"/api/plans/{pid2}/seal-releases",
                     json=_release_body(h2["handover_id"], "REL-X", 4))
    assert r2.status_code == 422
    assert r2.json()["mismatched_containers"][0]["container_no"] == "C001"


def _plan2_ready(client: TestClient):
    """复用独立槽位的第二套就绪方案（避免槽位占用冲突）。"""
    slots = {
        "slots": [
            {"code": "C01", "stack": "C", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "D01", "stack": "D", "tier": 1, "capacity": 100, "arm": 3.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    sr = client.post("/api/slot-revisions", json=slots).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=STACK_MANIFEST) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    assert client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    }).status_code == 200
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/schedules", json={
        "crane_count": 2,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
            {"no": "C002", "duration": 8, "release_minute": 0,
             "deadline_minute": 100},
        ],
    })
    client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C002", 2, 11),
        _ev(4, "finish", "C002", 2, 20),
    ]})
    h_resp = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": "H-2", "cutoff_minute": 60,
        "expected_actual_version": 1, "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 30,
    })
    assert h_resp.status_code == 201, h_resp.text
    h = h_resp.json()
    client.post(f"/api/plans/{pid}/seal-manifest", json={"items": [
        {"container_no": "C001", "seal_no": "SEAL-001"},
        {"container_no": "C002", "seal_no": "SEAL-002"},
    ]})
    return plan, h


def test_new_scan_matches_updated_expected_seal(client: TestClient):
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-replacements",
                json=_repl("REPL-1", "C001", "SEAL-001", "SEAL-901", 120))
    # 扫描新号 -> matched；扫旧号 -> matched=false（留痕）
    r_ok = client.post(f"/api/plans/{pid}/seal-scans",
                       json=_scan("SC-1", "C001", "SEAL-901", 125))
    r_bad = client.post(f"/api/plans/{pid}/seal-scans",
                        json=_scan("SC-2", "C001", "SEAL-001", 126))
    assert r_ok.json()["matched"] is True
    assert r_ok.json()["expected_seal_no"] == "SEAL-901"
    assert r_bad.json()["matched"] is False


def test_concurrent_replacements_version_serialized(client: TestClient):
    """并发换签：BEGIN IMMEDIATE 串行化，恰好一次成功，其余因版本过期拒绝。"""
    plan, _h = _ready(client)
    pid = plan["plan_id"]
    n = 4
    results = []
    barrier = threading.Barrier(n)

    def replace(idx):
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-replacements", json=_repl(
            f"REPL-C{idx}", "C001", "SEAL-001", f"SEAL-90{idx}",
            120 + idx, version=0,
        ))
        results.append(r.status_code)

    threads = [threading.Thread(target=replace, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 只有一个能以期望版本 0 成功；其余均 409（版本过期），无 5xx
    assert results.count(201) == 1
    assert sorted(set(results)) == [201, 409]
    chain = client.get(f"/api/plans/{pid}/seal-replacements").json()
    assert chain["seal_version"] == 1
    assert len(chain["replacements"]) == 1


def test_concurrent_replacement_and_release_atomic(client: TestClient):
    """并发换签与放行：换签与签发在写事务内原子核对当前状态，结果一致无 5xx。"""
    plan, handover = _ready(client)
    pid = plan["plan_id"]
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-1", "C001", "SEAL-001", 100))
    client.post(f"/api/plans/{pid}/seal-scans",
                json=_scan("SC-2", "C002", "SEAL-002", 110))
    outcome = {}
    barrier = threading.Barrier(2)

    def replace():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-replacements",
                       json=_repl("REPL-Z", "C001", "SEAL-001", "SEAL-901", 120))
        outcome["repl"] = r.status_code

    def release():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-releases",
                       json=_release_body(handover["handover_id"], "REL-Z", 2))
        outcome["rel"] = r.status_code

    for fn in (replace, release):
        threading.Thread(target=fn).start()
    import time
    time.sleep(1.0)
    assert set(outcome) == {"repl", "rel"}
    assert outcome["repl"] == 201
    # 若换签先提交：放行须拒绝（C001 缺换签后合格扫描 422）；
    # 若放行先提交：凭证已签发且随后标示 seals_replaced。
    if outcome["rel"] == 201:
        rel_id = client.get(
            f"/api/plans/{pid}/seal-releases"
        ).json()[0]["release_id"]
        got = client.get(f"/api/seal-releases/{rel_id}").json()
        assert got["is_current"] is False
        assert "seals_replaced" in got["invalid_reasons"]
    else:
        assert outcome["rel"] == 422
