"""Sonda SOMENTE LEITURA do sandbox Mercos via Adaptor.

Para cada um dos 12 recursos busca a primeira página e relata: status HTTP
traduzido, paginação, tipos dos campos e quais ainda NÃO têm mapeamento no ERP.
Só imprime nomes e tipos de campos, nunca valores (sem PII). Não faz POST/PUT.

    MERCOS_ADAPTOR_URL=... MERCOS_ADAPTOR_API_KEY=... python scripts/erp_sandbox_probe.py
    python scripts/erp_sandbox_probe.py --out docs/erp/sandbox-report.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.erp.integrations.mercos_client import AdaptorError, MercosAdaptorClient  # noqa: E402
from app.erp.registry import REGISTRY, SYNC_ORDER  # noqa: E402
from app.erp.sync.rows import _typed_keys  # noqa: E402


def mapped(definition, key: str) -> bool:
    child_names = {name for child in definition.children for name in child.keys}
    if "[]." in key:
        parent, inner = key.split("[].", 1)
        if parent in child_names:
            child_known = {k for child in definition.children for f in child.fields for k in f.keys} | {"id", "excluido"}
            return inner in child_known
        return parent in definition.known_keys
    return key in definition.known_keys


async def probe_resource(client, alias: str) -> dict:
    definition = REGISTRY[alias]
    try:
        page = await client.list_page(alias, None)
    except AdaptorError as exc:
        return {"resource": alias, "ok": False, "kind": exc.kind, "status": exc.status_code,
                "retryAfter": exc.retry_after, "message": exc.message}
    keys: dict[str, str] = {}
    for row in page.data:
        for key, kind in _typed_keys(row).items():
            if keys.get(key) in (None, "NoneType"):
                keys[key] = kind
    return {
        "resource": alias,
        "ok": True,
        "rows": len(page.data),
        "pageCursorPresent": bool(page.page_cursor),
        "nextCursorPresent": bool(page.next_cursor),
        "fields": dict(sorted(keys.items())),
        "unmapped": sorted(k for k in keys if not mapped(definition, k)),
        "missingId": sum(1 for row in page.data if row.get("id") in (None, "")) if alias != "product-prices" else 0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="grava o relatório JSON neste caminho")
    args = parser.parse_args()
    client = MercosAdaptorClient()
    if not client.configured:
        print("Configure MERCOS_ADAPTOR_URL e MERCOS_ADAPTOR_API_KEY (sandbox).", file=sys.stderr)
        return 2
    report = {"resources": []}
    for alias in SYNC_ORDER:
        result = await probe_resource(client, alias)
        report["resources"].append(result)
        print(f"{alias:22} {'OK ' if result['ok'] else 'ERRO'} "
              f"{result.get('rows', '-')} linhas  não mapeados: {result.get('unmapped', result.get('kind'))}")
        if result.get("kind") == "rate_limited":
            print("429: pare e rode de novo depois; a cota é da conta inteira.", file=sys.stderr)
            break
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
