"""Gera docs/erp/field-mapping.md a partir do registro de recursos do ERP."""

from pathlib import Path

from app.erp.registry import render_field_mapping

target = Path(__file__).resolve().parents[1] / "docs" / "erp" / "field-mapping.md"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(render_field_mapping(), encoding="utf-8")
print(f"escrito {target}")
