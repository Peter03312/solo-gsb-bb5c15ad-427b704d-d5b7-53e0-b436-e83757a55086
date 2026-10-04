"""录入校验测试：非法重量、重复箱号、无效槽位等。"""


def _slot_payload(**overrides):
    payload = {
        "slots": [
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2},
            {"code": "A02", "stack": "A", "tier": 2, "capacity": 100, "arm": 3},
        ],
        "adjacency": [],
        "incompatible_classes": [],
        "min_moment": -10000,
        "max_moment": 10000,
    }
    payload.update(overrides)
    return payload


def test_negative_weight_rejected(client):
    r = client.post("/api/manifest-revisions", json={
        "containers": [{"no": "X1", "weight": -5, "discharge_order": 1}]
    })
    assert r.status_code == 422
    assert "非法重量" in r.text


def test_zero_weight_rejected(client):
    r = client.post("/api/manifest-revisions", json={
        "containers": [{"no": "X1", "weight": 0, "discharge_order": 1}]
    })
    assert r.status_code == 422


def test_duplicate_container_no_rejected(client):
    r = client.post("/api/manifest-revisions", json={
        "containers": [
            {"no": "DUP", "weight": 10, "discharge_order": 1},
            {"no": "DUP", "weight": 20, "discharge_order": 2},
        ]
    })
    assert r.status_code == 422
    assert "箱号重复" in r.text


def test_adjacency_unknown_slot_rejected(client):
    r = client.post("/api/slot-revisions", json=_slot_payload(
        adjacency=[["A01", "ZZ9"]]
    ))
    assert r.status_code == 422
    assert "无效槽位" in r.text


def test_duplicate_slot_code_rejected(client):
    r = client.post("/api/slot-revisions", json=_slot_payload(
        slots=[
            {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2},
            {"code": "A01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2},
        ]
    ))
    assert r.status_code == 422
    assert "槽位编号重复" in r.text


def test_negative_capacity_rejected(client):
    r = client.post("/api/slot-revisions", json=_slot_payload(
        slots=[{"code": "A01", "stack": "A", "tier": 1, "capacity": -1, "arm": 2}]
    ))
    assert r.status_code == 422


def test_bad_moment_range_rejected(client):
    r = client.post("/api/slot-revisions", json=_slot_payload(
        min_moment=100, max_moment=10
    ))
    assert r.status_code == 422
    assert "下限不得大于上限" in r.text


def test_self_adjacency_rejected(client):
    r = client.post("/api/slot-revisions", json=_slot_payload(
        adjacency=[["A01", "A01"]]
    ))
    assert r.status_code == 422
