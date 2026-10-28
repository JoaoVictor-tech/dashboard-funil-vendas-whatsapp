"""Cliente da API da Helena (plataforma de atendimento) - so leitura de conversas.

Cada unidade tem a sua conta e o seu token; o cliente recebe o token no construtor.
Comportamento da API (verificado): `GET /chat/v2/session` aceita os filtros `CreatedAt.After`,
`UpdatedAt.After` (ISO 8601 em UTC) e pagina com PageNumber/PageSize (max 100).
Rate limit: 1000 req / 5 min - em backfill, uma pausa curta entre paginas basta.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

DETALHES = ["AgentDetails", "DepartmentsDetails", "ClassificationDetails", "ContactDetails"]


class HelenaClient:
    def __init__(self, token: str) -> None:
        s = get_settings()
        self._http = httpx.AsyncClient(
            base_url=s.helena_base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(60.0, connect=20.0),
        )
        self._tentativas = s.http_tentativas

    async def fechar(self) -> None:
        await self._http.aclose()

    async def _get(self, caminho: str, params: list[tuple[str, Any]]) -> dict[str, Any]:
        erro: Exception | None = None
        for tentativa in range(1, self._tentativas + 1):
            try:
                resp = await self._http.get(caminho, params=params)
                if resp.status_code == 429:
                    espera = 20 * tentativa
                    logger.warning("Helena 429 - aguardando %ss", espera)
                    await asyncio.sleep(espera)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                erro = exc
                logger.warning("Helena %s falhou (%s/%s): %s", caminho, tentativa, self._tentativas, exc)
                await asyncio.sleep(2 * tentativa)
        raise RuntimeError(f"Helena {caminho}: {erro}")

    async def sessoes(
        self, *, criadas_apos: datetime | None = None, atualizadas_apos: datetime | None = None,
        ordenar_por: str = "UpdatedAt",
    ) -> AsyncIterator[dict[str, Any]]:
        """Itera todas as sessoes que casam com o filtro, em ordem ascendente."""
        pagina = 1
        while True:
            params: list[tuple[str, Any]] = [
                ("PageSize", 100), ("PageNumber", pagina),
                ("OrderBy", ordenar_por), ("OrderDirection", "ASCENDING"),
            ]
            if criadas_apos:
                params.append(("CreatedAt.After", _iso(criadas_apos)))
            if atualizadas_apos:
                params.append(("UpdatedAt.After", _iso(atualizadas_apos)))
            params += [("IncludeDetails", d) for d in DETALHES]
            dados = await self._get("/chat/v2/session", params)
            for item in dados.get("items", []):
                yield item
            if not dados.get("hasMorePages"):
                return
            pagina += 1
            await asyncio.sleep(0.35)


    async def mensagens(self, session_id: str, desde: datetime | None = None) -> list[dict[str, Any]]:
        """Mensagens da conversa, da mais antiga para a mais nova (para em `desde`, se dado)."""
        saida: list[dict[str, Any]] = []
        pagina = 1
        while True:
            params: list[tuple[str, Any]] = [("PageSize", 100), ("PageNumber", pagina), ("OrderBy", "CreatedAt"), ("OrderDirection", "ASCENDING")]
            if desde:
                params.append(("CreatedAt.After", _iso(desde)))
            dados = await self._get(f"/chat/v1/session/{session_id}/message", params)
            saida += dados.get("items", [])
            if not dados.get("hasMorePages") or pagina >= 20:
                break
            pagina += 1
        return saida

    async def templates(self) -> list[dict[str, Any]]:
        """Todos os modelos de mensagem da conta (o nome e o que identifica o disparo)."""
        saida: list[dict[str, Any]] = []
        pagina = 1
        while True:
            dados = await self._get("/chat/v1/template", [("PageSize", 100), ("PageNumber", pagina), ("Archived", "false")])
            saida += dados.get("items", [])
            if not dados.get("hasMorePages"):
                break
            pagina += 1
        # arquivados tambem: um disparo antigo pode usar template ja arquivado
        pagina = 1
        while True:
            dados = await self._get("/chat/v1/template", [("PageSize", 100), ("PageNumber", pagina), ("Archived", "true")])
            saida += dados.get("items", [])
            if not dados.get("hasMorePages"):
                break
            pagina += 1
        return saida


def _iso(quando: datetime) -> str:
    """A API compara em UTC; um datetime ingenuo e tratado como UTC."""
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return quando.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
