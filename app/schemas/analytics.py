from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


Period = Literal["7d", "30d", "90d", "365d", "ytd", "all"]
Granularity = Literal["day", "week", "month", "quarter", "year"]
SortOrder = Literal["asc", "desc"]


class AnalyticsFilters(BaseModel):
    dateFrom: date | None = None
    dateTo: date | None = None
    period: Period = "30d"
    granularity: Granularity = "day"
    statuses: list[str] = Field(default_factory=list)
    sellerIds: list[str] = Field(default_factory=list)
    customerIds: list[str] = Field(default_factory=list)
    productIds: list[str] = Field(default_factory=list)
    categoryIds: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    segmentIds: list[str] = Field(default_factory=list)
    orderTypeIds: list[str] = Field(default_factory=list)
    paymentConditionIds: list[str] = Field(default_factory=list)
    minValue: Decimal | None = None
    maxValue: Decimal | None = None
    activeOnly: bool = False


class AnalyticsMetadata(BaseModel):
    generatedAt: datetime
    dataThrough: datetime | None
    isPartial: bool
    warnings: list[str] = Field(default_factory=list)
    quality: dict[str, Any] = Field(default_factory=dict)


class KpiValue(BaseModel):
    value: Decimal | int | float
    previousValue: Decimal | int | float
    absoluteChange: Decimal | int | float
    percentageChange: float | None
    trend: Literal["up", "down", "stable"]
    isPositive: bool
    definition: str


class PageResponse(BaseModel):
    items: list[dict[str, Any]]
    page: int
    pageSize: int
    totalItems: int
    totalPages: int
    sort: str
    order: SortOrder
    appliedFilters: dict[str, Any]
    metadata: AnalyticsMetadata
    summary: dict[str, Any] | None = None
