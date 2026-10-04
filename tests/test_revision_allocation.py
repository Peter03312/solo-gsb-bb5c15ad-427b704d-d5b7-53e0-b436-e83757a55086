"""资料修订号分配的并发、原子性与启动冲突检查回归测试。

修复前：录入先 SELECT MAX(revision) 再 INSERT，两个并发成功请求可能拿到
同一修订号；随后按号查询、核算或确认引用的资料不确定。
修复后：修订号分配与整份资料在同一 BEGIN IMMEDIATE 写事务内落库——
每次成功提交在本类别取得唯一且递增的修订号；失败整体回滚不留半份资料；
历史库若已存在同号多份资料，启动时明确报出冲突修订号并拒绝启动，
不能任取其一。
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app import models, services
from app.database import (
    RevisionConflictError,
    SessionLocal,
    engine,
    find_duplicate_revisions,
    init_db,
)
from app.main import app
from app.schemas import ContainerIn, ManifestIn, SlotDataIn, SlotIn


def _slot_payload(tag: str, min_moment: float = -100000) -> dict:
    return {
        "slots": [
            {"code": f"{tag}1", "stack": tag, "tier": 1,
             "capacity": 100, "arm": 2.0},
            {"code": f"{tag}2", "stack": tag, "tier": 2,
             "capacity": 100, "arm": 3.0},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": min_moment,
        "max_moment": 100000,
    }


def _manifest_payload(tag: str) -> dict:
    return {
        "containers": [
            {"no": f"{tag}02", "weight": 20, "hazmat": None,
             "discharge_order": 1},
            {"no": f"{tag}01", "weight": 10, "hazmat": None,
             "discharge_order": 1},
        ]
    }


def _run_threads(n, fn):
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------- 并发录入：唯一且递增 ----------


def test_concurrent_slot_revisions_unique_increasing(client):
    """并发录入槽位资料：全部成功，修订号恰为 1..N 且按号查回同一份资料。"""
    n = 8
    results = {}

    def submit(i):
        local = TestClient(app)
        r = local.post("/api/slot-revisions", json=_slot_payload(f"S{i}"))
        results[i] = (r.status_code, r.json())

    _run_threads(n, submit)

    assert all(sc == 201 for sc, _ in results.values())
    revisions = [body["slots_revision"] for _, body in results.values()]
    assert sorted(revisions) == list(range(1, n + 1))
    # 按号查询指向同一份资料：每个修订号查回的内容即该线程提交的内容
    for i, (_, body) in results.items():
        got = client.get(f"/api/slot-revisions/{body['slots_revision']}")
        assert got.status_code == 200
        doc = got.json()
        assert doc["slots_revision"] == body["slots_revision"]
        assert {s["code"] for s in doc["slots"]} == {f"S{i}1", f"S{i}2"}


def test_concurrent_manifest_revisions_unique_increasing(client):
    """并发录入箱清单：全部成功，修订号恰为 1..N 且按号查回同一份清单。"""
    n = 8
    results = {}

    def submit(i):
        local = TestClient(app)
        r = local.post("/api/manifest-revisions", json=_manifest_payload(f"M{i}"))
        results[i] = (r.status_code, r.json())

    _run_threads(n, submit)

    assert all(sc == 201 for sc, _ in results.values())
    revisions = [body["manifest_revision"] for _, body in results.values()]
    assert sorted(revisions) == list(range(1, n + 1))
    for i, (_, body) in results.items():
        got = client.get(f"/api/manifest-revisions/{body['manifest_revision']}")
        assert got.status_code == 200
        doc = got.json()
        assert doc["manifest_revision"] == body["manifest_revision"]
        assert {c["no"] for c in doc["containers"]} == {f"M{i}01", f"M{i}02"}


def test_slot_and_manifest_sequences_are_independent(client):
    """两类资料各自独立编号、互不影响。"""
    s1 = client.post("/api/slot-revisions", json=_slot_payload("A"))
    m1 = client.post("/api/manifest-revisions", json=_manifest_payload("B"))
    s2 = client.post("/api/slot-revisions", json=_slot_payload("C"))
    assert s1.json()["slots_revision"] == 1
    assert m1.json()["manifest_revision"] == 1
    assert s2.json()["slots_revision"] == 2


# ---------- 原子性：失败不留半份资料 ----------


def test_failed_submission_leaves_no_partial_document(client, db):
    """库级约束失败（力矩区间倒置）时整体回滚：修订头与子表均无残留，
    且未占用修订号（后续成功提交仍取下一号）。"""
    bad = SlotDataIn.model_construct(
        slots=[SlotIn(code="A01", stack="A", tier=1, capacity=100, arm=2.0)],
        adjacency=[],
        incompatible_classes=[],
        min_moment=10.0,
        max_moment=5.0,
    )
    with pytest.raises(sqlite3.IntegrityError):
        services.create_slot_revision(db, bad)

    assert db.query(models.SlotRevision).count() == 0
    assert db.query(models.SlotRow).count() == 0

    ok = client.post("/api/slot-revisions", json=_slot_payload("Z"))
    assert ok.status_code == 201
    assert ok.json()["slots_revision"] == 1


def test_concurrent_failure_leaves_no_partial_and_others_succeed(client):
    """并发中一笔失败（库级约束）：失败整体回滚，其余成功提交照常取号。"""
    n = 4
    results = {}
    errors = {}

    def submit_ok(i):
        local = TestClient(app)
        r = local.post(
            "/api/manifest-revisions", json=_manifest_payload(f"K{i}")
        )
        results[i] = (r.status_code, r.json())

    def submit_bad():
        s = SessionLocal()
        try:
            bad = ManifestIn.model_construct(containers=[
                ContainerIn.model_construct(
                    no="BAD", weight=-5, hazmat=None, discharge_order=1
                )
            ])
            services.create_manifest_revision(s, bad)
        except Exception as exc:  # noqa: BLE001 - 断言落库无残留即可
            errors["bad"] = exc
        finally:
            s.close()

    threads = [threading.Thread(target=submit_ok, args=(i,)) for i in range(n)]
    threads.append(threading.Thread(target=submit_bad))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert "bad" in errors  # 失败提交确实报错
    assert all(sc == 201 for sc, _ in results.values())
    revisions = [body["manifest_revision"] for _, body in results.values()]
    assert sorted(revisions) == list(range(1, n + 1))

    s = SessionLocal()
    try:
        assert s.query(models.ManifestRevision).count() == n
        # 失败提交不留半份清单
        assert s.query(models.ContainerRow).filter_by(no="BAD").count() == 0
    finally:
        s.close()


def test_fresh_db_enforces_unique_revision(client):
    """新库由表级唯一约束兜底：同号直接插入被拒绝。"""
    r = client.post("/api/slot-revisions", json=_slot_payload("A"))
    assert r.status_code == 201
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO slot_revisions "
                    "(revision, min_moment, max_moment, created_at) "
                    "VALUES (1, 0, 1, '2024-01-01 00:00:00')"
                )
            )


# ---------- 按号查询/核算/确认指向同一份资料 ----------


def test_compute_and_confirm_reference_exact_revision(client):
    """按号核算指向指定修订；确认仍只接受当时最新修订（过期 409）。"""
    client.post("/api/slot-revisions", json=_slot_payload("A", min_moment=-100000))
    client.post("/api/manifest-revisions", json=_manifest_payload("B"))
    # 第二份槽位资料（不同力矩区间）
    client.post("/api/slot-revisions", json=_slot_payload("A", min_moment=-50000))

    # 按号查询：修订 1 与修订 2 是各自提交的那份资料
    doc1 = client.get("/api/slot-revisions/1").json()
    doc2 = client.get("/api/slot-revisions/2").json()
    assert doc1["min_moment"] == -100000
    assert doc2["min_moment"] == -50000

    # 按号核算：显式指定修订 1
    plan = client.post(
        "/api/plans/compute",
        json={"slots_revision": 1, "manifest_revision": 1},
    )
    assert plan.status_code == 201
    assert plan.json()["slots_revision"] == 1
    assert plan.json()["manifest_revision"] == 1

    # 确认仍只接受当时最新修订：最新槽位修订为 2，提交 1 -> 409
    stale = client.post(
        f"/api/plans/{plan.json()['plan_id']}/confirm",
        json={"slots_revision": 1, "manifest_revision": 1},
    )
    assert stale.status_code == 409
    assert "已过期" in stale.json()["detail"]

    # 按最新修订核算并确认 -> 200
    plan2 = client.post(
        "/api/plans/compute",
        json={"slots_revision": 2, "manifest_revision": 1},
    )
    assert plan2.status_code == 201
    ok = client.post(
        f"/api/plans/{plan2.json()['plan_id']}/confirm",
        json={"slots_revision": 2, "manifest_revision": 1},
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "confirmed"


# ---------- 启动冲突检查：同号多份资料明确报出 ----------


def _build_legacy_db(path, slot_revisions, manifest_revisions):
    """模拟旧版库：修订号列无唯一约束，且可能已存在同号多份资料。"""
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE slot_revisions ("
        "id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, "
        "min_moment FLOAT NOT NULL, max_moment FLOAT NOT NULL, "
        "created_at DATETIME)"
    )
    cur.execute(
        "CREATE TABLE manifest_revisions ("
        "id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, "
        "created_at DATETIME)"
    )
    for rev in slot_revisions:
        cur.execute(
            "INSERT INTO slot_revisions (revision, min_moment, max_moment, "
            "created_at) VALUES (?, 0, 0, '2024-01-01 00:00:00')",
            (rev,),
        )
    for rev in manifest_revisions:
        cur.execute(
            "INSERT INTO manifest_revisions (revision, created_at) "
            "VALUES (?, '2024-01-01 00:00:00')",
            (rev,),
        )
    conn.commit()
    conn.close()


def test_find_duplicate_revisions_reports_all_conflicts(tmp_path):
    """冲突检查按类别列出全部同号修订（槽位 2、3；箱清单 1）。"""
    path = tmp_path / "legacy.db"
    _build_legacy_db(path, slot_revisions=[1, 2, 2, 3, 3],
                     manifest_revisions=[1, 1])
    legacy_engine = create_engine(f"sqlite:///{path}")
    try:
        problems = find_duplicate_revisions(legacy_engine)
        assert len(problems) == 2
        slot_problem = next(p for p in problems if "槽位资料" in p)
        manifest_problem = next(p for p in problems if "箱清单" in p)
        assert "2" in slot_problem and "3" in slot_problem
        assert "1" in manifest_problem
    finally:
        legacy_engine.dispose()


def test_init_db_refuses_to_start_on_conflicts(tmp_path, monkeypatch):
    """历史库存在同号多份资料：init_db 明确报出冲突修订号并拒绝启动。"""
    path = tmp_path / "legacy.db"
    _build_legacy_db(path, slot_revisions=[1, 2, 2], manifest_revisions=[3, 3])
    legacy_engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr("app.database.engine", legacy_engine)
    try:
        with pytest.raises(RevisionConflictError) as exc_info:
            init_db()
        msg = str(exc_info.value)
        assert "槽位资料" in msg and "2" in msg
        assert "箱清单" in msg and "3" in msg
    finally:
        legacy_engine.dispose()


def test_legacy_db_without_conflicts_gets_unique_index(tmp_path, monkeypatch):
    """无冲突的历史库：正常启动并补建唯一索引，此后同号插入被拒绝。"""
    path = tmp_path / "legacy.db"
    _build_legacy_db(path, slot_revisions=[1, 2], manifest_revisions=[1])
    legacy_engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr("app.database.engine", legacy_engine)
    try:
        init_db()  # 不抛异常
        assert find_duplicate_revisions(legacy_engine) == []
        # 唯一索引已兜底：同号插入违反约束
        with pytest.raises(IntegrityError):
            with legacy_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO slot_revisions "
                        "(revision, min_moment, max_moment, created_at) "
                        "VALUES (2, 0, 0, '2024-01-01 00:00:00')"
                    )
                )
        with pytest.raises(IntegrityError):
            with legacy_engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO manifest_revisions (revision, created_at) "
                        "VALUES (1, '2024-01-01 00:00:00')"
                    )
                )
        # 幂等：再次初始化仍正常
        init_db()
    finally:
        legacy_engine.dispose()
