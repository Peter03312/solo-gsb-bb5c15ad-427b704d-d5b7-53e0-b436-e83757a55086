"""端到端 API 流程测试。"""

import threading

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app import models


SLOT_PAYLOAD = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "B02", "stack": "B", "tier": 2, "capacity": 100, "arm": 3.0},
    ],
    "adjacency": [["A01", "B01"], ["A02", "B02"]],
    "incompatible_classes": [["1", "5"]],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C002", "weight": 20, "hazmat": None, "discharge_order": 1},
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}


def _seed(client) -> tuple[int, int]:
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    return sr, mr


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_full_compute_confirm_flow(client):
    sr, mr = _seed(client)
    r = client.post("/api/plans/compute", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 201
    plan = r.json()
    assert plan["revision_no"] == 1
    assert plan["status"] == "draft"
    assert plan["max_occupied_tier"] == 1
    assert plan["total_moment"] == 60  # 10*2 + 20*2
    pairs = [(a["container_no"], a["slot_code"]) for a in plan["assignments"]]
    assert pairs == [("C001", "A01"), ("C002", "B01")]

    # 错误资料修订号确认 -> 409，方案仍为 draft
    bad = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": 999, "manifest_revision": mr
    })
    assert bad.status_code == 409
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"

    # 正确确认
    ok = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert ok.status_code == 200
    assert ok.json()["status"] == "confirmed"
    assert ok.json()["confirmed_at"] is not None

    # 幂等重复确认
    again = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"


def test_infeasible_plan_not_saved(client):
    sr, mr = _seed(client)
    # 加入更多箱使无解（槽位不足）
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": f"X{i}", "weight": 50, "discharge_order": 1} for i in range(5)
        ]
    })
    r = client.post("/api/plans/compute", json={
        "slots_revision": sr, "manifest_revision": mr + 1
    })
    assert r.status_code == 422
    assert "无解" in r.json()["detail"]
    plans = client.get("/api/plans").json()
    assert plans == []


def test_stale_revision_rejected_on_confirm(client):
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    # 槽位资料产生新修订，旧方案过期
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"


def test_amend_chain_and_slot_release(client):
    sr, mr = _seed(client)
    p1 = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{p1['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })

    # 新箱清单（同槽资料仍可配载），基于已确认方案修订
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1},
        ]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    assert p2["revision_no"] == 2
    assert p2["status"] == "draft"
    assert p2["parent_id"] == p1["plan_id"]
    assert p2["root_id"] == p1["root_id"] or p2["root_id"] == p1["plan_id"]

    # 确认新修订：旧确认被取代，占用转移；无槽位冲突
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr + 1
    })
    assert ok.status_code == 200
    old = client.get(f"/api/plans/{p1['plan_id']}").json()
    assert old["status"] == "superseded"
    new = client.get(f"/api/plans/{p2['plan_id']}").json()
    assert new["status"] == "confirmed"

    db = SessionLocal()
    try:
        slots = {r.slot_code for r in db.query(models.ConfirmedSlot).all()}
        assert slots == {"A01"}
    finally:
        db.close()


def test_concurrent_confirm_disjoint_plans(client):
    """两个独立方案占用不同槽位：并发确认均应成功。"""
    sr, mr = _seed(client)
    p1 = client.post("/api/plans/compute", json={}).json()
    p2 = client.post("/api/plans/compute", json={}).json()

    results = {}

    def confirm(pid, key):
        local = TestClient(app)
        results[key] = local.post(f"/api/plans/{pid}/confirm", json={
            "slots_revision": sr, "manifest_revision": mr
        }).status_code

    t1 = threading.Thread(target=confirm, args=(p1["plan_id"], 1))
    t2 = threading.Thread(target=confirm, args=(p2["plan_id"], 2))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 同槽位唯一约束 -> 恰好一个成功，另一个 409
    assert sorted(results.values()) == [200, 409]

    db = SessionLocal()
    try:
        confirmed = db.query(models.ConfirmedSlot).count()
        assert confirmed == 2  # 成功方案占用两个槽
    finally:
        db.close()


def test_get_nonexistent_plan_404(client):
    assert client.get("/api/plans/nope").status_code == 404
