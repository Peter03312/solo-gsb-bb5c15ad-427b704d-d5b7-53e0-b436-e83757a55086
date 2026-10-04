"""资料修订号并发分配的回归测试。

修复前：录入先读最大修订号再提交，两个并发成功请求可能拿到同号；
随后按号查询、核算或确认会引用不确定的资料。
修复后：每次成功提交在本类别取得唯一且递增的修订号（计数器在写锁内
分配，唯一索引兜底），修订号与整份资料在同一事务原子落库；按号查询、
核算与确认指向同一份资料；确认仍只接受当时最新修订；历史库存在同号
多份资料时启动明确报出冲突修订号，不任取其一。
"""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app import database, models
from app.main import app


def _slot_payload(marker: int) -> dict:
    """每线程一份可区分的槽位资料（槽位编号与力矩下限作标记）。"""
    return {
        "slots": [
            {
                "code": f"S{marker}01",
                "stack": f"S{marker}",
                "tier": 1,
                "capacity": 100,
                "arm": 2.0,
            },
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -1000.0 - marker,
        "max_moment": 100000.0,
    }


def _manifest_payload(marker: int) -> dict:
    return {
        "containers": [
            {
                "no": f"C{marker:03d}",
                "weight": 10 + marker,
                "hazmat": None,
                "discharge_order": 1,
            },
        ]
    }


def _post_concurrently(path: str, payloads: list[dict]) -> list[dict]:
    """多线程并发提交，返回与 payloads 对齐的响应 JSON 列表。"""
    responses: list[dict] = [None] * len(payloads)

    def post(i):
        local = TestClient(app)
        r = local.post(path, json=payloads[i])
        assert r.status_code == 201, r.text
        responses[i] = r.json()

    threads = [threading.Thread(target=post, args=(i,)) for i in range(len(payloads))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return responses


def test_concurrent_slot_submissions_get_unique_increasing_revisions(client):
    """两名调度员并发录入槽位资料：每个成功请求取得唯一且递增的修订号。"""
    payloads = [_slot_payload(i) for i in range(8)]
    responses = _post_concurrently("/api/slot-revisions", payloads)

    revisions = [r["slots_revision"] for r in responses]
    # 同号不再出现：8 个成功请求取得 1..8 各一次（唯一且递增）
    assert sorted(revisions) == list(range(1, 9))

    # 修订号与整份资料原子落库：按号查询指向提交时的那一份资料
    for payload, response in zip(payloads, responses):
        got = client.get(f"/api/slot-revisions/{response['slots_revision']}").json()
        assert got["min_moment"] == payload["min_moment"]
        assert got["max_moment"] == payload["max_moment"]
        assert [s["code"] for s in got["slots"]] == [payload["slots"][0]["code"]]
        assert response["slot_count"] == 1


def test_concurrent_manifest_submissions_get_unique_increasing_revisions(client):
    """两名调度员并发录入箱清单：每个成功请求取得唯一且递增的修订号。"""
    payloads = [_manifest_payload(i) for i in range(8)]
    responses = _post_concurrently("/api/manifest-revisions", payloads)

    revisions = [r["manifest_revision"] for r in responses]
    assert sorted(revisions) == list(range(1, 9))

    for payload, response in zip(payloads, responses):
        got = client.get(
            f"/api/manifest-revisions/{response['manifest_revision']}"
        ).json()
        assert [c["no"] for c in got["containers"]] == [
            payload["containers"][0]["no"]
        ]
        assert response["container_count"] == 1


def test_two_categories_allocate_independently(client):
    """槽位资料与箱清单各自独立编号，互不占用对方的号段。"""
    slot_responses = _post_concurrently(
        "/api/slot-revisions", [_slot_payload(i) for i in range(4)]
    )
    manifest_responses = _post_concurrently(
        "/api/manifest-revisions", [_manifest_payload(i) for i in range(4)]
    )
    assert sorted(r["slots_revision"] for r in slot_responses) == [1, 2, 3, 4]
    assert sorted(r["manifest_revision"] for r in manifest_responses) == [1, 2, 3, 4]


SLOTS_TWO = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}

SLOTS_FOUR = {
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

MANIFEST_TWO = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 20, "hazmat": None, "discharge_order": 1},
    ]
}


def test_query_compute_confirm_bind_to_the_same_revision_data(client):
    """按号查询/核算/确认指向同一份资料；确认仍只接受当时最新修订。"""
    rev_a = client.post("/api/slot-revisions", json=SLOTS_TWO) \
        .json()["slots_revision"]
    rev_b = client.post("/api/slot-revisions", json=SLOTS_FOUR) \
        .json()["slots_revision"]
    rev_m = client.post("/api/manifest-revisions", json=MANIFEST_TWO) \
        .json()["manifest_revision"]
    assert (rev_a, rev_b, rev_m) == (1, 2, 1)

    # 按号查询：两个修订号各自指向提交时的那一份资料
    got_a = client.get(f"/api/slot-revisions/{rev_a}").json()
    got_b = client.get(f"/api/slot-revisions/{rev_b}").json()
    assert len(got_a["slots"]) == 2 and got_a["adjacency"] == []
    assert len(got_b["slots"]) == 4 and got_b["adjacency"] == [["A01", "B01"], ["A02", "B02"]]

    # 按号核算：方案记录的资料修订号即所提交的修订号
    plan_a = client.post("/api/plans/compute", json={
        "slots_revision": rev_a, "manifest_revision": rev_m
    }).json()
    assert plan_a["slots_revision"] == rev_a
    assert {a["slot_code"] for a in plan_a["assignments"]} <= {"A01", "B01"}

    # 确认仍只接受当时最新修订：rev_a 已过期 -> 409，草稿不变
    stale = client.post(f"/api/plans/{plan_a['plan_id']}/confirm", json={
        "slots_revision": rev_a, "manifest_revision": rev_m
    })
    assert stale.status_code == 409
    assert "已过期" in stale.json()["detail"]
    assert client.get(f"/api/plans/{plan_a['plan_id']}").json()["status"] == "draft"

    # 基于最新修订核算并确认 -> 成功
    plan_b = client.post("/api/plans/compute", json={
        "slots_revision": rev_b, "manifest_revision": rev_m
    }).json()
    ok = client.post(f"/api/plans/{plan_b['plan_id']}/confirm", json={
        "slots_revision": rev_b, "manifest_revision": rev_m
    })
    assert ok.status_code == 200
    assert ok.json()["status"] == "confirmed"


def _counter_value(db, category: str) -> int:
    """本类别计数器当前值（无行视为 0；启动迁移会为各类别补建 0 行）。"""
    row = db.query(models.RevisionCounter).filter_by(category=category).first()
    return row.last_allocated if row else 0


def test_invalid_submission_rejected_and_burns_no_number(client, db):
    """入参校验失败（422）：不落任何资料，也不占用修订号。"""
    bad = dict(SLOTS_TWO)
    bad["slots"] = [SLOTS_TWO["slots"][0], SLOTS_TWO["slots"][0]]  # 重复槽位编号
    r = client.post("/api/slot-revisions", json=bad)
    assert r.status_code == 422
    assert db.query(models.SlotRevision).count() == 0
    assert _counter_value(db, models.SLOT_REVISION_CATEGORY) == 0

    ok = client.post("/api/slot-revisions", json=SLOTS_TWO)
    assert ok.status_code == 201
    assert ok.json()["slots_revision"] == 1


def test_failed_commit_leaves_no_partial_data_and_burns_no_number(
    client, db, monkeypatch
):
    """提交中途失败：整份资料与计数器一起回滚，不留半份资料、不烧号。"""
    import sqlalchemy.orm as orm

    original_commit = orm.Session.commit
    state = {"fail": True}

    def fail_once(self):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("模拟提交时失败")
        return original_commit(self)

    monkeypatch.setattr(orm.Session, "commit", fail_once)

    local = TestClient(app, raise_server_exceptions=False)
    r = local.post("/api/slot-revisions", json=SLOTS_TWO)
    assert r.status_code == 500

    # 修订头、槽位行、相邻/不相容行与计数器递增均未留下半份
    assert db.query(models.SlotRevision).count() == 0
    assert db.query(models.SlotRow).count() == 0
    assert _counter_value(db, models.SLOT_REVISION_CATEGORY) == 0

    # 下一次成功提交仍取得修订 1（失败未烧号）
    ok = local.post("/api/slot-revisions", json=SLOTS_TWO)
    assert ok.status_code == 201
    assert ok.json()["slots_revision"] == 1
    got = local.get("/api/slot-revisions/1").json()
    assert len(got["slots"]) == 2


# ---------- 启动时的历史冲突检查 ----------


def _create_old_db(path, slot_revisions, manifest_revisions):
    """构造修复前的旧库：修订号无唯一约束，可含同号多份资料。"""
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE slot_revisions ("
            "id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, "
            "min_moment FLOAT NOT NULL, max_moment FLOAT NOT NULL, "
            "created_at DATETIME)"
        ))
        conn.execute(text(
            "CREATE TABLE manifest_revisions ("
            "id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, "
            "created_at DATETIME)"
        ))
        for rev in slot_revisions:
            conn.execute(text(
                "INSERT INTO slot_revisions (revision, min_moment, max_moment) "
                "VALUES (:r, -100, 100)"
            ), {"r": rev})
        for rev in manifest_revisions:
            conn.execute(text(
                "INSERT INTO manifest_revisions (revision) VALUES (:r)"
            ), {"r": rev})
    return eng


def test_startup_check_reports_conflicting_revisions(tmp_path):
    """旧库同号多份：明确报出两类资料的冲突修订号，不任取其一。"""
    eng = _create_old_db(tmp_path / "old.db", [1, 2, 2, 3, 3], [1, 1])
    try:
        with pytest.raises(RuntimeError) as exc_info:
            database.check_revision_conflicts(eng)
        msg = str(exc_info.value)
        assert "槽位资料" in msg and "2、3" in msg
        assert "箱清单" in msg and "1" in msg
        assert "不任取其一" in msg
    finally:
        eng.dispose()


def test_startup_check_passes_on_clean_history(tmp_path):
    """无冲突旧库：检查通过，不中止启动。"""
    eng = _create_old_db(tmp_path / "old.db", [1, 2, 3], [1, 2])
    try:
        database.check_revision_conflicts(eng)
    finally:
        eng.dispose()


def test_init_db_aborts_on_conflicting_history(tmp_path, monkeypatch):
    """启动流程：历史库存在同号多份资料时 init_db 中止并报出冲突号。"""
    eng = _create_old_db(tmp_path / "old.db", [2, 2], [1])
    monkeypatch.setattr(database, "engine", eng)
    try:
        with pytest.raises(RuntimeError, match="同号多份"):
            database.init_db()
    finally:
        eng.dispose()


def test_init_db_migrates_clean_old_db(tmp_path, monkeypatch):
    """无冲突旧库：启动幂等补齐唯一索引与计数器，后续录入取得递增唯一号。"""
    eng = _create_old_db(tmp_path / "old.db", [1, 2, 3], [1, 2])
    monkeypatch.setattr(database, "engine", eng)
    try:
        database.init_db()
        # 计数器回填到各类别当前最大修订号
        with eng.begin() as conn:
            assert conn.execute(text(
                "SELECT last_allocated FROM revision_counters "
                "WHERE category = 'slots'"
            )).scalar_one() == 3
            assert conn.execute(text(
                "SELECT last_allocated FROM revision_counters "
                "WHERE category = 'manifests'"
            )).scalar_one() == 2
        # 唯一索引已生效：手工插入同号被拒绝
        with pytest.raises(IntegrityError):
            with eng.begin() as conn:
                conn.execute(text(
                    "INSERT INTO slot_revisions (revision, min_moment, max_moment) "
                    "VALUES (3, -1, 1)"
                ))
        # init_db 幂等：再次执行不报错、计数器不回退
        database.init_db()
        with eng.begin() as conn:
            assert conn.execute(text(
                "SELECT last_allocated FROM revision_counters "
                "WHERE category = 'slots'"
            )).scalar_one() == 3
    finally:
        eng.dispose()


def test_allocate_revision_continues_after_migrated_counter(db):
    """计数器与资料表一致：成功提交沿计数器递增，不复用历史号。"""
    from app import services
    from app.schemas import ManifestIn

    rev1 = services.create_manifest_revision(db, ManifestIn(**MANIFEST_TWO))
    rev2 = services.create_manifest_revision(db, ManifestIn(**MANIFEST_TWO))
    assert (rev1.revision, rev2.revision) == (1, 2)
    assert db.query(models.ManifestRevision).count() == 2
    # 每份资料完整落库
    assert all(
        len(r.containers) == 2
        for r in db.query(models.ManifestRevision).all()
    )
