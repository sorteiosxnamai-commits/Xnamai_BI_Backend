from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.auth import AuthUser, require_admin
from app.database import db_session
from app.models import ExportRun
from app.schemas.analytics import AnalyticsFilters
from app.services.exports import create_export


router = APIRouter(prefix="/api/v1/exports", tags=["exports"])


class ExportRequest(BaseModel):
    report: Literal["orders", "products", "customers", "sellers", "inventory"]
    format: Literal["csv", "xlsx"] = "csv"
    filters: AnalyticsFilters = Field(default_factory=AnalyticsFilters)


@router.post("")
def export(
    payload: ExportRequest,
    user: AuthUser = Depends(require_admin),
    db: Session = Depends(db_session),
):
    path, content_type, filename, run_id = create_export(
        db,
        username=user.username,
        report=payload.report,
        export_format=payload.format,
        filters=payload.filters,
    )
    return FileResponse(
        path,
        media_type=content_type,
        filename=filename,
        headers={"X-Export-Run-Id": str(run_id)},
        background=BackgroundTask(Path(path).unlink, missing_ok=True),
    )


@router.get("/runs")
def export_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: AuthUser = Depends(require_admin),
    db: Session = Depends(db_session),
):
    total = int(db.scalar(select(func.count(ExportRun.id))) or 0)
    rows = db.scalars(
        select(ExportRun)
        .order_by(desc(ExportRun.started_at), desc(ExportRun.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "report": row.report,
                "format": row.format,
                "status": row.status,
                "filters": row.filters,
                "startedAt": row.started_at,
                "finishedAt": row.finished_at,
                "rows": row.rows,
                "error": row.error,
            }
            for row in rows
        ],
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": (total + page_size - 1) // page_size,
    }
