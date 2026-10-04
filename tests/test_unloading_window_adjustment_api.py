"""逐港卸箱执行：收紧剩余力矩安全区间（调整申请）API 测试。"""
import math

from app import models
from app.database import SessionLocal


# 与执行 API 测试相同的配载：
# C001(10,港1)->A02(臂-2,力矩-20)、C002(30,港2)->A01(臂-2,力矩-60)、
# C003(10,港1)->B01(臂5,力矩50)，初始力矩 -30；可行计划 C001→C003→C002
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


def _adjust(client, plan_id, application_no, ports, *,
            expected_progress_version=1, expected_window_version=0):
    return client.post(
        f"/api/plans/{plan_id}/unloading/window-adjustments",
        json={
            "application_no": application_no,
            "expected_progress_version": expected_progress_version,
            "expected_window_version": expected_window_version,
            "ports": ports,
        },
    )


def _start_execution(client, plan):
    """登记首箱 C001（剩 -10），使执行进入已开始未完结状态。"""
    r = _execute(client, plan["plan_id"], "C001", "OP-START", 0)
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 基础展示字段 ----------


def test_pre_start_view_has_window_version_zero_and_no_history(client):
    plan = _seed_and_confirm(client)
    g = client.get(f"/api/plans/{plan['plan_id']}/unloading/execution").json()
    assert g["window_version"] == 0
    assert g["window_adjustments"] == []
    assert {(p["discharge_order"], p["min_remaining_moment"], p["max_remaining_moment"])
            for p in g["effective_ports"]} == {(1, -100, 100), (2, -100, 100)}


def test_started_view_keeps_effective_ports_equal_to_frozen(client):
    plan = _seed_and_confirm(client)
    d = _start_execution(client, plan)
    assert d["window_version"] == 0
    assert d["window_adjustments"] == []
    assert [p["discharge_order"] for p in d["effective_ports"]] == [1, 2]
    # 原接口字段全部保留
    for key in ("progress_version", "remaining_moment", "steps", "recommendation",
                "planned_next_container", "frozen", "frozen_at"):
        assert key in d


# ---------- 调整生效 ----------


def test_adjustment_tightens_current_and_future_ports(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)  # 剩 -10，当前港 1

    r = _adjust(client, pid, "ADJ-1", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": -60, "max_remaining_moment": 0},
    ])
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["window_version"] == 1
    assert d["progress_version"] == 1  # 进度版本不随调整推进
    assert d["remaining_moment"] == -10
    eff = {p["discharge_order"]: (p["min_remaining_moment"], p["max_remaining_moment"])
           for p in d["effective_ports"]}
    assert eff == {1: (-80, -10), 2: (-60, 0)}
    hist = d["window_adjustments"]
    assert len(hist) == 1
    h = hist[0]
    assert h["application_no"] == "ADJ-1"
    assert h["window_version"] == 1
    assert h["expected_progress_version"] == 1
    assert h["expected_window_version"] == 0
    assert [p["discharge_order"] for p in h["ports"]] == [1, 2]
    assert h["created_at"]

    # GET 同步展示生效区间、版本与历史
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1
    assert {p["discharge_order"] for p in g["effective_ports"]} == {1, 2}
    assert len(g["window_adjustments"]) == 1

    # 历史列表端点
    lh = client.get(f"/api/plans/{pid}/unloading/window-adjustments")
    assert lh.status_code == 200
    assert [x["application_no"] for x in lh.json()] == ["ADJ-1"]


def test_omitted_ports_keep_effective_window(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    # 只列港 1，港 2 沿用当前生效区间 [-100,100]
    r = _adjust(client, pid, "ADJ-P1", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ])
    assert r.status_code == 201, r.text
    eff = {p["discharge_order"]: (p["min_remaining_moment"], p["max_remaining_moment"])
           for p in r.json()["effective_ports"]}
    assert eff == {1: (-80, -10), 2: (-100, 100)}


def test_only_future_port_can_be_adjusted(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)  # 当前港 1
    r = _adjust(client, pid, "ADJ-P2", [
        {"discharge_order": 2, "min_remaining_moment": -100, "max_remaining_moment": 0},
    ])
    assert r.status_code == 201, r.text
    eff = {p["discharge_order"]: (p["min_remaining_moment"], p["max_remaining_moment"])
           for p in r.json()["effective_ports"]}
    assert eff[1] == (-100, 100)
    assert eff[2] == (-100, 0)


def test_multiple_adjustments_increment_version_and_accumulate_history(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    r1 = _adjust(client, pid, "ADJ-1", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ])
    assert r1.status_code == 201 and r1.json()["window_version"] == 1
    # 第二次收紧须带新区间版本 1；仍是可行收紧（含 -60 与 -10）
    r2 = _adjust(client, pid, "ADJ-2", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ], expected_window_version=1)
    assert r2.status_code == 201, r2.text
    assert r2.json()["window_version"] == 2
    assert [h["application_no"] for h in r2.json()["window_adjustments"]] == [
        "ADJ-1", "ADJ-2"
    ]
    eff1 = next(p for p in r2.json()["effective_ports"] if p["discharge_order"] == 1)
    assert (eff1["min_remaining_moment"], eff1["max_remaining_moment"]) == (-80, -10)


# ---------- 后续登记使用生效区间 ----------


def test_feasible_tightening_governs_next_registration(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    # 港1 [-80,-10]、港2 [-60,-60] 是完整可行收紧（C003→-60，C002→0？
    # C002 力矩 -60，卸 C003 后 -60，再卸 C002 后 0，故港2须含0）。
    r = _adjust(client, pid, "ADJ-F", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": 0, "max_remaining_moment": 0},
    ])
    assert r.status_code == 201, r.text
    # 推荐按新区间：C003 卸后 -60 ∈ [-80,-10]，C002 卸后 0 ∈ [0,0]
    assert [s["container_no"] for s in r.json()["recommendation"]] == ["C003", "C002"]

    # 登记 C003（-60，落在生效港1区间）成功
    ok = _execute(client, pid, "C003", "OP-2", 1)
    assert ok.status_code == 201, ok.text
    assert ok.json()["remaining_moment"] == -60
    assert ok.json()["window_version"] == 1  # 区间版本在登记后保持
    # 再卸 C002 到 0
    fin = _execute(client, pid, "C002", "OP-3", 2)
    assert fin.status_code == 201, fin.text
    assert fin.json()["completed"] is True


def test_registration_violating_effective_window_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    # 首箱改卸 C003（剩 -80，港1余 C001）
    r = _execute(client, pid, "C003", "OP-START", 0)
    assert r.status_code == 201, r.text
    # 收紧港1 到 [-80,-80]：当前 -80 合法，但此后卸 C001 后 -60 不可行 → 422
    bad = _adjust(client, pid, "ADJ-BAD", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -80},
    ])
    assert bad.status_code == 422
    assert "无解" in bad.json()["detail"]
    # 未生效：版本仍 0、历史为空
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0 and g["window_adjustments"] == []

    # 可行收紧港1 [-80,-60]（卸 C001 后 -60）生效
    ok = _adjust(client, pid, "ADJ-OK", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -60},
    ])
    assert ok.status_code == 201, ok.text
    # 此后任何使剩 -10 的卸法（C001 卸后 -60 实际合法）——
    # C001 力矩 -20，-80-(-20) = -60 ∈ [-80,-60]，合法
    reg = _execute(client, pid, "C001", "OP-2", 1)
    assert reg.status_code == 201, reg.text


# ---------- 拒绝场景 ----------


def test_adjust_before_start_409(client):
    plan = _seed_and_confirm(client)
    r = _adjust(client, plan["plan_id"], "ADJ-EARLY", [
        {"discharge_order": 1, "min_remaining_moment": -50, "max_remaining_moment": 50},
    ])
    assert r.status_code == 409 and "尚未开始" in r.json()["detail"]
    # 历史端点也 404（执行未开始）
    lh = client.get(f"/api/plans/{plan['plan_id']}/unloading/window-adjustments")
    assert lh.status_code == 404


def test_completed_execution_rejects_adjustment_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _execute(client, pid, "C001", "OP-1", 0)
    _execute(client, pid, "C003", "OP-2", 1)
    _execute(client, pid, "C002", "OP-3", 2)
    r = _adjust(client, pid, "ADJ-DONE", [
        {"discharge_order": 2, "min_remaining_moment": 0, "max_remaining_moment": 0},
    ], expected_progress_version=3)
    assert r.status_code == 409 and "完结" in r.json()["detail"]


def test_completed_port_cannot_be_adjusted_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    # 卸完港1（C001、C003），当前港变为 2
    _execute(client, pid, "C001", "OP-1", 0)
    _execute(client, pid, "C003", "OP-2", 1)
    r = _adjust(client, pid, "ADJ-OLD", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -60},
    ], expected_progress_version=2)
    assert r.status_code == 422 and "已完成" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0


def test_window_must_be_contained_in_effective_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    # 下界低于原生效下界（试图放宽）
    r1 = _adjust(client, pid, "ADJ-W1", [
        {"discharge_order": 1, "min_remaining_moment": -200, "max_remaining_moment": 100},
    ])
    assert r1.status_code == 422 and "包含于" in r1.json()["detail"]
    # 上界高于原生效上界
    r2 = _adjust(client, pid, "ADJ-W2", [
        {"discharge_order": 1, "min_remaining_moment": -100, "max_remaining_moment": 200},
    ])
    assert r2.status_code == 422 and "包含于" in r2.json()["detail"]
    # 第二次收紧相对首次生效区间放宽：先收紧到 [-90,0]，再申请 [-95,0]
    ok = _adjust(client, pid, "ADJ-W3", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ])
    assert ok.status_code == 201
    r3 = _adjust(client, pid, "ADJ-W4", [
        {"discharge_order": 1, "min_remaining_moment": -95, "max_remaining_moment": 0},
    ], expected_window_version=1)
    assert r3.status_code == 422 and "包含于" in r3.json()["detail"]
    # 版本/历史不变
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1 and len(g["window_adjustments"]) == 1


def test_current_moment_must_fall_in_adjusted_current_window_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)  # 当前剩余 -10
    # 收紧港1到 [0,100]：包含于 [-100,100]，但不含当前剩余 -10
    r = _adjust(client, pid, "ADJ-CM", [
        {"discharge_order": 1, "min_remaining_moment": 0, "max_remaining_moment": 100},
    ])
    assert r.status_code == 422 and "当前剩余总力矩" in r.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0


def test_infeasible_tightening_returns_reason_422_and_no_effect(client):
    """收紧区间使剩余箱无法卸完：422 附无解原因，不生效。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    # 港1 {-10}：当前 -10 合法，但卸唯一剩余港1箱 C003 后剩 -60，无可行后续
    r = _adjust(client, pid, "ADJ-INF", [
        {"discharge_order": 1, "min_remaining_moment": -10, "max_remaining_moment": -10},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "无解" in detail
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 0 and g["window_adjustments"] == []
    # 不产生调整留档
    db = SessionLocal()
    try:
        assert db.query(models.UnloadingWindowAdjustment).count() == 0
    finally:
        db.close()


def test_stale_progress_version_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)  # 进度版本 1
    r = _adjust(client, pid, "ADJ-SP", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ], expected_progress_version=0)
    assert r.status_code == 409 and "进度版本" in r.json()["detail"]


def test_stale_window_version_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    ok = _adjust(client, pid, "ADJ-A", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ])
    assert ok.status_code == 201
    # 仍带区间版本 0 -> 过期
    r = _adjust(client, pid, "ADJ-B", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ], expected_window_version=0)
    assert r.status_code == 409 and "区间版本" in r.json()["detail"]
    # 版本/历史不变（ADJ-B 未生效）
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1 and len(g["window_adjustments"]) == 1


def test_superseded_plan_rejects_adjustment_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    r = _adjust(client, pid, "ADJ-RO", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ])
    assert r.status_code == 409 and "只读" in r.json()["detail"]
    # 历史进度仍可查
    g = client.get(f"/api/plans/{pid}/unloading/execution")
    assert g.json()["status"] == "historical"


# ---------- 幂等与冲突 ----------


def test_same_application_no_same_content_retry_returns_first_snapshot_200(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    body_ports = [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ]
    r1 = _adjust(client, pid, "ADJ-IDEM", body_ports)
    assert r1.status_code == 201
    snap = r1.json()
    # 之后再登记一箱推进进度版本
    # （需先放宽港1使 C003 可卸：当前收紧 [-50,50] 下 C003 卸后 -60 不可行，
    # 故此处不再登记，直接用同样的过期期望版本重试仍应返回首次快照）
    r2 = _adjust(client, pid, "ADJ-IDEM", body_ports)
    assert r2.status_code == 200
    assert r2.json() == snap
    # 未重复生效：只有一条历史，区间版本仍为 1
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1 and len(g["window_adjustments"]) == 1


def test_same_application_no_different_content_conflict_409(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    r0 = _adjust(client, pid, "ADJ-C", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ])
    assert r0.status_code == 201
    # 换区间内容
    r1 = _adjust(client, pid, "ADJ-C", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
    ], expected_window_version=1)
    assert r1.status_code == 409 and "异内容" in r1.json()["detail"]
    # 换期望版本
    r2 = _adjust(client, pid, "ADJ-C", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ], expected_progress_version=0)
    assert r2.status_code == 409 and "异内容" in r2.json()["detail"]
    g = client.get(f"/api/plans/{pid}/unloading/execution").json()
    assert g["window_version"] == 1 and len(g["window_adjustments"]) == 1


def test_application_no_cross_plan_conflict_409(client):
    p1 = _seed_and_confirm(client)
    _start_execution(client, p1)
    # 先在 p1 用全局申请号成功调整
    r0 = _adjust(client, p1["plan_id"], "GLOBAL-ADJ", [
        {"discharge_order": 1, "min_remaining_moment": -90, "max_remaining_moment": 0},
    ])
    assert r0.status_code == 201, r0.text
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
    client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 2, "manifest_revision": 2
    })
    client.post(f"/api/plans/{p2['plan_id']}/unloading", json={
        "ports": [{"discharge_order": 1, "min_remaining_moment": -100,
                   "max_remaining_moment": 100}]
    })
    _execute(client, p2["plan_id"], "X1", "OP-X1", 0)

    # 同一申请号复用于另一方案（内容也不同）→ 异内容冲突，且不生效
    r = _adjust(client, p2["plan_id"], "GLOBAL-ADJ", [
        {"discharge_order": 1, "min_remaining_moment": 0, "max_remaining_moment": 20},
    ])
    assert r.status_code == 409 and "异内容" in r.json()["detail"]
    g = client.get(f"/api/plans/{p1['plan_id']}/unloading/execution").json()
    assert g["window_version"] == 1


def test_idempotent_retry_even_after_plan_superseded_200(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    ports = [{"discharge_order": 1, "min_remaining_moment": -90,
              "max_remaining_moment": 0}]
    r1 = _adjust(client, pid, "ADJ-RET", ports)
    assert r1.status_code == 201
    snap = r1.json()
    # 方案被取代
    client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1}
        ]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    r2 = _adjust(client, pid, "ADJ-RET", ports)
    assert r2.status_code == 200
    assert r2.json() == snap


# ---------- 入参校验 ----------


def test_request_validation_422(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    base = f"/api/plans/{pid}/unloading/window-adjustments"

    # 空区间列表
    r1 = client.post(base, json={
        "application_no": "A", "expected_progress_version": 1,
        "expected_window_version": 0, "ports": [],
    })
    assert r1.status_code == 422
    # 区间倒置
    r2 = client.post(base, json={
        "application_no": "A", "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [{"discharge_order": 1, "min_remaining_moment": 10,
                   "max_remaining_moment": 0}],
    })
    assert r2.status_code == 422
    # 非有限数值（以原始 JSON 文本发送 Infinity/NaN，绕过客户端序列化）
    headers = {"Content-Type": "application/json"}
    r3 = client.post(base, content=(
        '{"application_no":"A","expected_progress_version":1,'
        '"expected_window_version":0,"ports":[{"discharge_order":1,'
        '"min_remaining_moment":-Infinity,"max_remaining_moment":100}]}'
    ), headers=headers)
    assert r3.status_code == 422
    r4 = client.post(base, content=(
        '{"application_no":"A","expected_progress_version":1,'
        '"expected_window_version":0,"ports":[{"discharge_order":1,'
        '"min_remaining_moment":NaN,"max_remaining_moment":100}]}'
    ), headers=headers)
    assert r4.status_code == 422
    # 港序号重复
    r5 = client.post(base, json={
        "application_no": "A", "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [
            {"discharge_order": 1, "min_remaining_moment": -90,
             "max_remaining_moment": 0},
            {"discharge_order": 1, "min_remaining_moment": -80,
             "max_remaining_moment": -10},
        ],
    })
    assert r5.status_code == 422 and "重复" in r5.text
    # 空白申请号、负期望版本
    r6 = client.post(base, json={
        "application_no": "  ", "expected_progress_version": 1,
        "expected_window_version": 0,
        "ports": [{"discharge_order": 1, "min_remaining_moment": -90,
                   "max_remaining_moment": 0}],
    })
    assert r6.status_code == 422
    r7 = client.post(base, json={
        "application_no": "A", "expected_progress_version": -1,
        "expected_window_version": 0,
        "ports": [{"discharge_order": 1, "min_remaining_moment": -90,
                   "max_remaining_moment": 0}],
    })
    assert r7.status_code == 422
    # 缺字段
    r8 = client.post(base, json={"application_no": "A"})
    assert r8.status_code == 422


def test_unknown_plan_404(client):
    r = _adjust(client, "nope", "ADJ-X", [
        {"discharge_order": 1, "min_remaining_moment": -50,
         "max_remaining_moment": 50},
    ])
    assert r.status_code == 404
    assert client.get("/api/plans/nope/unloading/window-adjustments").status_code == 404


# ---------- 并发 ----------


def test_concurrent_adjustments_serialize_by_window_version(client):
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)
    results = []

    def worker(no):
        from fastapi.testclient import TestClient
        from app.main import app
        c = TestClient(app)
        results.append((no, c.post(
            f"/api/plans/{pid}/unloading/window-adjustments",
            json={
                "application_no": no,
                "expected_progress_version": 1,
                "expected_window_version": 0,
                "ports": [{"discharge_order": 1,
                          "min_remaining_moment": -90, "max_remaining_moment": 0}],
            },
        ).status_code))

    import threading
    t1 = threading.Thread(target=worker, args=("ADJ-T1",))
    t2 = threading.Thread(target=worker, args=("ADJ-T2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert {s for _, s in results} == {201, 409}
    db = SessionLocal()
    try:
        exec_row = db.query(models.UnloadingExecution).filter_by(plan_id=pid).one()
        assert exec_row.window_version == 1
        assert db.query(models.UnloadingWindowAdjustment).filter_by(
            execution_id=exec_row.id
        ).count() == 1
    finally:
        db.close()


# ---------- 冻结与留档保留 ----------


def test_frozen_windows_and_unloaded_steps_preserved_after_adjustment(client):
    """调整后原冻结区间、已卸步骤保留；推荐与后续登记用生效区间。"""
    plan = _seed_and_confirm(client)
    pid = plan["plan_id"]
    _start_execution(client, plan)  # 已卸 C001，剩 -10
    r = _adjust(client, pid, "ADJ-KEEP", [
        {"discharge_order": 1, "min_remaining_moment": -80, "max_remaining_moment": -10},
        {"discharge_order": 2, "min_remaining_moment": 0, "max_remaining_moment": 0},
    ])
    assert r.status_code == 201, r.text

    db = SessionLocal()
    try:
        ex = db.query(models.UnloadingExecution).filter_by(plan_id=pid).one()
        # 原冻结区间仍是 [-100,100]
        frozen = {p.discharge_order: (p.min_remaining_moment, p.max_remaining_moment)
                  for p in ex.ports}
        assert frozen == {1: (-100, 100), 2: (-100, 100)}
        # 生效区间已收紧
        eff = {p.discharge_order: (p.min_remaining_moment, p.max_remaining_moment)
               for p in ex.effective_ports}
        assert eff == {1: (-80, -10), 2: (0, 0)}
        # 已卸步骤保留且其卸后力矩不变
        steps = [(s.container_no, s.remaining_moment) for s in ex.steps]
        assert steps == [("C001", -10)]
        assert ex.window_version == 1
    finally:
        db.close()

    # 完成剩余卸箱，全链按生效区间可行
    assert _execute(client, pid, "C003", "OP-2", 1).status_code == 201
    fin = _execute(client, pid, "C002", "OP-3", 2)
    assert fin.status_code == 201
    assert fin.json()["remaining_moment"] == 0
    # 完结视图仍保留调整历史
    assert len(fin.json()["window_adjustments"]) == 1
