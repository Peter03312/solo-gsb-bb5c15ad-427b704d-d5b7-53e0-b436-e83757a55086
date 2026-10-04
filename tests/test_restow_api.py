"""应急改配 API 测试。"""

import pytest
from fastapi.testclient import TestClient

from app import services
from app.database import SessionLocal, engine
from app.main import app
from app import models
from app.solver import solve_restow as real_solve_restow


SLOT_PAYLOAD = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "B02", "stack": "B", "tier": 2, "capacity": 100, "arm": 3.0},
        {"code": "C01", "stack": "C", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "C02", "stack": "C", "tier": 2, "capacity": 100, "arm": 3.0},
    ],
    "adjacency": [["A01", "B01"], ["A02", "B02"], ["B01", "C01"], ["B02", "C02"]],
    "incompatible_classes": [["1", "5"]],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 20, "hazmat": None, "discharge_order": 1},
    ]
}


@pytest.fixture
def confirmed_plan(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 200
    return plan["plan_id"], sr, mr


def test_restow_happy_path_minimal_moves(client, confirmed_plan):
    pid, sr, mr = confirmed_plan
    # 原方案：C001@A01, C002@B01；封锁 B01 -> C002 平移 C01，仅 1 箱换槽
    r = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["B01"]})
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["status"] == "draft"
    assert draft["revision_no"] == 2
    assert draft["parent_id"] == pid
    assert draft["root_id"] == pid
    assert draft["slots_revision"] == sr and draft["manifest_revision"] == mr
    assert draft["blocked_slots"] == ["B01"]
    assert draft["moved_boxes"] == 1
    assert draft["max_occupied_tier"] == 1
    mapping = {a["container_no"]: a["slot_code"] for a in draft["assignments"]}
    assert mapping == {"C001": "A01", "C002": "C01"}
    assert draft["moves"] == [{
        "container_no": "C002",
        "from_slot_code": "B01", "to_slot_code": "C01",
        "from_stack": "B", "to_stack": "C",
        "from_tier": 1, "to_tier": 1,
    }]

    # 原确认方案仍有效，占用槽位不变；草稿不产生新占用
    assert client.get(f"/api/plans/{pid}").json()["status"] == "confirmed"
    db = SessionLocal()
    try:
        occupied = {row.slot_code for row in db.query(models.ConfirmedSlot).all()}
        assert occupied == {"A01", "B01"}
    finally:
        db.close()


def test_restow_empty_and_duplicate_blocked_rejected(client, confirmed_plan):
    pid, _, _ = confirmed_plan
    r1 = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": []})
    assert r1.status_code == 422
    r2 = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["A01", "A01"]})
    assert r2.status_code == 422
    r3 = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["  "]})
    assert r3.status_code == 422
    # 被拒后不落库任何新方案
    plans = client.get("/api/plans").json()
    assert len(plans) == 1


def test_restow_unknown_slot_code_422(client, confirmed_plan):
    pid, _, _ = confirmed_plan
    r = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["ZZ9"]})
    assert r.status_code == 422
    assert "不属于" in r.json()["detail"]


def test_restow_infeasible_422_not_saved(client, confirmed_plan):
    pid, _, _ = confirmed_plan
    # 封锁全部一层槽：仅剩二层槽，堆叠支撑约束使任何箱都无法放置 -> 无解
    r = client.post(f"/api/plans/{pid}/restow",
                    json={"blocked_slots": ["A01", "B01", "C01"]})
    assert r.status_code == 422
    assert "无完整可行改配" in r.json()["detail"]
    plans = client.get("/api/plans")
    assert len(plans.json()) == 1


def test_restow_requires_confirmed_plan(client, confirmed_plan):
    pid, sr, mr = confirmed_plan
    # 已有的改配草稿不能再次改配
    draft = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["C02"]})
    assert draft.status_code == 201
    did = draft.json()["plan_id"]
    r = client.post(f"/api/plans/{did}/restow", json={"blocked_slots": ["A01"]})
    assert r.status_code == 409


def test_restow_stale_slot_revision_409(client, confirmed_plan):
    pid, _, mr = confirmed_plan
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)  # 新槽位修订
    r = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["B01"]})
    assert r.status_code == 409
    assert "槽位资料已更新" in r.json()["detail"]


def test_restow_stale_manifest_revision_409(client, confirmed_plan):
    pid, sr, _ = confirmed_plan
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    r = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["B01"]})
    assert r.status_code == 409
    assert "箱清单已更新" in r.json()["detail"]


def test_restow_plan_not_found_404(client):
    r = client.post("/api/plans/nope/restow", json={"blocked_slots": ["A01"]})
    assert r.status_code == 404


def test_restow_draft_can_be_confirmed_and_supersedes(client, confirmed_plan):
    pid, sr, mr = confirmed_plan
    draft = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["B01"]})
    did = draft.json()["plan_id"]

    ok = client.post(f"/api/plans/{did}/confirm",
                     json={"slots_revision": sr, "manifest_revision": mr})
    assert ok.status_code == 200
    assert ok.json()["status"] == "confirmed"
    assert client.get(f"/api/plans/{pid}").json()["status"] == "superseded"
    db = SessionLocal()
    try:
        occupied = {row.slot_code for row in db.query(models.ConfirmedSlot).all()}
        assert occupied == {"A01", "C01"}  # 占用转移，封锁槽 B01 不再占用
    finally:
        db.close()


def test_restow_save_time_recheck_slot_data_changed(client, confirmed_plan, monkeypatch):
    pid, sr, mr = confirmed_plan
    payload = SLOT_PAYLOAD

    def slow_solve(*args, **kwargs):
        # 模拟求解期间槽位资料产生新修订
        db = SessionLocal()
        try:
            rev = models.SlotRevision(
                revision=sr + 1,
                min_moment=payload["min_moment"],
                max_moment=payload["max_moment"],
            )
            for s in payload["slots"]:
                rev.slots.append(models.SlotRow(**s))
            db.add(rev)
            db.commit()
        finally:
            db.close()
        return real_solve_restow(*args, **kwargs)

    monkeypatch.setattr(services, "solve_restow", slow_solve)
    r = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["B01"]})
    assert r.status_code == 409
    assert "槽位资料已产生新修订" in r.json()["detail"]
    # 原确认方案仍有效，无新草稿落库
    assert client.get(f"/api/plans/{pid}").json()["status"] == "confirmed"
    plans = client.get("/api/plans").json()
    assert len(plans) == 1


def test_restow_save_time_recheck_base_superseded(client, confirmed_plan, monkeypatch):
    pid, sr, mr = confirmed_plan

    def slow_solve(*args, **kwargs):
        # 模拟求解期间同根出现更新修订（走正常 amend+confirm 通道）
        local = TestClient(app)
        amend = local.post(f"/api/plans/{pid}/amend", json={}).json()
        ok = local.post(f"/api/plans/{amend['plan_id']}/confirm",
                        json={"slots_revision": sr, "manifest_revision": mr})
        assert ok.status_code == 200
        return real_solve_restow(*args, **kwargs)

    monkeypatch.setattr(services, "solve_restow", slow_solve)
    r = client.post(f"/api/plans/{pid}/restow", json={"blocked_slots": ["B01"]})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert "更新修订" in body or "已为" in body
    # 应急草稿未保存；同根只有原方案(被amend取代)+amend两个
    plans = client.get("/api/plans").json()
    assert len(plans) == 2
