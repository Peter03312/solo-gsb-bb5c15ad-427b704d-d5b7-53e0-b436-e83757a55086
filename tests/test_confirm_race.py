"""方案确认与资料更新竞态的回归测试。

修复前：确认在读取最新槽位/箱清单修订**之后**才取得写锁，新资料恰在
两步之间提交时，基于过期资料的旧草稿仍可能被确认。
修复后：确认在 `BEGIN IMMEDIATE` 写锁内按确认与资料更新的实际提交先后，
重新核对“方案所用修订 == 提交修订 == 当时最新修订”，三者一致才确认。
"""

import threading

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models, services
from app.database import SessionLocal
from app.main import app
from app.schemas import ManifestIn, SlotDataIn

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


def _inject_revision_in_race_window(monkeypatch, kind: str):
    """在确认“释放只读事务”与“获取写锁”之间提交一笔新资料修订。

    confirm_plan 在锁外预检后调用 ``db.rollback()`` 释放只读事务，随后才
    以 BEGIN IMMEDIATE 拿写锁；在此补丁 rollback 返回前注入新修订，恰好
    模拟“新资料在两步之间提交”的竞态窗口。
    """
    original_rollback = Session.rollback
    injected = False

    def rollback_then_inject(self):
        nonlocal injected
        original_rollback(self)
        if injected:
            return
        injected = True
        s = SessionLocal()
        try:
            if kind == "slots":
                services.create_slot_revision(s, SlotDataIn(**SLOT_PAYLOAD))
            else:
                services.create_manifest_revision(s, ManifestIn(**MANIFEST_PAYLOAD))
        finally:
            s.close()

    monkeypatch.setattr(Session, "rollback", rollback_then_inject)


def _confirmed_slots() -> dict[str, str]:
    db = SessionLocal()
    try:
        return {r.slot_code: r.plan_id for r in db.query(models.ConfirmedSlot).all()}
    finally:
        db.close()


def test_confirm_rejects_slot_revision_committed_in_race_window(client, monkeypatch):
    """锁外预检到拿锁之间提交了新槽位资料：确认被拒绝（409），草稿不变。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    _inject_revision_in_race_window(monkeypatch, "slots")
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    # 草稿、槽位占用均不变
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"
    assert _confirmed_slots() == {}


def test_confirm_rejects_manifest_revision_committed_in_race_window(
    client, monkeypatch
):
    """竞态窗口内提交了新箱清单：确认被拒绝（409），草稿不变。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    _inject_revision_in_race_window(monkeypatch, "manifest")
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "draft"
    assert _confirmed_slots() == {}


def test_race_reject_keeps_chain_confirmed_plan_and_slots(client, monkeypatch):
    """竞态拒绝时：同链旧已确认方案保持 confirmed，槽位占用不转移。"""
    sr, mr = _seed(client)
    p1 = client.post("/api/plans/compute", json={}).json()
    ok = client.post(f"/api/plans/{p1['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert ok.status_code == 200
    # 同链新草稿（仍基于修订 1/1）
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    assert p2["status"] == "draft"

    _inject_revision_in_race_window(monkeypatch, "slots")
    r = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 409
    assert "已过期" in r.json()["detail"]
    # 新草稿仍为草稿；旧确认方案未被取代；槽位占用仍属旧方案
    assert client.get(f"/api/plans/{p2['plan_id']}").json()["status"] == "draft"
    assert client.get(f"/api/plans/{p1['plan_id']}").json()["status"] == "confirmed"
    assert _confirmed_slots() == {"A01": p1["plan_id"], "B01": p1["plan_id"]}


def test_confirm_committed_first_not_revoked_by_later_revision(client):
    """确认先提交则成功：之后的资料更新不追溯撤销，原样重复确认仍幂等。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()
    ok = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert ok.status_code == 200

    # 确认后资料更新（产生修订 2）
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)

    # 已确认方案不追溯撤销
    got = client.get(f"/api/plans/{plan['plan_id']}").json()
    assert got["status"] == "confirmed"
    assert _confirmed_slots() == {
        "A01": plan["plan_id"], "B01": plan["plan_id"]
    }

    # 同一已确认方案的原样重复确认保持幂等（即使资料已更新）
    again = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert again.status_code == 200
    assert again.json()["status"] == "confirmed"
    assert _confirmed_slots() == {
        "A01": plan["plan_id"], "B01": plan["plan_id"]
    }


def test_concurrent_identical_confirm_is_idempotent(client):
    """并发同内容确认同一方案：均成功（200），槽位只被占用一次。"""
    sr, mr = _seed(client)
    plan = client.post("/api/plans/compute", json={}).json()

    results = {}

    def confirm(key):
        local = TestClient(app)
        results[key] = local.post(f"/api/plans/{plan['plan_id']}/confirm", json={
            "slots_revision": sr, "manifest_revision": mr
        }).status_code

    threads = [threading.Thread(target=confirm, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [200, 200]
    assert client.get(f"/api/plans/{plan['plan_id']}").json()["status"] == "confirmed"
    # 槽位不重复占用：恰好两个槽位、均属该方案
    assert _confirmed_slots() == {
        "A01": plan["plan_id"], "B01": plan["plan_id"]
    }
