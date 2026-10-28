"""Cliente do Reportei (API v2) - investimento e resultados de Meta Ads e Google Ads.

Como a API funciona (verificado, 2026-09):
- `GET /metrics?integration_slug=facebook_ads` devolve a DEFINICAO de cada metrica
  (id, reference_key, component, metrics[], dimensions[]...). O `POST /metrics/get-data`
  exige esse objeto inteiro em `metrics` - mandar so a chave da 422.
- A definicao pode ser editada antes de enviar: na tabela de anuncios (`fb_ads:ads`),
  trocar `metrics` por ["campaign_name", "results", "spend"] faz a resposta trazer o
  ID do anuncio E o nome da campanha - e a ponte com o utm.campaign da Helena.
- Limite: 100 chamadas de get-data por minuto (companies/settings).
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import date
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

META_TOTAIS = [
    "fb_ads:spend", "fb_ads:spend_facebook", "fb_ads:spend_instagram",
    "fb_ads:actions_onsite_conversion.messaging_conversation_started_7d",
    "fb_ads:actions_onsite_conversion.messaging_conversation_started_7d_facebook",
    "fb_ads:actions_onsite_conversion.messaging_conversation_started_7d_instagram",
]
GOOGLE_METRICAS = [
    "gads:campaign_performance_by_id", "gads:custom_conversion_actions", "gads:cost_micros",
    "gads:conversions",
]


def _num(valor: Any) -> float:
    if isinstance(valor, dict):
        valor = valor.get("value")
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


class ReporteiClient:
    def __init__(self) -> None:
        s = get_settings()
        self._http = httpx.AsyncClient(
            base_url=s.reportei_base_url,
            headers={"Authorization": f"Bearer {s.reportei_token}", "Accept": "application/json"},
            timeout=httpx.Timeout(120.0, connect=20.0),
        )
        self._intervalo = s.reportei_intervalo_seg
        self._tentativas = s.http_tentativas
        self._defs: dict[str, dict[str, dict[str, Any]]] = {}

    async def fechar(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- definicoes
    async def definicoes(self, slug: str) -> dict[str, dict[str, Any]]:
        if slug in self._defs:
            return self._defs[slug]
        todas: dict[str, dict[str, Any]] = {}
        pagina = 1
        while True:
            resp = await self._http.get("/metrics", params={"integration_slug": slug, "per_page": 100, "page": pagina})
            resp.raise_for_status()
            corpo = resp.json()
            for m in corpo.get("data", []):
                todas[m["reference_key"]] = m
            if pagina >= corpo.get("meta", {}).get("last_page", 1):
                break
            pagina += 1
        self._defs[slug] = todas
        return todas

    async def _get_data(self, integration_id: int, defs: list[dict[str, Any]], inicio: date, fim: date) -> dict[str, Any]:
        corpo = {"integration_id": integration_id, "start": inicio.isoformat(), "end": fim.isoformat(), "metrics": defs}
        erro: Exception | None = None
        for tentativa in range(1, self._tentativas + 1):
            try:
                resp = await self._http.post("/metrics/get-data", json=corpo)
                if resp.status_code == 429:
                    logger.warning("Reportei 429 - aguardando 65s")
                    await asyncio.sleep(65)
                    continue
                resp.raise_for_status()
                await asyncio.sleep(self._intervalo)
                return resp.json().get("data", {})
            except (httpx.HTTPError, ValueError) as exc:
                erro = exc
                logger.warning("Reportei get-data falhou (%s/%s): %s", tentativa, self._tentativas, exc)
                await asyncio.sleep(3 * tentativa)
        raise RuntimeError(f"Reportei get-data: {erro}")

    # ---------------------------------------------------------------- Meta Ads
    async def meta_anuncios(self, integration_id: int, inicio: date, fim: date) -> list[dict[str, Any]]:
        """Anuncios com resultado no periodo: id, nome, campanha, resultados, gasto."""
        defs = await self.definicoes("facebook_ads")
        d = copy.deepcopy(defs["fb_ads:ads"])
        d["metrics"] = ["campaign_name", "results", "spend", "impressions"]
        dados = await self._get_data(integration_id, [d], inicio, fim)
        linhas = []
        for bloco in dados.values():
            for row in bloco.get("values", []) or []:
                if not isinstance(row[0], dict) or not row[0].get("id"):
                    continue
                linhas.append({
                    "anuncio_id": row[0]["id"], "anuncio_nome": row[0].get("text"), "campanha": row[1],
                    "resultados": _num(row[2]),
                    "tipo": row[2].get("title") if isinstance(row[2], dict) else None,
                    "investimento": _num(row[3]), "impressoes": int(_num(row[4])),
                })
        return linhas

    async def meta_campanhas(self, integration_id: int, inicio: date, fim: date) -> list[dict[str, Any]]:
        defs = await self.definicoes("facebook_ads")
        dados = await self._get_data(integration_id, [defs["fb_ads:insights_by_campaign"]], inicio, fim)
        linhas = []
        for bloco in dados.values():
            cols = bloco.get("columns_order") or []
            for row in bloco.get("values", []) or []:
                r = dict(zip(cols, row))
                if not r.get("campaign"):
                    continue
                linhas.append({
                    "campanha": r["campaign"], "resultados": _num(r.get("results")),
                    "tipo": r["results"].get("title") if isinstance(r.get("results"), dict) else None,
                    "investimento": _num(r.get("spend")), "alcance": int(_num(r.get("reach"))),
                    "impressoes": int(_num(r.get("impressions"))),
                })
        return linhas

    async def meta_campanhas_por_plataforma(self, integration_id: int, inicio: date, fim: date) -> dict[str, dict[str, dict[str, float]]]:
        """campanha -> plataforma (facebook/instagram/outros) -> {investimento, resultados}.

        A tabela de campanhas aceita a dimensao publisher_platform quando pedida na definicao
        - e o que permite separar Facebook x Instagram por campanha.
        """
        defs = await self.definicoes("facebook_ads")
        d = copy.deepcopy(defs["fb_ads:insights_by_campaign"])
        d["dimensions"] = [{"field": "campaign"}, {"field": "publisher_platform"}]
        d["metrics"] = ["spend", "results"]
        dados = await self._get_data(integration_id, [d], inicio, fim)
        saida: dict[str, dict[str, dict[str, float]]] = {}
        for bloco in dados.values():
            cols = bloco.get("columns_order") or []
            for row in bloco.get("values", []) or []:
                r = dict(zip(cols, row))
                campanha = r.get("campaign")
                if not campanha:
                    continue
                plat = str(r.get("publisher_platform") or "").lower()
                chave = plat if plat in ("facebook", "instagram") else "outros"
                alvo = saida.setdefault(campanha, {}).setdefault(chave, {"investimento": 0.0, "resultados": 0.0})
                alvo["investimento"] += _num(r.get("spend"))
                alvo["resultados"] += _num(r.get("results"))
        return saida

    async def meta_totais(self, integration_id: int, inicio: date, fim: date) -> dict[str, float]:
        defs = await self.definicoes("facebook_ads")
        pedidos = [defs[k] for k in META_TOTAIS]
        dados = await self._get_data(integration_id, pedidos, inicio, fim)
        por_id = {defs[k]["id"]: k for k in META_TOTAIS}
        saida = {k: 0.0 for k in META_TOTAIS}
        for mid, bloco in dados.items():
            chave = por_id.get(mid)
            if chave and isinstance(bloco, dict) and "values" in bloco:
                saida[chave] = _num(bloco["values"])
        return saida

    # ---------------------------------------------------------------- Google Ads
    async def google(self, integration_id: int, inicio: date, fim: date) -> dict[str, Any]:
        defs = await self.definicoes("google_adwords")
        pedidos = [defs[k] for k in GOOGLE_METRICAS]
        dados = await self._get_data(integration_id, pedidos, inicio, fim)
        por_id = {defs[k]["id"]: k for k in GOOGLE_METRICAS}
        campanhas: list[dict[str, Any]] = []
        acoes: dict[str, float] = {}
        custo = conversoes = 0.0
        for mid, bloco in dados.items():
            chave = por_id.get(mid)
            if not isinstance(bloco, dict):
                continue
            if chave == "gads:campaign_performance_by_id":
                cols = bloco.get("columns_order") or []
                for row in bloco.get("values", []) or []:
                    r = dict(zip(cols, row))
                    campanhas.append({
                        "campanha": r.get("campaign"), "campanha_id": str(r.get("campaign.id") or ""),
                        "tipo": r.get("campaign.advertising_channel_type"),
                        "investimento": _num(r.get("metrics.cost_micros")),
                        "resultados": _num(r.get("metrics.conversions")),
                        "cliques": int(_num(r.get("metrics.clicks"))),
                        "impressoes": int(_num(r.get("metrics.impressions"))),
                    })
            elif chave == "gads:custom_conversion_actions":
                for row in bloco.get("values", []) or []:
                    acoes[str(row[0])] = _num(row[1])
            elif chave == "gads:cost_micros":
                custo = _num(bloco.get("values"))
            elif chave == "gads:conversions":
                conversoes = _num(bloco.get("values"))
        return {"campanhas": [c for c in campanhas if c["campanha"]], "acoes": acoes, "custo": custo, "conversoes": conversoes}
