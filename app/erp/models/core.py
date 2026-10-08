"""Tabelas de integração, acesso, fila, auditoria e conflitos do ERP."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.erp.common import utcnow
from app.erp.db import ErpBase

BigId = BigInteger().with_variant(Integer, "sqlite")


class ErpConnection(ErpBase):
    """Identidade local da integração. Não guarda CompanyToken."""

    __tablename__ = "erp_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ErpCapability(ErpBase):
    __tablename__ = "erp_capabilities"
    __table_args__ = (
        UniqueConstraint("connection_id", "key", name="uq_erp_capabilities_conn_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(80))
    supported_by_adaptor: Mapped[bool] = mapped_column(Boolean, default=False)
    documented_by_provider: Mapped[bool] = mapped_column(Boolean, default=False)
    account_access: Mapped[str] = mapped_column(String(12), default="unknown")
    implemented_in_erp: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class ErpOperator(ErpBase):
    __tablename__ = "erp_operators"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    roles: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Credencial própria do ERP (independente do login do BI). Nula: o operador
    # ainda não tem acesso individual e só entra pelo vínculo legado, se permitido.
    password_hash: Mapped[str | None] = mapped_column(String(300))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ErpSession(ErpBase):
    """Sessão individual e revogável. O refresh token gira a cada uso."""

    __tablename__ = "erp_sessions"
    __table_args__ = (Index("ix_erp_sessions_operator", "operator_id", "revoked_at"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operator_id: Mapped[int] = mapped_column(Integer)
    refresh_jti: Mapped[str] = mapped_column(String(36), index=True)
    previous_jti: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(80))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))


class ErpSourceSnapshot(ErpBase):
    """Payload de origem em área restrita, com retenção definida."""

    __tablename__ = "erp_source_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "resource",
            "external_key",
            "fingerprint",
            name="uq_erp_snapshots_identity_version",
        ),
        Index("ix_erp_snapshots_entity", "connection_id", "resource", "external_key"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(50))
    external_key: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(80), default="")
    source_version: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpFieldInventory(ErpBase):
    """Todo campo de origem já visto, mapeado ou não. Campo novo fica visível."""

    __tablename__ = "erp_field_inventory"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "resource", "source_key", name="uq_erp_field_inventory_key"
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(50))
    source_key: Mapped[str] = mapped_column(String(160))
    mapped: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_count: Mapped[int] = mapped_column(BigId, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sample_type: Mapped[str | None] = mapped_column(String(20))


class ErpSyncCheckpoint(ErpBase):
    """Progresso de transporte (`transport_cursor`) e de processamento (`cursor`)."""

    __tablename__ = "erp_sync_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "resource", "scope", name="uq_erp_checkpoints_identity"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(50))
    scope: Mapped[str] = mapped_column(String(80), default="")
    cursor: Mapped[str | None] = mapped_column(Text)
    transport_cursor: Mapped[str | None] = mapped_column(Text)
    data_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="never")
    records: Mapped[int] = mapped_column(Integer, default=0)
    unresolved: Mapped[int] = mapped_column(Integer, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ErpSyncRun(ErpBase):
    __tablename__ = "erp_sync_runs"
    __table_args__ = (Index("ix_erp_sync_runs_started", "connection_id", "started_at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(50))
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30))
    job_id: Mapped[int | None] = mapped_column(BigId)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_before: Mapped[str | None] = mapped_column(Text)
    cursor_after: Mapped[str | None] = mapped_column(Text)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    received: Mapped[int] = mapped_column(Integer, default=0)
    persisted: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    quarantined: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ErpQuarantine(ErpBase):
    __tablename__ = "erp_quarantine"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "resource", "external_key", name="uq_erp_quarantine_entity"
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str] = mapped_column(String(50))
    external_key: Mapped[str] = mapped_column(String(200))
    run_id: Mapped[int | None] = mapped_column(BigId)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpJob(ErpBase):
    """Fila persistida consumida pelo worker (claim atômico + lease)."""

    __tablename__ = "erp_jobs"
    __table_args__ = (
        Index("ix_erp_jobs_pick", "status", "run_after"),
        # No máximo um job ativo equivalente: rajadas de webhook não multiplicam syncs.
        Index(
            "uq_erp_jobs_active",
            "kind",
            "connection_id",
            "resource",
            "mode",
            unique=True,
            postgresql_where=text("status IN ('queued','processing','waiting_rate_limit')"),
            sqlite_where=text("status IN ('queued','processing','waiting_rate_limit')"),
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    connection_id: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str | None] = mapped_column(String(50))
    mode: Mapped[str] = mapped_column(String(20), default="incremental")
    status: Mapped[str] = mapped_column(String(30), default="queued")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    requested_by: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpOperation(ErpBase):
    """Intenção de escrita externa (outbox) com estado explícito."""

    __tablename__ = "erp_integration_operations"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "operator",
            "kind",
            "idempotency_key",
            name="uq_erp_operations_idempotency",
        ),
        Index("ix_erp_operations_pick", "status", "next_attempt_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(50))
    target_resource: Mapped[str] = mapped_column(String(50))
    target_external_id: Mapped[str | None] = mapped_column(String(200))
    local_ref: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    base_fingerprint: Mapped[str | None] = mapped_column(String(64))
    base_snapshot: Mapped[dict | None] = mapped_column(JSON)
    expected_version: Mapped[int | None] = mapped_column(Integer)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | list | None] = mapped_column(JSON)
    external_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    lease_token: Mapped[str | None] = mapped_column(String(36))
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mirror_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    reconcile_evidence: Mapped[dict | None] = mapped_column(JSON)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ErpIdempotencyKey(ErpBase):
    """Idempotência de operações locais (compras, estoque, financeiro)."""

    __tablename__ = "erp_idempotency_keys"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "operator",
            "operation",
            "key",
            name="uq_erp_idempotency_identity",
        ),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(200))
    operation: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict | list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErpWebhookInbox(ErpBase):
    __tablename__ = "erp_webhook_inbox"
    __table_args__ = (
        Index("ix_erp_inbox_pick", "status", "received_at"),
        Index("ix_erp_inbox_dedupe", "connection_id", "dedupe_key", "received_at"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    delivery_id: Mapped[str | None] = mapped_column(String(120))
    event: Mapped[str | None] = mapped_column(String(120))
    dedupe_key: Mapped[str] = mapped_column(String(80))
    body_hash: Mapped[str] = mapped_column(String(64))
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), default="received")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class ErpConflict(ErpBase):
    __tablename__ = "erp_conflicts"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(64))
    operation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_external_id: Mapped[str | None] = mapped_column(String(200))
    fields: Mapped[list] = mapped_column(JSON, default=list)
    base: Mapped[dict | None] = mapped_column(JSON)
    local: Mapped[dict | None] = mapped_column(JSON)
    external: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    resolution: Mapped[str | None] = mapped_column(String(30))
    resolved_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ErpAuditEvent(ErpBase):
    __tablename__ = "erp_audit_events"
    __table_args__ = (Index("ix_erp_audit_at", "at"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    connection_id: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(80))
    resource: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSON)
