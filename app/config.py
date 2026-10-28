"""Configuracao via .env (nada de segredo em codigo)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict

TZ = ZoneInfo("America/Sao_Paulo")


@dataclass(frozen=True)
class Unidade:
    chave: str                      # como aparece em `loja` nas tabelas (centro / norte)
    nome: str                       # como aparece na tela
    helena_token: str | None        # conta da Helena da unidade (None = conversas ainda nao conectadas)
    reportei_meta_id: int | None    # integracao Meta Ads no Reportei (None = sem dados)
    reportei_google_id: int | None  # integracao Google Ads no Reportei

    @property
    def tem_conversas(self) -> bool:
        return bool(self.helena_token)

    @property
    def tem_meta(self) -> bool:
        return self.reportei_meta_id is not None

    @property
    def tem_google(self) -> bool:
        return self.reportei_google_id is not None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False, env_ignore_empty=True)

    # --- Banco -----------------------------------------------------------------------
    # Postgres. As tabelas deste monitor usam o prefixo mf_; vendas_dms (vendas do DMS
    # cruzadas com o contato da Helena) e alimentada por outra integracao e so e lida.
    database_url: str

    # --- Modo demonstracao -----------------------------------------------------------
    # Sem nenhuma API: as unidades contam como conectadas, o coletor nao roda e os dados
    # vem de `python -m app.cli demo` (app/demo.py).
    modo_demo: bool = False

    # --- Helena (plataforma de atendimento) ------------------------------------------
    helena_base_url: str = "https://api.helena.run"
    # Uma conta por unidade. Deixe vazio enquanto a unidade nao tiver token: o dashboard
    # mostra o aviso e segue com midia e vendas.
    helena_token_centro: str | None = None
    helena_token_norte: str | None = None

    # --- Reportei (Meta Ads + Google Ads) --------------------------------------------
    reportei_base_url: str = "https://app.reportei.com/api/v2"
    reportei_token: str | None = None
    # IDs das integracoes por unidade. Vazio = a unidade nao tem essa fonte.
    reportei_meta_centro: int | None = None
    reportei_google_centro: int | None = None
    reportei_meta_norte: int | None = None
    reportei_google_norte: int | None = None
    # O Reportei limita metrics/get-data a 100 chamadas por minuto.
    reportei_intervalo_seg: float = 0.7

    # --- Coletor ---------------------------------------------------------------------
    # Primeiro dia com investimento, conversas e vendas nas fontes.
    backfill_inicio: date = date(2026, 6, 1)
    # Conversas: a Helena e consultada por UpdatedAt a partir da marca d'agua menos esta
    # sobreposicao, para pegar mudanca de status em conversa antiga.
    sobreposicao_horas: int = 12
    # Midia: os ultimos N dias sao rebaixados a cada ciclo (a Meta ajusta a atribuicao de
    # "conversa iniciada" por ate 7 dias).
    midia_rebaixar_dias: int = 7
    coleta_intervalo_min: int = 60
    http_tentativas: int = 3
    scheduler_ativo: bool = True

    # --- Dashboard -------------------------------------------------------------------
    dashboard_username: str = "admin"
    dashboard_password: str = "changeme"
    empresa_nome: str = "Auto Vale"
    dashboard_titulo: str = "Auto Vale · Monitor de Funil"
    # Prefixo que as campanhas repetem no nome (cortado na tela para caber).
    campanha_prefixo: str = "AutoVale_"

    log_level: str = "INFO"

    @property
    def unidades(self) -> list[Unidade]:
        if self.modo_demo:
            return [Unidade("centro", "Centro", "demo", 0, 0), Unidade("norte", "Zona Norte", "demo", 0, 0)]
        return [
            Unidade("centro", "Centro", self.helena_token_centro,
                    self.reportei_meta_centro, self.reportei_google_centro),
            Unidade("norte", "Zona Norte", self.helena_token_norte,
                    self.reportei_meta_norte, self.reportei_google_norte),
        ]

    def unidade(self, chave: str) -> Unidade | None:
        return next((u for u in self.unidades if u.chave == chave), None)


@lru_cache
def get_settings() -> Settings:
    return Settings()
