"""Gera docs/erp/coverage.md a partir da matriz de capacidades do ERP."""

from pathlib import Path

from app.erp.coverage_doc import render_coverage

target = Path(__file__).resolve().parents[1] / "docs" / "erp" / "coverage.md"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(render_coverage(), encoding="utf-8")
print(f"escrito {target}")
