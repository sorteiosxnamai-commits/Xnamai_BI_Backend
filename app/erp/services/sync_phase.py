"""Fase da carga de um recurso, separando o que antes aparecia como "sincronizado".

- done: houve sucesso confirmado (pode estar atualizando agora);
- running: carga em andamento ou aguardando limite, ainda sem sucesso;
- denied: a conta/Adaptor negou o recurso (403/404): bloqueio preservado, não é erro de carga;
- error: falha, interrupção, execução parcial ou quarentena pendente;
- not_started: nunca houve tentativa.
Tentativa não é conclusão e 403 não é sincronização concluída."""

PHASES = ("done", "running", "denied", "error", "not_started")
RUNNING = ("running", "waiting_rate_limit", "queued", "continuing")
DENIED = ("forbidden", "unavailable")
ERRORS = ("failed", "interrupted", "partial", "partial_quarantine", "auth_failed", "cancelled")


def sync_phase(status: str | None, last_success_at) -> str:
    status = status or "never"
    if status in DENIED:
        return "denied"
    if status == "never":
        return "not_started"
    if last_success_at is not None and (status == "success" or status in RUNNING):
        return "done"
    if status in RUNNING:
        return "running"
    if status == "success":
        return "done"
    if status in ERRORS:
        return "error"
    return "error"


def count_phases(items: list[dict]) -> dict[str, int]:
    out = {phase: 0 for phase in PHASES}
    for item in items:
        out[item["phase"]] += 1
    return out
