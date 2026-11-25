"""Fotos do catálogo público do Mercos.

A API autenticada devolve só o hash SHA-512. O B2B publica a URL em
`https://arquivos.mercos.com/...`. A foto escolhida no ERP não é substituída.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.erp.config import erp_settings

log = logging.getLogger(__name__)

_MISS_TTL = 600.0
_misses: dict[str, float] = {}


def media_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"images": [], "videos": [], "parentId": None}
    images: list[str] = []
    cover = payload.get("imagem")
    if isinstance(cover, str) and cover.startswith("https://"):
        images.append(cover)
    for item in payload.get("imagens") or []:
        if isinstance(item, str) and item.startswith("https://") and item not in images:
            images.append(item)
    videos = [
        item for item in (payload.get("videos") or [])
        if isinstance(item, str) and item.startswith("https://")
    ]
    parent = payload.get("produto_agregador_id")
    return {"images": images[:12], "videos": videos[:8], "parentId": str(parent) if parent else None}


def apply_media(row: Any, media: dict[str, Any]) -> bool:
    """Grava URL só onde o operador ainda não informou foto ou vídeo."""
    changed = False
    images = media.get("images") or []
    videos = media.get("videos") or []
    if images and not getattr(row, "image_url", None):
        row.image_url = images[0]
        changed = True
    if images and not (getattr(row, "photos", None) or []):
        row.photos = images
        changed = True
    if videos and not (getattr(row, "videos", None) or []):
        row.videos = videos
        changed = True
    return changed


def _get_json(url: str, timeout: float) -> Any | None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "XnamaiERP/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _lookup(base: str, external_id: str, name: str | None) -> dict[str, Any]:
    detail = _get_json(f"{base}/api_b2b/v1/produtos/{external_id}", 6)
    media = media_from_payload(detail)
    if media["images"]:
        return media
    parent_id = media["parentId"]
    if parent_id and parent_id != external_id:
        parent = media_from_payload(_get_json(f"{base}/api_b2b/v1/produtos/{parent_id}", 6))
        if parent["images"]:
            return parent
    query = (name or "").strip()
    if not query:
        return media
    found = _get_json(
        f"{base}/api_b2b/v1/produtos?busca={urllib.parse.quote(query[:80])}",
        8,
    )
    if not isinstance(found, list):
        return media
    for item in found:
        if not isinstance(item, dict):
            continue
        if str(item.get("produto_id") or "") == external_id:
            picked = media_from_payload(item)
            if picked["images"]:
                return picked
        for grade in item.get("grades_v3") or []:
            if isinstance(grade, dict) and str(grade.get("produto_id") or "") == external_id:
                picked = media_from_payload(item)
                if picked["images"]:
                    return picked
    return media


def attach_storefront_images(db: Session, rows: list[Any]) -> None:
    """Preenche a foto dos produtos desta página e grava para a próxima consulta."""
    settings = erp_settings()
    if not settings.erp_storefront_images:
        return
    base = (settings.erp_storefront_url or "").rstrip("/")
    if not base:
        return
    now = time.monotonic()
    pending = []
    for row in rows:
        external_id = str(getattr(row, "external_id", "") or "")
        if not external_id.isdigit() or getattr(row, "image_url", None):
            continue
        missed = _misses.get(external_id)
        if missed is not None and now - missed < _MISS_TTL:
            continue
        pending.append(row)
    if not pending:
        return
    found: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {
            pool.submit(_lookup, base, str(row.external_id), getattr(row, "name", None)): row
            for row in pending
        }
        for job, row in jobs.items():
            external_id = str(row.external_id)
            try:
                found[external_id] = job.result()
            except Exception:
                log.warning("foto do produto %s indisponível", external_id)
                found[external_id] = {"images": [], "videos": []}
    changed = False
    for row in pending:
        media = found.get(str(row.external_id)) or {"images": [], "videos": []}
        if apply_media(row, media):
            changed = True
        elif not media.get("images"):
            _misses[str(row.external_id)] = time.monotonic()
    if changed:
        db.commit()
