from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.schemas.analytics import AnalyticsFilters
from app.services.analytics_filters import (
    adjacent_previous_bounds,
    comparison_period,
    date_bounds,
    lookback_previous_bounds,
    previous_bounds,
)

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


def test_90d_adjacent_previous_window_does_not_overlap_current() -> None:
    now = datetime(2026, 9, 11, 18, tzinfo=timezone.utc)
    filters = AnalyticsFilters(period="90d")
    start, _end = date_bounds(filters, now=now)
    prev_start, prev_end = adjacent_previous_bounds(filters, now=now)
    assert start is not None and prev_start is not None and prev_end is not None
    assert prev_end == start
    assert prev_start < prev_end
    comparison = comparison_period(filters, now=now, adjacent=True)
    assert comparison is not None
    month_shifted = comparison_period(filters, now=now)
    assert month_shifted is not None
    assert month_shifted["previousTo"] > comparison["currentFrom"]


def test_club_lookback_is_60_days_before_current_start() -> None:
    filters = AnalyticsFilters(
        dateFrom=date(2026, 9, 1),
        dateTo=date(2026, 9, 11),
        period="30d",
    )
    prev_start, prev_end = lookback_previous_bounds(filters, days=60)
    assert prev_start is not None and prev_end is not None
    assert prev_start.astimezone(BR) == datetime(2026, 7, 3, tzinfo=BR)
    assert prev_end.astimezone(BR) == datetime(2026, 9, 1, tzinfo=BR)
    comparison = comparison_period(filters, lookback_days=60)
    assert comparison == {
        "currentFrom": "2026-09-01",
        "currentTo": "2026-09-11",
        "previousFrom": "2026-07-03",
        "previousTo": "2026-08-31",
    }
