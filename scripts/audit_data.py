"""Read-only production data audit.

Usage:
    python scripts/audit_data.py
    python scripts/audit_data.py --raw-fields --sample-limit 200 --output audit.json

The script never writes to the database and never emits raw customer values. The
optional raw inventory contains field names, occurrence counts and Python types.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys

from sqlalchemy import inspect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import SessionLocal  # noqa: E402
from app.services.data_quality import build_data_quality_report  # noqa: E402


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Tipo não serializável: {type(value).__name__}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auditoria read-only do Xnamai BI")
    parser.add_argument(
        "--raw-fields",
        action="store_true",
        help="Inclui somente nomes/tipos dos campos raw; nunca inclui valores.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=100,
        help="Máximo de raws por tabela usados no inventário (1–500).",
    )
    parser.add_argument("--output", type=Path, help="Arquivo JSON de saída")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with SessionLocal() as db:
        required_tables = {
            "customers",
            "products",
            "sellers",
            "orders",
            "order_items",
            "sync_states",
        }
        available_tables = set(inspect(db.get_bind()).get_table_names())
        missing_tables = sorted(required_tables - available_tables)
        if missing_tables:
            print(
                "Banco de auditoria não configurado ou sem schema. "
                "Defina DATABASE_URL para o PostgreSQL do BI. "
                f"Tabelas ausentes: {', '.join(missing_tables)}.",
                file=sys.stderr,
            )
            return 2
        report = build_data_quality_report(
            db,
            include_raw_inventory=args.raw_fields,
            raw_sample_limit=args.sample_limit,
        )
        db.rollback()

    payload = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    )
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"Relatório gravado em {args.output}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
