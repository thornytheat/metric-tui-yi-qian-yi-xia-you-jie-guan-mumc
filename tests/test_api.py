"""HTTP 层：页面渲染、成功/错误请求（4xx）、端到端主流程。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["metrics"] == 24


def test_index_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "指标口径注册与依赖门禁" in response.text
    assert "执行注册表体检" in response.text
    assert "同名异口径" in response.text


def test_workbench_page(client):
    response = client.get("/workbench")
    assert response.status_code == 200
    assert "口径工作台" in response.text
    assert "gmv_daily" in response.text
    assert "metric-data" in response.text


def test_review_page(client):
    response = client.get("/review")
    assert response.status_code == 200
    assert "变更影响面与发布门禁" in response.text


def test_registry_api_lists_24_metrics(client):
    response = client.get("/api/registry")
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["metric_entries"] == 24
    assert body["stats"]["metric_names"] == 20
    assert body["stats"]["conflict_count"] == 3
    assert body["stats"]["cycle_count"] == 2
    assert body["stats"]["publish_blocked_count"] == 2
    assert len(body["metrics"]) == 24
    gmv = [item for item in body["metrics"] if item["name"] == "gmv"]
    assert [item["normalized"] for item in gmv] == [
        "metric:order_amount",
        "metric:gmv@1-metric:refund_amount",
        "metric:pay_amount",
    ]


def test_check_end_to_end(client):
    response = client.post("/api/check")
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["conflict_count"] == 3
    assert body["stats"]["cycle_count"] == 2
    assert body["stats"]["publish_blocked_count"] == 2
    assert body["stats"]["cross_version_count"] == 4
    assert body["stats"]["unresolved_count"] == 0
    assert body["verdict"] == "blocked"
    assert {item["name"] for item in body["conflicts"]} == {"conversion_rate", "gmv", "uv"}
    assert len(body["cycles"]) == 2
    assert {item["key"] for item in body["publish_blocked"]} == {"pay_gmv_share@1", "uv_per_gmv@1"}

    detail = client.get(f"/api/check/{body['check_id']}")
    assert detail.status_code == 200
    assert detail.json()["check_id"] == body["check_id"]

    page = client.get(f"/result/{body['check_id']}")
    assert page.status_code == 200
    assert "注册表体检结果" in page.text
    assert "逐条依据" in page.text


def test_check_form_redirects_to_result(client):
    response = client.post("/check", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/result/chk_")
    page = client.get(location)
    assert page.status_code == 200


def test_parse_success(client):
    response = client.post(
        "/api/parse", json={"expression": "( metric:order_count / metric:visit_count ) * 100.0"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["normalized"] == "metric:order_count/metric:visit_count*100"
    assert body["changed"] is True
    assert body["metrics"] == ["order_count", "visit_count"]


def test_parse_invalid_returns_400(client):
    response = client.post("/api/parse", json={"expression": "metric:a +"})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]


def test_register_unknown_reference_returns_400(client):
    response = client.post(
        "/api/registry",
        json={
            "name": "t_broken_metric",
            "expression": "metric:not_exist_metric + metric:order_amount",
            "aggregation": "sum",
            "dimensions": ["dt"],
        },
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["missing_references"] == ["not_exist_metric"]


def test_register_bad_expression_returns_400(client):
    response = client.post(
        "/api/registry",
        json={"name": "t_bad_expr", "expression": "metric:a ** 2", "aggregation": "sum"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "expression"


def test_register_bad_aggregation_returns_400(client):
    response = client.post(
        "/api/registry",
        json={"name": "t_bad_agg", "expression": "metric:order_amount", "aggregation": "median"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["field"] == "aggregation"


def test_register_success_then_conflict(client):
    payload = {
        "name": "gmv_daily",
        "expression": "metric:order_amount - metric:refund_amount",
        "aggregation": "sum",
        "dimensions": ["dt", "channel"],
        "unit": "CNY",
        "owner": "指标体系组",
        "description": "修正后的 gmv_daily 下一版",
    }
    created = client.post("/api/registry", json=payload)
    assert created.status_code == 201
    body = created.json()
    assert body["key"] == "gmv_daily@2"
    assert body["normalized"] == "metric:order_amount-metric:refund_amount"
    assert body["caliber_group_count"] == 1
    assert body["caliber_conflict"] is False

    duplicated = client.post("/api/registry", json={**payload, "version": 2})
    assert duplicated.status_code == 409
    assert "已存在" in duplicated.json()["detail"]


def test_impact_endpoint_reports_six_downstream_two_breaking(client):
    response = client.post(
        "/api/impact",
        json={
            "name": "gmv_daily",
            "version": 1,
            "expression": "metric:order_amount - metric:refund_amount",
            "aggregation": "sum",
            "dimensions": ["dt"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["downstream_count"] == 6
    assert body["breaking_count"] == 2
    assert body["requires_sync_count"] == 4
    assert {item["key"] for item in body["breaking"]} == {"gmv_7d@1", "pay_gmv_share@1"}
    assert body["proposed"]["removed_dims"] == ["channel"]
    assert body["impact_id"].startswith("imp_")


def test_impact_unknown_metric_returns_404(client):
    response = client.post(
        "/api/impact", json={"name": "no_such_metric", "expression": "metric:order_amount"}
    )
    assert response.status_code == 404


def test_impact_invalid_expression_returns_400(client):
    response = client.post(
        "/api/impact", json={"name": "gmv_daily", "expression": "metric:a +", "dimensions": ["dt"]}
    )
    assert response.status_code == 400


def test_publish_blocked_returns_409(client):
    response = client.post("/api/publish", json={"name": "gmv", "version": 3})
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert {item["code"] for item in body["blockers"]} == {"caliber_conflict"}


def test_publish_success_after_fix(client):
    response = client.post("/api/publish", json={"name": "gmv_daily", "version": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["published"] is True
    assert body["key"] == "gmv_daily@2"


def test_publish_unknown_metric_returns_404(client):
    response = client.post("/api/publish", json={"name": "no_such_metric", "version": 1})
    assert response.status_code == 404


def test_result_page_missing_check_returns_error_page(client):
    response = client.get("/result/chk_doesnotexist")
    assert response.status_code == 200
    assert "体检记录不存在" in response.text


def test_metrics_api(client):
    response = client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 25  # 24 seed + 1 用例中登记的 gmv_daily@2
    assert "gmv" in body["names"]
