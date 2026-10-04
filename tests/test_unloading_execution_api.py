"""逐港卸箱执行（现场逐步登记）API 测试。"""
import threading

from app import models
from app.database import SessionLocal


# 与 test_unloading_api 相同的配载：
# C001(10,港1)->A02、C002(30,港2)->A01、C003(10,港1)->B01，初始力矩 -30
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

PORTS = {
    "ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 100},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]
}


def _seed_and_confirm(client, ports=PORTS):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=ports)
    assert r.status_code == 201, r.text
    return plan


def _execute(client, plan_id, container, op, version):
    return client.post(
        f"/api/plans/{plan_id}/unloading/execute",
        json={
            "container_no": container,
            "operation_no": op,
            "expected_version": version,
        },
    )


def test_pre_execution_get_shows_version_zero_preview(client):
    plan = _seed_and_confirm(client)
    r = client.get(f"/api/plans/{plan['plan_id']}/unloading/execution")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["progress_version"] == 0
    assert data["frozen"] is False
    assert data["frozen_at"] is None
    assert data["current_port"] == 1
    assert data["remaining_moment"] == -30
    assert data["initial_moment"] == -30
    assert data["completed"] is False
    assert data["read_only"] is False
    assert data["planned_next_container"] == "C001"
    assert data["steps"] == []
    assert [s["container_no"] for s in data["recommendation"]] == [
        "C001", "C003", "C002"
    ]


def test_get_execution_404_without_sequence(client):
    plan = _seed_and_confirm(client)
    # 未规划卸箱顺序的全新方案
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    p2 = client.post("/api/plans/compute", json={}).json()
    assert client.get(f"/api/plans/{p2['plan_id']}/unloading/execution").status_code == 404
    # 未知方案
    assert client.get("/api/plans/nope/unloading/execution").status_code == 404
    # 已规划方案正常
    assert client.get(f"/api/plans/{plan['plan_id']}/unloading/execution").status_code == 200


def test_execute_happy_path_complete_all(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]

    r1 = _execute(client, pid, "C001", "OP-1", 0)
    assert r1.status_code == 201, r1.text
    d1 = r1.json()
    assert d1["progress_version"] == 1
    assert d1["frozen"] is True
    assert d1["frozen_at"] is not None
    assert d1["current_port"] == 1
    assert d1["remaining_moment"] == -10
    assert d1["planned_next_container"] == "C003"
    assert [s["container_no"] for s in d1["steps"]] == ["C001"]
    assert d1["steps"][0]["remaining_moment"] == -10
    assert d1["steps"][0]["unloaded_at"] is not None
    assert [s["container_no"] for s in d1["recommendation"]] == ["C003", "C002"]

    r2 = _execute(client, pid, "C003", "OP-2", 1)
    assert r2.status_code == 201, r2.text
    d2 = r2.json()
    assert d2["progress_version"] == 2
    assert d2["current_port"] == 2
    assert d2["remaining_moment"] == -60
    assert d2["planned_next_container"] == "C002"
    assert [s["container_no"] for s in d2["steps"]] == ["C001", "C003"]
    assert [s["container_no"] for s in d2["recommendation"]] == ["C002"]

    r3 = _execute(client, pid, "C002", "OP-3", 2)
    assert r3.status_code == 201, r3.text
    d3 = r3.json()
    assert d3["progress_version"] == 3
    assert d3["completed"] is True
    assert d3["read_only"] is True
    assert d3["current_port"] is None
    assert d3["remaining_moment"] == 0
    assert d3["planned_next_container"] is None
    assert d3["recommendation"] == []
    assert [s["container_no"] for s in d3["steps"]] == ["C001", "C003", "C002"]

    # GET 与登记结果一致
    g = client.get(f"/api/plans/{pid}/unloading/execution")
    assert g.status_code == 200
    assert g.json()["progress_version"] == 3
    assert g.json()["completed"] is True


def test_field_can_differ_from_plan_next_container(client):
    """允许现场选择不同于预案下一步的箱（C003 先于计划里的 C001）。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    r = _execute(client, pid, "C003", "OP-X", 0)
    assert r.status_code == 201, r.text
    d = r.json()
    # 实际卸了 C003，但原计划下一步仍对照显示 C001
    assert [s["container_no"] for s in d["steps"]] == ["C003"]
    assert d["planned_next_container"] == "C001"
    assert d["remaining_moment"] == -80
    assert [s["container_no"] for s in d["recommendation"]] == ["C001", "C002"]


def test_lower_stack_box_rejected_422(client):
    """同堆栈上层箱未卸时不得取下层箱：港2箱 C002 在 A01，其上方 C001 未卸。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    # 当前港为 1，C002 既不属当前港也不在栈顶
    r = _execute(client, pid, "C002", "OP-BAD", 0)
    assert r.status_code == 422
    assert "当前港" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution")
    assert g.json()["progress_version"] == 0 and g.json()["frozen"] is False


def test_unknown_and_already_unloaded_box_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    r1 = _execute(client, pid, "C999", "OP-NOPE", 0)
    assert r1.status_code == 422 and "非法箱项" in r1.json()["detail"]
    assert _execute(client, pid, "C001", "OP-1", 0).status_code == 201
    r2 = _execute(client, pid, "C001", "OP-DUP", 1)
    assert r2.status_code == 422 and "已经卸下" in r2.json()["detail"]


def test_moment_window_violation_rejected_422(client):
    # 港1区间 [-100,-30]：先卸 C001 后剩余 -10，不在区间内
    ports = {
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": -30},
            {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
        ]
    }
    plan = _seed_and_confirm(client, ports=ports)
    pid = plan["plan_id"]
    r = _execute(client, pid, "C001", "OP-W", 0)
    assert r.status_code == 422
    assert "剩余总力矩" in r.json()["detail"]
    # 进度不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["progress_version"] == 0 and g["steps"] == []
    # 合法选择 C003（剩余 -80）可登记
    ok = _execute(client, pid, "C003", "OP-OK", 0)
    assert ok.status_code == 201, ok.text


def test_blocked_choice_returns_422_and_keeps_progress(client):
    """即时合法、但新状态无可行后续的箱项被拒绝（422，进度不变）。

    配载（初始总力矩 -115）：
      栈A：C2(港1,重5,臂+1,力矩5) 压 C1(港2,重20,臂-1,力矩-20)
      栈B：C3(港1,重30,臂-3,力矩-90)
      栈C：C5(港1,重10,臂-2,力矩-20,上层) 压 C4(港1,重10,臂+1,力矩10,下层)
    港1窗口 [-120,-20]、港2窗口 [0,0]。可行计划：C2(-120)→C5(-100)→
    C4(-110)→C3(-20)→C1(0)。
    现场首箱选 C3：卸后 -25 ∈ [-120,-20] 即时合法，但此后栈C只能先卸
    C5（力矩-20），其卸下时剩余力矩约 -5～-10，与港1窗口不相交 → 死局。
    """
    slots = {
        "slots": [
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 20, "arm": -1.0},
            {"code": "A02", "stack": "A", "tier": 2, "capacity": 5, "arm": 1.0},
            {"code": "B01", "stack": "B", "tier": 1, "capacity": 30, "arm": -3.0},
            {"code": "C01", "stack": "C", "tier": 1, "capacity": 10, "arm": 1.0},
            {"code": "C02", "stack": "C", "tier": 2, "capacity": 10, "arm": -2.0},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -100000,
        "max_moment": 100000,
    }
    manifest = {
        "containers": [
            {"no": "C1", "weight": 20, "hazmat": None, "discharge_order": 2},
            {"no": "C2", "weight": 5, "hazmat": None, "discharge_order": 1},
            {"no": "C3", "weight": 30, "hazmat": None, "discharge_order": 1},
            {"no": "C4", "weight": 10, "hazmat": None, "discharge_order": 1},
            {"no": "C5", "weight": 10, "hazmat": None, "discharge_order": 1},
        ]
    }
    sr = client.post("/api/slot-revisions", json=slots).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=manifest) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    pairs = {x["container_no"]: x["slot_code"] for x in plan["assignments"]}
    assert pairs == {"C1": "A01", "C2": "A02", "C3": "B01", "C4": "C01",
                     "C5": "C02"}, pairs
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    body = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -120, "max_remaining_moment": -20},
        {"discharge_order": 2, "min_remaining_moment": 0, "max_remaining_moment": 0},
    ]}
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading", json=body)
    assert r.status_code == 201, r.text
    assert [s["container_no"] for s in r.json()["steps"]] == [
        "C2", "C5", "C4", "C3", "C1"
    ]
    pid = plan["plan_id"]

    # 选 C3：即时剩 -25 ∈ [-120,-20] 合法，但后续不可行
    bad = _execute(client, pid, "C3", "OP-BLOCK", 0)
    assert bad.status_code == 422, bad.text
    detail = bad.json()
    assert "无法" in detail["detail"] and "阻碍原因" in detail["detail"]
    assert detail["container_no"] == "C3"
    assert detail["current_port"] == 1
    assert detail["remaining_moment_after"] == -25
    # 进度不变，冻结与台账都不留下
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["progress_version"] == 0 and g["steps"] == [] and g["frozen"] is False
    db = SessionLocal()
    try:
        assert db.query(models.UnloadingExecOperation).count() == 0
        assert db.query(models.UnloadingExecution).count() == 0
    finally:
        db.close()

    # 按推荐选 C2 可行，登记成功
    ok = _execute(client, pid, "C2", "OP-OK", 0)
    assert ok.status_code == 201, ok.text
    assert ok.json()["remaining_moment"] == -120
    assert [s["container_no"] for s in ok.json()["recommendation"]] == [
        "C5", "C4", "C3", "C1"
    ]


def test_version_conflict_409_and_concurrency(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C001", "OP-1", 0).status_code == 201
    # 过期版本
    r = _execute(client, pid, "C003", "OP-STALE", 0)
    assert r.status_code == 409
    assert "进度版本" in r.json()["detail"]
    # 非法箱项也不得推进版本
    assert client.get(f"/api/plans/{pid}/unloading/execution").json()[
        "progress_version"
    ] == 1


def test_same_operation_retry_returns_first_snapshot_200(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    r1 = _execute(client, pid, "C001", "OP-R", 0)
    assert r1.status_code == 201
    snap1 = r1.json()
    # 同操作号同内容重试（即使带的是过期期望版本 0，仍返回首次快照，不生效）
    r2 = _execute(client, pid, "C001", "OP-R", 0)
    assert r2.status_code == 200
    assert r2.json() == snap1
    # 版本未被重试推进
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["progress_version"] == 1


def test_same_operation_different_content_conflict_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C001", "OP-C", 0).status_code == 201
    # 同操作号换箱
    r1 = _execute(client, pid, "C003", "OP-C", 1)
    assert r1.status_code == 409 and "异内容冲突" in r1.json()["detail"]
    # 同操作号换期望版本
    r2 = _execute(client, pid, "C001", "OP-C", 1)
    assert r2.status_code == 409 and "异内容冲突" in r2.json()["detail"]
    # 进度不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["progress_version"] == 1
    assert [s["container_no"] for s in g["steps"]] == ["C001"]


def test_operation_no_cross_plan_conflict_409(client):
    p1 = _seed_and_confirm(client)
    # 第二套槽位资料 + 箱清单（独立方案链，不与 p1 抢槽位唯一占用）
    client.post("/api/slot-revisions", json={
        "slots": [
            {"code": "Z01", "stack": "Z", "tier": 1, "capacity": 100, "arm": 2.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    })
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "X1", "weight": 10, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post("/api/plans/compute", json={
        "slots_revision": 2, "manifest_revision": 2
    }).json()
    cr = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 2, "manifest_revision": 2
    })
    assert cr.status_code == 200, cr.text
    ur = client.post(f"/api/plans/{p2['plan_id']}/unloading", json={
        "ports": [{"discharge_order": 1, "min_remaining_moment": -100,
                   "max_remaining_moment": 100}]
    })
    assert ur.status_code == 201, ur.text
    assert _execute(client, p1["plan_id"], "C001", "GLOBAL-OP", 0).status_code == 201
    r = _execute(client, p2["plan_id"], "X1", "GLOBAL-OP", 0)
    assert r.status_code == 409 and "异内容冲突" in r.json()["detail"]


def test_no_sequence_and_draft_plan_rejected_409(client):
    # draft 方案
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    draft = client.post("/api/plans/compute", json={}).json()
    r = _execute(client, draft["plan_id"], "C001", "OP-D", 0)
    assert r.status_code == 409 and "确认" in r.json()["detail"]

    # confirmed 但无卸箱计划
    plan = _seed_and_confirm(client)
    db = SessionLocal()
    try:
        seq = db.query(models.UnloadingSequence).filter_by(
            plan_id=plan["plan_id"]
        ).one()
        db.query(models.UnloadingStepRow).filter_by(sequence_id=seq.id).delete()
        db.query(models.UnloadingPortRow).filter_by(sequence_id=seq.id).delete()
        db.query(models.UnloadingSequence).filter_by(id=seq.id).delete()
        db.commit()
    finally:
        db.close()
    r2 = _execute(client, plan["plan_id"], "C001", "OP-NOPLAN", 0)
    assert r2.status_code == 409 and "卸箱计划" in r2.json()["detail"]


def test_superseded_plan_read_only_but_history_queryable(client):
    p1 = _seed_and_confirm(client)
    pid = p1["plan_id"]
    assert _execute(client, pid, "C001", "OP-1", 0).status_code == 201

    # 新修订确认取代 p1
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    assert client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    }).status_code == 200

    # 再登记只读
    r = _execute(client, pid, "C003", "OP-RO", 1)
    assert r.status_code == 409 and "只读" in r.json()["detail"]
    # 历史进度仍可查
    g = client.get(f"/api/plans/{pid}/unloading/execution")
    assert g.status_code == 200
    data = g.json()
    assert data["status"] == "historical"
    assert data["read_only"] is True
    assert data["progress_version"] == 1
    assert [s["container_no"] for s in data["steps"]] == ["C001"]
    assert data["frozen"] is True

    # 同操作号重试在取代后仍返回首次快照
    retry = _execute(client, pid, "C001", "OP-1", 0)
    assert retry.status_code == 200
    assert retry.json()["progress_version"] == 1


def test_replan_after_freeze_does_not_change_progress(client):
    """首次登记冻结计划后，重排原计划不改已卸记录与后续核算。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    assert _execute(client, pid, "C001", "OP-1", 0).status_code == 201

    # 同一方案修订重排：用迫使 C003 先卸的窗口原子替换原顺序
    body = {"ports": [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": -30},
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 100},
    ]}
    r = client.post(f"/api/plans/{pid}/unloading", json=body)
    assert r.status_code == 201
    new_plan = [s["container_no"] for s in r.json()["steps"]]
    assert new_plan == ["C003", "C001", "C002"]

    # 已卸记录与原计划对照不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["progress_version"] == 1
    assert [s["container_no"] for s in g["steps"]] == ["C001"]
    assert g["planned_next_container"] == "C003"
    # 后续仍按冻结港区间核算：当前港1剩余 C003，卸下后 -60 落在原区间 [-100,100]
    ok = _execute(client, pid, "C003", "OP-2", 1)
    assert ok.status_code == 201, ok.text
    assert ok.json()["remaining_moment"] == -60


def test_completed_plan_rejects_further_registration_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _execute(client, pid, "C001", "OP-1", 0)
    _execute(client, pid, "C003", "OP-2", 1)
    _execute(client, pid, "C002", "OP-3", 2)
    r = _execute(client, pid, "C002", "OP-4", 3)
    assert r.status_code == 409 and "完结" in r.json()["detail"]


def test_concurrent_registrations_serialize_by_version(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    results = []

    def worker(op, container, version):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)
        results.append(
            (op, c.post(
                f"/api/plans/{pid}/unloading/execute",
                json={"container_no": container, "operation_no": op,
                      "expected_version": version},
            ).status_code)
        )

    t1 = threading.Thread(target=worker, args=("OP-T1", "C001", 0))
    t2 = threading.Thread(target=worker, args=("OP-T2", "C003", 0))
    t1.start(); t2.start(); t1.join(); t2.join()
    statuses = sorted(results, key=lambda x: x[0])
    assert {s for _, s in statuses} == {201, 409}

    db = SessionLocal()
    try:
        exec_row = db.query(models.UnloadingExecution).filter_by(plan_id=pid).one()
        assert exec_row.progress_version == 1
        assert db.query(models.UnloadingExecStep).filter_by(
            execution_id=exec_row.id
        ).count() == 1
        assert db.query(models.UnloadingExecOperation).filter(
            models.UnloadingExecOperation.execution_id == exec_row.id
        ).count() == 1
    finally:
        db.close()


def test_empty_plan_execution_completed_from_start(client):
    """空箱方案：执行视图直接完结，登记返回 409。"""
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
    client.post(f"/api/plans/{plan['plan_id']}/unloading", json={"ports": []})
    g = client.get(f"/api/plans/{plan['plan_id']}/unloading/execution")
    assert g.status_code == 200
    data = g.json()
    assert data["completed"] is True
    assert data["current_port"] is None
    assert data["remaining_moment"] == 0
    assert data["recommendation"] == []
    # 空方案无箱可卸，任何登记都拒绝（且无箱号合法）
    r = client.post(f"/api/plans/{plan['plan_id']}/unloading/execute", json={
        "container_no": "C001", "operation_no": "OP-E", "expected_version": 0
    })
    assert r.status_code in (409, 422)


def test_validation_errors_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    # 空操作号
    r1 = client.post(f"/api/plans/{pid}/unloading/execute", json={
        "container_no": "C001", "operation_no": "  ", "expected_version": 0
    })
    assert r1.status_code == 422
    # 负期望版本
    r2 = client.post(f"/api/plans/{pid}/unloading/execute", json={
        "container_no": "C001", "operation_no": "OP-V", "expected_version": -1
    })
    assert r2.status_code == 422
    # 缺字段
    r3 = client.post(f"/api/plans/{pid}/unloading/execute", json={
        "container_no": "C001"
    })
    assert r3.status_code == 422
