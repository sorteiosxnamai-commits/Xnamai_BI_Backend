from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CoverageMetrics(BaseModel):
    ordersWithItemsPct: float
    ordersWithCustomerPct: float
    ordersWithSellerPct: float
    itemsWithProductPct: float
    recognizedStatusPct: float


class IntegrityMetrics(BaseModel):
    ordersWithoutItems: int
    ordersWithoutCustomer: int
    ordersWithoutSeller: int
    itemsWithoutProduct: int
    orderTotalDivergences: int


class DateRange(BaseModel):
    min: datetime | None
    max: datetime | None


class SyncQuality(BaseModel):
    resource: str
    status: str
    cursor: str | None
    lastSuccessAt: datetime | None
    records: int
    error: str | None


class QualityMetadata(BaseModel):
    generatedAt: datetime
    dataThrough: datetime | None
    isPartial: bool
    warnings: list[str] = Field(default_factory=list)


class DataQualityResponse(BaseModel):
    coverage: CoverageMetrics
    integrity: IntegrityMetrics
    dateRange: DateRange
    sync: list[SyncQuality]
    warnings: list[str]
    counts: dict[str, int]
    zeroValues: dict[str, int]
    duplicates: dict[str, int]
    missingDimensions: dict[str, int]
    emptyRaw: dict[str, int]
    metadata: QualityMetadata
    rawFieldInventory: dict[str, dict[str, Any]] | None = None
