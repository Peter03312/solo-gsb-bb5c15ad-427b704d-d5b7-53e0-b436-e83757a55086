"""装船时间表 API 端到端测试。"""

from tests.test_api_workflow import MANIFEST_PAYLOAD, SLOT_PAYLOAD


def _seed_and_confirm(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    return plan  # 方案：C001->A01(A,tier1)，C002->B01(B,tier1)


def _entries(duration=10, deadline=100, **over):
    base = {
        "crane_count": 1,
        "entries": [
            {"no": "C001", "duration": duration, "release_minute": 0,
             "deadline_minute": deadline},
            {"no": "C002", "duration": duration, "release_minute": 0,
             "deadline_minute": deadline},
        ],
    }
    base.update(over)
    return base


def test_create_schedule_happy_path(client):
    plan = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=_entries())
    assert r.status_code == 201, r.text
    sch = r.json()
    assert sch["plan_id"] == plan["plan_id"]
    assert sch["revision_no"] == 1
    assert sch["status"] == "current"
    assert sch["crane_count"] == 1
    # 两箱在不同栈、无先后关系，单桥串行 makespan=20
    assert sch["makespan"] == 20
    starts = {e["container_no"]: (e["start_minute"], e["finish_minute"], e["crane_no"])
              for e in sch["entries"]}
    # 箱号序字典序：C001 先 [0,10) 桥1
    assert starts["C001"] == (0, 10, 1)
    assert starts["C002"][0] == 10 and starts["C002"][1] == 20
    assert starts["C002"][2] == 1


def test_two_cranes_parallel_finish_at_10(client):
    plan = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules",
                    json=_entries() | {"crane_count": 2})
    assert r.status_code == 201
    sch = r.json()
    assert sch["makespan"] == 10
    cranes = {e["container_no"]: e["crane_no"] for e in sch["entries"]}
    starts = {e["container_no"]: e["start_minute"] for e in sch["entries"]}
    assert starts == {"C001": 0, "C002": 0}
    # 按箱号字典序：C001 取 1 号桥，C002 取 2 号桥
    assert cranes == {"C001": 1, "C002": 2}


def test_schedule_on_unconfirmed_plan_rejected_409(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=_entries())
    assert r.status_code == 409
    assert "已确认" in r.json()["detail"]


def test_schedule_missing_and_extra_entries_rejected_422(client):
    plan = _seed_and_confirm(client)
    # 缺 C002
    r1 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "crane_count": 1,
        "entries": [{"no": "C001", "duration": 10, "release_minute": 0,
                     "deadline_minute": 100}],
    })
    assert r1.status_code == 422 and "缺少" in r1.json()["detail"]
    # 多 C999
    body = _entries()
    body["entries"].append({"no": "C999", "duration": 10, "release_minute": 0,
                            "deadline_minute": 100})
    r2 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=body)
    assert r2.status_code == 422 and "多余" in r2.json()["detail"]
    # 被拒绝时不落库
    assert client.get("/api/schedules").json() == []


def test_infeasible_window_rejected_422_not_saved(client):
    plan = _seed_and_confirm(client)
    # 单桥两箱都必须在 [0,10) 内完工 -> 不可能
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules",
                    json=_entries(deadline=10))
    assert r.status_code == 422
    assert "无解" in r.json()["detail"]
    assert client.get("/api/schedules").json() == []


def test_invalid_payload_rejected_422(client):
    plan = _seed_and_confirm(client)
    # 岸桥数为 0
    r0 = client.post(f"/api/plans/{plan['plan_id']}/schedules",
                     json=_entries() | {"crane_count": 0})
    assert r0.status_code == 422
    # 时长非正
    r1 = client.post(f"/api/plans/{plan['plan_id']}/schedules",
                     json=_entries(duration=0))
    assert r1.status_code == 422
    # 负开工
    body = _entries()
    body["entries"][0]["release_minute"] = -1
    r2 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=body)
    assert r2.status_code == 422
    # 时间窗倒置（最早开工晚于最迟完工）
    body = _entries()
    body["entries"][0]["release_minute"] = 50
    body["entries"][0]["deadline_minute"] = 40
    r3 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=body)
    assert r3.status_code == 422 and "时间窗" in r3.text
    # 提交箱号重复
    body = _entries()
    body["entries"][1]["no"] = "C001"
    r4 = client.post(f"/api/plans/{plan['plan_id']}/schedules", json=body)
    assert r4.status_code == 422 and "重复" in r4.text


def test_get_schedule_and_list_filters_historical(client):
    p1 = _seed_and_confirm(client)
    client.post(f"/api/plans/{p1['plan_id']}/schedules",
                json=_entries() | {"crane_count": 2})

    # 新箱清单修订 -> 修订并确认 p2（同根），p1 变 superseded
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    # 当前排程列表为空（p2 尚未排程；p1 排程已转历史）
    current = client.get("/api/schedules").json()
    assert current == []

    # 含历史参数可查到旧排程，标记 historical
    allsch = client.get("/api/schedules?historical=true").json()
    assert len(allsch) == 1
    assert allsch[0]["plan_id"] == p1["plan_id"]
    assert allsch[0]["status"] == "historical"

    # 旧排程按方案 ID 仍可查
    old = client.get(f"/api/plans/{p1['plan_id']}/schedules")
    assert old.status_code == 200
    assert old.json()["status"] == "historical"

    # 为新修订生成当前排程（空箱清单外：1 箱）
    r = client.post(f"/api/plans/{p2['plan_id']}/schedules", json={
        "crane_count": 1,
        "entries": [{"no": "C001", "duration": 7, "release_minute": 0,
                     "deadline_minute": 50}],
    })
    assert r.status_code == 201
    assert r.json()["makespan"] == 7
    current = client.get("/api/schedules").json()
    assert [s["plan_id"] for s in current] == [p2["plan_id"]]
    assert current[0]["status"] == "current"


def test_reschedule_replaces_same_plan_schedule(client):
    plan = _seed_and_confirm(client)
    client.post(f"/api/plans/{plan['plan_id']}/schedules", json=_entries())
    r2 = client.post(f"/api/plans/{plan['plan_id']}/schedules",
                     json=_entries() | {"crane_count": 2})
    assert r2.status_code == 201
    sch = client.get("/api/schedules").json()
    assert len(sch) == 1
    assert sch[0]["crane_count"] == 2 and sch[0]["makespan"] == 10


def test_get_schedule_404(client):
    plan = _seed_and_confirm(client)
    assert client.get(f"/api/plans/{plan['plan_id']}/schedules").status_code == 404
    assert client.get("/api/plans/nope/schedules").status_code == 404


def test_save_time_recheck_rejects_if_plan_superseded(client, monkeypatch):
    """求解完成后、落库前方案被并发取代：锁内复核须拒绝且不保存。"""
    from app import services

    p1 = _seed_and_confirm(client)

    # 准备一个已确认的同根新修订 p2（取代 p1 的时刻点稍后发生）
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()

    real_solve = services.solve_loading

    def raced_solve(jobs, crane_count):
        result = real_solve(jobs, crane_count)
        # 在求解之后、保存之前，另一请求确认了新修订，p1 被取代
        client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
            "slots_revision": 1, "manifest_revision": 2
        })
        return result

    monkeypatch.setattr(services, "solve_loading", raced_solve)
    r = client.post(f"/api/plans/{p1['plan_id']}/schedules", json=_entries())
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    # 无任何排程落库
    assert client.get("/api/schedules?historical=true").json() == []


def test_empty_manifest_plan_schedule_is_empty_makespan_zero(client):
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json={"containers": []})
    plan = client.post("/api/plans/compute", json={}).json()
    assert plan["assignments"] == []
    ok = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 1
    })
    assert ok.status_code == 200
    r = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "crane_count": 2, "entries": []
    })
    assert r.status_code == 201
    sch = r.json()
    assert sch["entries"] == [] and sch["makespan"] == 0
    assert sch["crane_count"] == 2
    # 空箱方案提交多余箱项仍拒绝
    bad = client.post(f"/api/plans/{plan['plan_id']}/schedules", json={
        "crane_count": 2,
        "entries": [{"no": "GHOST", "duration": 1, "release_minute": 0,
                     "deadline_minute": 10}],
    })
    assert bad.status_code == 422 and "多余" in bad.json()["detail"]
