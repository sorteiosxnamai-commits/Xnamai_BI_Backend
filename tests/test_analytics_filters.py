from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import comparison_period, previous_bounds

BR = ZoneInfo("America/Sao_Paulo")


def test_previous_bounds_aligns_same_calendar_days_last_month() -> None:
    filters = AnalyticsFilters(
        dateFrom=date(2026, 9, 1),
        dateTo=date(2026, 9, 9),
        period="all",
    )
    start, end = previous_bounds(filters)
    assert start is not None and end is not None
    assert start.astimezone(BR) == datetime(2026, 8, 1, tzinfo=BR)
    assert end.astimezone(BR) == datetime(2026, 8, 10, tzinfo=BR)
    assert comparison_period(filters) == {
        "currentFrom": "2026-09-01",
        "currentTo": "2026-09-09",
        "previousFrom": "2026-08-01",
        "previousTo": "2026-08-09",
    }


def test_previous_bounds_clamps_end_of_month() -> None:
    filters = AnalyticsFilters(
        dateFrom=date(2026, 3, 1),
        dateTo=date(2026, 3, 31),
        period="all",
    )
    start, end = previous_bounds(filters)
    assert start is not None and end is not None
    assert start.astimezone(BR) == datetime(2026, 2, 1, tzinfo=BR)
    assert end.astimezone(BR) == datetime(2026, 3, 1, tzinfo=BR)
    comparison = comparison_period(filters)
    assert comparison is not None
    assert comparison["previousFrom"] == "2026-02-01"
    assert comparison["previousTo"] == "2026-02-28"


def test_ytd_compares_same_dates_last_year() -> None:
    now = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
    filters = AnalyticsFilters(period="ytd")
    start, end = previous_bounds(filters, now=now)
    assert start is not None and end is not None
    assert start.astimezone(BR) == datetime(2025, 1, 1, tzinfo=BR)
    comparison = comparison_period(filters, now=now)
    assert comparison is not None
    assert comparison["previousFrom"] == "2025-01-01"
    assert comparison["previousTo"] == "2025-09-09"
