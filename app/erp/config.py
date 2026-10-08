from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import settings as bi_settings

# Capacidades de escrita controláveis por flag. Todas desligadas por padrão.
WRITE_FLAGS = (
    "customers",
    "orders",
    "titles",
    "products",
    "inventory_publish",
    "billing",
)


class ErpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    erp_enabled: bool = False
    erp_connection_id: str = "xnamai"
    # Chave exclusiva do ERP no Adaptor. Sem ela, cai na chave do BI apenas para
    # leitura; escritas exigem chave ERP explícita (escopos no gateway).
    erp_adaptor_api_key: str = ""
    erp_bootstrap_admins: str = ""
    erp_write_customers: bool = False
    erp_write_orders: bool = False
    erp_write_titles: bool = False
    erp_write_products: bool = False
    erp_write_inventory_publish: bool = False
    erp_write_billing: bool = False
    erp_webhook_secret_hex: str = ""
    erp_webhook_max_bytes: int = 1_000_000
    erp_webhook_dedupe_window_seconds: int = 600
    erp_sync_overlap_seconds: int = 5
    erp_sync_max_pages: int = 5000
    erp_reconcile_window_hours: int = 24
    erp_job_lease_seconds: int = 300
    erp_worker_poll_seconds: float = 2.0
    # O scheduler do BI (já ativo no serviço web) esvazia a fila do ERP a cada 30 s.
    # Dispensa serviço de worker separado; desligue se um worker dedicado for usado.
    erp_queue_in_scheduler: bool = True
    # Carga inicial e sincronização incremental automáticas: enfileira, por recurso, o que
    # está vencido (nunca sincronizado = carga inicial; depois, incremental pelo checkpoint).
    # Não repete carga completa a cada reinício e não duplica jobs ativos.
    erp_auto_sync: bool = True
    erp_snapshot_retention_days: int = 90
    erp_inventory_authority: str = "mercos"
    erp_adaptor_timeout_seconds: float = 90.0
    # Autenticação individual do ERP (independente do login do BI).
    erp_jwt_secret: str = ""
    erp_access_minutes: int = 15
    erp_refresh_days: int = 7
    erp_login_max_failures: int = 5
    erp_lockout_minutes: int = 15
    erp_min_password_length: int = 12
    # Compatibilidade: aceitar o token do BI para operadores cadastrados. Desligado
    # por padrão porque o BI só tem contas compartilhadas (admin/viewer); o
    # administrador de bootstrap continua entrando pelo BI para criar os operadores.
    erp_allow_bi_operator_link: bool = False

    @property
    def bootstrap_admins(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.erp_bootstrap_admins.split(",")
            if value.strip()
        }

    @property
    def adaptor_url(self) -> str:
        return bi_settings().mercos_adaptor_url.rstrip("/")

    @property
    def read_key(self) -> str:
        return self.erp_adaptor_api_key or bi_settings().mercos_adaptor_api_key

    @property
    def has_write_key(self) -> bool:
        return bool(self.erp_adaptor_api_key)

    def write_enabled(self, flag: str) -> bool:
        return bool(getattr(self, f"erp_write_{flag}", False))


@lru_cache
def erp_settings() -> ErpSettings:
    return ErpSettings()
