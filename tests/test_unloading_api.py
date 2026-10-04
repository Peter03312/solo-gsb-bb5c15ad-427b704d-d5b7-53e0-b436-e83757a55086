"""逐港卸箱顺序 API 端到端测试。"""

from app import models
from app.database import SessionLocal


# 槽位：A01/A02（臂 -2），B01（臂 +5）；用承重迫使配载
# C001(10,港1)->A02(上)、C002(30,港2)->A01(下)、C003(10,港1)->B01。
# 初始总力矩 = 10*(-2) + 30*(-2) + 10*5 = -30
SLOT_PAYLOAD = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 40, "arm": -2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 15, "arm": -2.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 15, "arm": 5.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 30, "hazmat": None, "discharge_order": 2},
        {"no": "C003", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}


def _seed_and_confirm(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    pairs = {a["container_no"]: a["slot_code"] for a in plan["assignments"]}
    assert pairs == {"C001": "A02", "C002": "A01", "C003": "B01"}
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    return plan


def _ports(**over):
    base = {
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
            {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
        ]
    }
    base.update(over)
    return base


def test_unloading_happy_path_lex_min(client):
    plan = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=_ports())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["plan_id"] == plan["plan_id"]
    assert data["revision_no"] == 1
    assert data["status"] == "current"
    assert data["initial_moment"] == -30 and data["final_moment"] == 0
    seq = [(s["step_no"], s["discharge_order"], s["container_no"],
            s["remaining_moment"]) for s in data["steps"]]
    # 港1两箱 C001/C003 字典序 C001 先：rem -10（C001）、-60（C003）；末港 C002 rem 0
    assert seq == [
        (1, 1, "C001", -10),
        (2, 1, "C003", -60),
        (3, 2, "C002", 0),
    ]
    # 港口区间留档
    assert [(p["discharge_order"], p["min_remaining_moment"],
             p["max_remaining_moment"]) for p in data["ports"]] == [
        (1, -100, 100), (2, -100, 100)
    ]
    # 每步携带方案所用箱重与槽位力臂
    first = data["steps"][0]
    assert first["weight"] == 10 and first["arm"] == -2
    assert first["slot_code"] == "A02" and first["stack"] == "A" and first["tier"] == 2


def test_moment_window_forces_non_greedy_sequence(client):
    plan = _seed_and_confirm(client)
    # 港1 区间 [-100,-30]：C001 先卸 rem=-10 不满足；C003 先卸 rem=-80 满足
    body = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": -30},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]}
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=body)
    assert r.status_code == 201, r.text
    seq = [(s["container_no"], s["remaining_moment"]) for s in r.json()["steps"]]
    assert seq == [("C003", -80), ("C001", -60), ("C002", 0)]


def test_infeasible_windows_rejected_422_not_saved(client):
    plan = _seed_and_confirm(client)
    body = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": 100, "max_remaining_moment": 200},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]}
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=body)
    assert r.status_code == 422
    assert "无可行卸箱顺序" in r.json()["detail"]
    assert client.get("/api/unloading").json() == []


def test_unloading_on_unconfirmed_plan_rejected_409(client):
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=_ports())
    assert r.status_code == 409
    assert "已确认" in r.json()["detail"]


def test_port_set_mismatch_rejected_422(client):
    plan = _seed_and_confirm(client)
    # 缺少卸港 2
    r1 = client.post(f"/api/plans/{plan['plan_id']}/unloading", json={
        "ports": [{"discharge_order": 1, "min_remaining_moment": -100,
                   "max_remaining_moment": 100}]
    })
    assert r1.status_code == 422 and "缺少" in r1.json()["detail"]
    # 多余卸港 9
    r2 = client.post(f"/api/plans/{plan['plan_id']}/unloading", json={
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
            {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
            {"discharge_order": 9, "min_remaining_moment": -100, "max_remaining_moment": 100},
        ]
    })
    assert r2.status_code == 422 and "多余" in r2.json()["detail"]
    assert client.get("/api/unloading").json() == []


def test_duplicate_and_inverted_ports_rejected_422(client):
    plan = _seed_and_confirm(client)
    r1 = client.post(f"/api/plans/{plan['plan_id']}/unloading", json={
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
            {"discharge_order": 1, "min_remaining_moment": -50, "max_remaining_moment": 50},
            {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
        ]
    })
    assert r1.status_code == 422 and "重复" in r1.text
    r2 = client.post(f"/api/plans/{plan['plan_id']}/unloading", json={
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": 100, "max_remaining_moment": -100},
            {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
        ]
    })
    assert r2.status_code == 422 and "倒置" in r2.text


def test_resubmit_atomically_replaces_sequence(client):
    plan = _seed_and_confirm(client)
    client.post(f"/api/plans/{plan['plan_id']}/unloading", json=_ports())
    body = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": -30},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]}
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=body)
    assert r.status_code == 201
    current = client.get("/api/unloading").json()
    assert len(current) == 1
    assert [s["container_no"] for s in current[0]["steps"]] == ["C003", "C001", "C002"]
    # 同一方案修订只保留一条
    db = SessionLocal()
    try:
        assert db.query(models.UnloadingSequence).count() == 1
        assert db.query(models.UnloadingStepRow).count() == 3
    finally:
        db.close()


def test_old_revision_sequence_turns_historical_and_remains_queryable(client):
    p1 = _seed_and_confirm(client)
    client.post(f"/api/plans/{p1['plan_id']}/unloading", json=_ports())

    # 新箱清单修订 -> 修订并确认 p2（同根），p1 变 superseded
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    # 当前列表不含旧顺序
    assert client.get("/api/unloading").json() == []
    # 旧顺序仍可按方案查询，标记 historical
    old = client.get(f"/api/plans/{p1['plan_id']}/unloading")
    assert old.status_code == 200
    assert old.json()["status"] == "historical"
    # 含历史参数可列出
    allseq = client.get("/api/unloading?historical=true").json()
    assert [s["plan_id"] for s in allseq] == [p1["plan_id"]]
    assert allseq[0]["status"] == "historical"


def test_get_unloading_404(client):
    plan = _seed_and_confirm(client)
    assert client.get(f"/api/plans/{plan['plan_id']}/unloading").status_code == 404
    assert client.get("/api/plans/nope/unloading").status_code == 404


def test_save_time_recheck_rejects_if_plan_superseded(client, monkeypatch):
    """求解完成后、落库前方案被并发取代：锁内复核须拒绝且不保存。"""
    from app import services

    p1 = _seed_and_confirm(client)
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()

    real_solve = services.solve_unloading

    def raced_solve(boxes, windows):
        result = real_solve(boxes, windows)
        client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
            "slots_revision": 1, "manifest_revision": 2
        })
        return result

    monkeypatch.setattr(services, "solve_unloading", raced_solve)
    r = client.post(f"/api/plans/{p1['plan_id']}/unloading", json=_ports())
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert client.get("/api/unloading?historical=true").json() == []


def test_empty_plan_unloading_sequence(client):
    client.post("/api/slot-revisions", json={
        "slots": [{"code": "A01", "stack": "A", "tier": 1, "capacity": 100,
                   "arm": 2.0}],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    })
    client.post("/api/manifest-revisions", json={"containers": []})
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 1
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json={"ports": []})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["steps"] == [] and data["ports"] == []
    assert data["initial_moment"] == 0 and data["final_moment"] == 0
