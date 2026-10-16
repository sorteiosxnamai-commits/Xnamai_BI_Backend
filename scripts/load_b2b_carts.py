"""Backfill observed B2B snapshots; optionally fetch all Mercos orders first."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import String, cast, select  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Order  # noqa: E402
from app.schemas.analytics import AnalyticsFilters  # noqa: E402
from app.services.b2b_carts import b2b_status, observe_cart, cart_report  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Sync orders using configured Adaptor credentials and the saved cursor",
    )
    parser.add_argument(
        "--full", action="store_true", help="With --sync, restart from the first page"
    )
    args = parser.parse_args()
    if args.full and not args.sync:
        parser.error("--full requires --sync")
    if args.sync:
        cfg = settings()
        if not cfg.mercos_adaptor_url or not cfg.mercos_adaptor_api_key:
            print(
                "Carga não executada: configure MERCOS_ADAPTOR_URL e MERCOS_ADAPTOR_API_KEY."
            )
            return 2
        from app.sync import sync_resource

        result = asyncio.run(sync_resource("orders", full=args.full, raise_http=False))
        print(json.dumps(result, default=str, ensure_ascii=False))
        if result["status"] != "success":
            return 2
    with SessionLocal() as db:
        inserted = 0
        for order in db.scalars(
            select(Order)
            .where(cast(Order.raw["status_b2b"].as_string(), String).in_(("1", "2")))
            .execution_options(yield_per=500)
        ):
            order.b2b_status = b2b_status(order.raw.get("status_b2b"))
            inserted += observe_cart(db, order)
        db.commit()
        report = cart_report(db, AnalyticsFilters(period="all"))
        print(
            json.dumps(
                {
                    "observationsInserted": inserted,
                    "summary": report["summary"],
                    "metadata": report["metadata"],
                },
                default=str,
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
