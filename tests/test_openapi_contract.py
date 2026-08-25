from app.main import app


def test_required_bi_contract_is_published():
    schema = app.openapi()
    paths = schema["paths"]
    required = {
        "/api/v1/analytics/overview",
        "/api/v1/analytics/timeseries",
        "/api/v1/analytics/breakdowns",
        "/api/v1/analytics/rankings",
        "/api/v1/analytics/orders",
        "/api/v1/analytics/orders/{mercos_id}",
        "/api/v1/analytics/products",
        "/api/v1/analytics/products/{mercos_id}",
        "/api/v1/analytics/customers",
        "/api/v1/analytics/customers/{mercos_id}",
        "/api/v1/analytics/sellers",
        "/api/v1/analytics/sellers/{mercos_id}",
        "/api/v1/analytics/inventory",
        "/api/v1/analytics/geography",
        "/api/v1/analytics/cohorts",
        "/api/v1/analytics/associations",
        "/api/v1/analytics/filter-options",
        "/api/v1/data-quality",
        "/api/v1/sync/runs",
        "/api/v1/exports",
        "/api/v1/exports/runs",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/me",
        "/api/v1/crm/leads",
        "/api/v1/crm/leads/{customer_id}",
        "/api/v1/crm/leads/{customer_id}/analysis",
        "/api/v1/crm/leads/{customer_id}/claim",
        "/api/v1/crm/leads/{customer_id}/finish",
        "/api/v1/crm/dashboard",
    }
    assert required <= set(paths)

    order_parameters = {
        parameter["name"]
        for parameter in paths["/api/v1/analytics/orders"]["get"]["parameters"]
    }
    assert {
        "page",
        "page_size",
        "search",
        "sort",
        "order",
        "dateFrom",
        "dateTo",
        "statuses",
        "sellerIds",
        "customerIds",
        "excludedCustomerIds",
        "productIds",
        "categoryIds",
        "states",
        "cities",
        "minValue",
        "maxValue",
        "activeOnly",
        "period",
        "granularity",
        "segmentIds",
        "orderTypeIds",
        "paymentConditionIds",
    } <= order_parameters

    common_filters = {
        "dateFrom",
        "dateTo",
        "period",
        "granularity",
        "statuses",
        "sellerIds",
        "customerIds",
        "excludedCustomerIds",
        "productIds",
        "categoryIds",
        "states",
        "cities",
        "segmentIds",
        "orderTypeIds",
        "paymentConditionIds",
        "minValue",
        "maxValue",
        "activeOnly",
    }
    for path in (
        "/api/v1/analytics/overview",
        "/api/v1/analytics/timeseries",
        "/api/v1/analytics/breakdowns",
        "/api/v1/analytics/rankings",
        "/api/v1/analytics/products",
        "/api/v1/analytics/customers",
        "/api/v1/analytics/sellers",
        "/api/v1/analytics/inventory",
    ):
        parameters = {
            parameter["name"]
            for parameter in paths[path]["get"]["parameters"]
        }
        assert common_filters <= parameters, path
