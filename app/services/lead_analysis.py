import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import _aware
from app.config import settings
from app.models import CrmAttendance
from app.services.crm import _iso, _upsert_attendance, lead_detail

log = logging.getLogger(__name__)
CACHE_HOURS = 24

INSTRUCTIONS = (
    "Voce e um analista comercial B2B da XNamai (distribuidora/atacado). "
    "Use os dados internos do cliente e pesquise informacoes publicas na web quando fizer sentido. "
    "Responda SOMENTE com JSON valido (sem markdown), neste formato: "
    '{"companyProfile":"resumo curto da empresa","sector":"ramo/setor identificado",'
    '"website":"url ou null","publicProducts":["produtos publicos"],'
    '"purchasePreferences":["preferencias do historico"],"approachStrategy":"melhor abordagem",'
    '"openingMessage":"mensagem WhatsApp curta","talkingPoints":["pontos de conversa"],'
    '"risksOrCautions":["cuidados"],"sources":[{"title":"titulo","url":"https://..."}],'
    '"confidence":"alta|media|baixa"} '
    "Priorize dados do Brasil. Se nao encontrar algo, use null ou lista vazia - nao invente."
)


def _whatsapp_url(*phones: str | None) -> str | None:
    for phone in phones:
        if not phone:
            continue
        digits = re.sub(r"\D", "", phone)
        if len(digits) < 10:
            continue
        if not digits.startswith("55"):
            digits = "55" + digits.lstrip("0")
        return f"https://wa.me/{digits}"
    return None


def _contact_from_lead(lead: dict) -> dict:
    phone = lead.get("mobile") or lead.get("phone") or lead.get("extraPhone")
    return {
        "phone": phone,
        "email": lead.get("email") or lead.get("extraEmail"),
        "whatsappUrl": _whatsapp_url(lead.get("mobile"), lead.get("phone"), lead.get("extraPhone")),
    }


def _compact_lead_context(lead: dict) -> dict:
    return {
        "name": lead.get("name"),
        "legalName": lead.get("legalName"),
        "tradeName": lead.get("tradeName"),
        "document": lead.get("document"),
        "city": lead.get("city"),
        "state": lead.get("state"),
        "address": lead.get("address"),
        "branch": lead.get("branch"),
        "segment": lead.get("segment"),
        "orders": lead.get("orders"),
        "revenue": lead.get("revenue"),
        "ticketAverage": lead.get("ticketAverage"),
        "lastOrderAt": lead.get("lastOrderAt"),
        "daysSinceLastOrder": lead.get("daysSinceLastOrder"),
        "phone": lead.get("phone"),
        "mobile": lead.get("mobile"),
        "email": lead.get("email"),
        "mostBoughtProducts": [
            {"name": item.get("name"), "quantity": item.get("quantity"), "revenue": item.get("revenue")}
            for item in (lead.get("mostBoughtProducts") or [])[:8]
        ],
        "lastProducts": [
            {"name": item.get("name"), "quantity": item.get("quantity"), "total": item.get("total")}
            for item in (lead.get("lastProducts") or [])[:5]
        ],
    }


def _build_prompt(lead: dict) -> str:
    context = _compact_lead_context(lead)
    search_hint = " ".join(
        part
        for part in [
            lead.get("legalName") or lead.get("name"),
            lead.get("tradeName"),
            lead.get("city"),
            lead.get("state"),
            lead.get("document"),
        ]
        if part
    )
    return (
        f"Pesquise na web informacoes publicas sobre esta empresa brasileira: {search_hint}.\n"
        f"Dados internos do CRM:\n{json.dumps(context, ensure_ascii=False, default=str)}"
    )


def _extract_output_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def _extract_sources(payload: dict) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for item in payload.get("output") or []:
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        for source in action.get("sources") or []:
            url = source.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({"title": source.get("title") or url, "url": url})
    return sources


def _parse_analysis(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as error:
        log.warning("OpenAI analysis JSON parse failed: %s", error)
        raise HTTPException(502, "Resposta da IA em formato invalido") from error
    if not isinstance(parsed, dict):
        raise HTTPException(502, "Resposta da IA em formato invalido")
    return parsed


def _call_openai(prompt: str) -> dict:
    cfg = settings()
    if not cfg.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY nao configurada")
    body = {
        "model": cfg.openai_model,
        "instructions": INSTRUCTIONS,
        "input": prompt,
        "tools": [
            {
                "type": "web_search",
                "user_location": {"type": "approximate", "country": "BR"},
            }
        ],
        "include": ["web_search_call.action.sources"],
    }
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        response = client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {cfg.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 400:
        log.error("OpenAI Responses API error %s: %s", response.status_code, response.text[:800])
        raise HTTPException(502, "Falha ao gerar analise com IA")
    payload = response.json()
    text = _extract_output_text(payload)
    if not text:
        raise HTTPException(502, "IA nao retornou analise")
    analysis = _parse_analysis(text)
    if not analysis.get("sources"):
        analysis["sources"] = _extract_sources(payload)
    return analysis


def analyze_lead(db: Session, customer_id: str, *, refresh: bool = False) -> dict:
    lead = lead_detail(db, customer_id)
    contact = _contact_from_lead(lead)
    now = datetime.now(timezone.utc)

    row = db.scalar(select(CrmAttendance).where(CrmAttendance.customer_mercos_id == customer_id))
    if row and row.ai_analysis and not refresh:
        cached_at = _aware(row.ai_analysis_at)
        if cached_at and (now - cached_at) < timedelta(hours=CACHE_HOURS):
            return {
                "contact": contact,
                "analysis": row.ai_analysis,
                "cached": True,
                "generatedAt": _iso(row.ai_analysis_at),
            }

    analysis = _call_openai(_build_prompt(lead))
    row = _upsert_attendance(db, customer_id)
    row.ai_analysis = analysis
    row.ai_analysis_at = now
    row.updated_at = now
    db.add(row)
    db.commit()

    return {
        "contact": contact,
        "analysis": analysis,
        "cached": False,
        "generatedAt": _iso(now),
    }
