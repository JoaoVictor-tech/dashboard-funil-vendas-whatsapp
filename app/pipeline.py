"""Coletor: Helena (conversas) + Reportei (midia paga, por dia) -> tabelas mf_, por unidade.

Duas fontes, dois ritmos, para cada unidade configurada em `Settings.unidades`:
- Conversas: incremental por `UpdatedAt` da Helena, com marca d'agua por unidade em
  mf_monitor_estado e uma sobreposicao de seguranca. Assim uma conversa antiga que mudou
  de status (foi encerrada, classificada como perdida) e reescrita.
- Midia: um dia por vez. Os ultimos `midia_rebaixar_dias` sao sempre recoletados porque
  a Meta reatribui "conversas iniciadas" por ate 7 dias. Cada dia custa ate 5 chamadas ao
  Reportei (anuncios, campanhas, campanhas por plataforma e totais da Meta; Google),
  pausadas para caber no limite.

Uma unidade sem token da Helena ou sem integracao Meta simplesmente pula aquela parte -
o dashboard mostra o aviso correspondente.

Vendas (DMS) NAO sao copiadas: o dashboard le a tabela vendas_dms direto, ja que esta
no mesmo banco.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from app import repository as repo
from app.clients.helena import HelenaClient
from app.clients.reportei import ReporteiClient
from app.config import TZ, Unidade, get_settings
from app.db import get_engine
from app.taxonomia import acao_google_e_lead, midia_de, primeiro_nome

logger = logging.getLogger(__name__)


def chave_marca(unidade: Unidade) -> str:
    return f"helena_updated_at:{unidade.chave}"


class Coletor:
    def __init__(self) -> None:
        self.s = get_settings()

    async def executar(self, gatilho: str = "agendado", *, dias_midia: int | None = None,
                       conversas_desde: date | None = None, disparos_limite: int = 400) -> dict[str, Any]:
        engine = get_engine()
        async with engine.begin() as conn:
            execucao_id = await repo.abrir_execucao(conn, gatilho)
        resultado: dict[str, Any] = {"status": "ok", "unidades": {}}
        try:
            reportei = ReporteiClient()
            try:
                for unidade in self.s.unidades:
                    r: dict[str, Any] = {}
                    r["midia"] = await self._coletar_midia(reportei, unidade, dias_midia)
                    r["conversas"] = await self._coletar_conversas(unidade, conversas_desde)
                    r["disparos"] = await self._completar_disparos(unidade, disparos_limite)
                    resultado["unidades"][unidade.chave] = r
            finally:
                await reportei.fechar()
            async with engine.begin() as conn:
                resultado["campanhas_resolvidas"] = await repo.resolver_campanhas(conn)
                await repo.fechar_execucao(
                    conn, execucao_id, status="ok",
                    conversas=sum(u["conversas"]["gravadas"] for u in resultado["unidades"].values()),
                    dias_midia=sum(u["midia"]["dias"] for u in resultado["unidades"].values()),
                    anuncios=max(u["midia"]["anuncios"] for u in resultado["unidades"].values()),
                    detalhes=resultado,
                )
        except Exception as exc:
            logger.exception("Coleta falhou")
            async with engine.begin() as conn:
                await repo.fechar_execucao(conn, execucao_id, status="falhou", erro=str(exc)[:900], detalhes=resultado)
            raise
        return resultado

    # ------------------------------------------------------------------ midia
    async def _coletar_midia(self, reportei: ReporteiClient, unidade: Unidade, dias_forcados: int | None) -> dict[str, Any]:
        if not (unidade.tem_meta or unidade.tem_google):
            return {"dias": 0, "anuncios": 0, "pulado": "sem integracao no Reportei"}
        hoje = datetime.now(TZ).date()
        engine = get_engine()
        async with engine.connect() as conn:
            ultimo = await repo.ultimo_dia_midia(conn, unidade.chave)
        if dias_forcados:
            inicio = hoje - timedelta(days=dias_forcados - 1)
        elif ultimo is None:
            inicio = self.s.backfill_inicio
        else:
            inicio = min(ultimo, hoje) - timedelta(days=self.s.midia_rebaixar_dias - 1)
        inicio = max(inicio, self.s.backfill_inicio)

        dias = 0
        dia = inicio
        while dia <= hoje:
            await self._coletar_dia(reportei, unidade, dia)
            dias += 1
            dia += timedelta(days=1)
        async with engine.connect() as conn:
            row = (await conn.execute(text("SELECT count(*) FROM mf_anuncios WHERE loja = :l"), {"l": unidade.chave})).first()
            anuncios = int(row[0]) if row else 0
        logger.info("[%s] Midia: %s dia(s) coletado(s) de %s a %s", unidade.chave, dias, inicio, hoje)
        return {"dias": dias, "de": inicio.isoformat(), "ate": hoje.isoformat(), "anuncios": anuncios}

    async def _coletar_dia(self, reportei: ReporteiClient, unidade: Unidade, dia: date) -> None:
        anuncios: list[dict[str, Any]] = []
        campanhas_meta: list[dict[str, Any]] = []
        totais = {k: 0.0 for k in (
            "fb_ads:spend", "fb_ads:spend_facebook", "fb_ads:spend_instagram",
            "fb_ads:actions_onsite_conversion.messaging_conversation_started_7d",
            "fb_ads:actions_onsite_conversion.messaging_conversation_started_7d_facebook",
            "fb_ads:actions_onsite_conversion.messaging_conversation_started_7d_instagram",
        )}
        google: dict[str, Any] = {"campanhas": [], "acoes": {}, "custo": 0.0, "conversoes": 0.0}
        if unidade.tem_meta:
            anuncios = await reportei.meta_anuncios(unidade.reportei_meta_id, dia, dia)
            campanhas_meta = await reportei.meta_campanhas(unidade.reportei_meta_id, dia, dia)
            por_plataforma = await reportei.meta_campanhas_por_plataforma(unidade.reportei_meta_id, dia, dia)
            for c in campanhas_meta:
                p = por_plataforma.get(c["campanha"], {})
                c["invest_facebook"] = p.get("facebook", {}).get("investimento", 0.0)
                c["invest_instagram"] = p.get("instagram", {}).get("investimento", 0.0)
                c["invest_outros"] = p.get("outros", {}).get("investimento", 0.0)
                c["resultados_facebook"] = p.get("facebook", {}).get("resultados", 0.0)
                c["resultados_instagram"] = p.get("instagram", {}).get("resultados", 0.0)
            totais = await reportei.meta_totais(unidade.reportei_meta_id, dia, dia)
        if unidade.tem_google:
            google = await reportei.google(unidade.reportei_google_id, dia, dia)

        leads_google = sum(v for k, v in google["acoes"].items() if acao_google_e_lead(k))
        total = {
            "invest_meta": totais["fb_ads:spend"],
            "invest_facebook": totais["fb_ads:spend_facebook"],
            "invest_instagram": totais["fb_ads:spend_instagram"],
            "conversas_meta": int(totais["fb_ads:actions_onsite_conversion.messaging_conversation_started_7d"]),
            "conversas_facebook": int(totais["fb_ads:actions_onsite_conversion.messaging_conversation_started_7d_facebook"]),
            "conversas_instagram": int(totais["fb_ads:actions_onsite_conversion.messaging_conversation_started_7d_instagram"]),
            "invest_google": google["custo"],
            "conversoes_google": google["conversoes"],
            "leads_google": leads_google,
            "acoes_google": google["acoes"],
        }
        engine = get_engine()
        async with engine.begin() as conn:
            await repo.substituir_midia_dia(conn, unidade.chave, dia, "meta", campanhas_meta)
            await repo.substituir_midia_dia(conn, unidade.chave, dia, "google", google["campanhas"])
            await repo.upsert_midia_total(conn, unidade.chave, dia, total)
            await repo.upsert_anuncios(conn, unidade.chave, dia, anuncios)

    # ------------------------------------------------------------- conversas
    async def _coletar_conversas(self, unidade: Unidade, desde_forcado: date | None = None) -> dict[str, Any]:
        if not unidade.tem_conversas:
            return {"gravadas": 0, "pulado": "sem token da Helena"}
        engine = get_engine()
        async with engine.connect() as conn:
            marca = await repo.ler_estado(conn, chave_marca(unidade))
        inicio_backfill = datetime.combine(self.s.backfill_inicio, datetime.min.time(), tzinfo=TZ)
        if desde_forcado:
            # recarga completa (ex.: para preencher um campo novo em conversas antigas)
            marca = None
            inicio_backfill = max(inicio_backfill, datetime.combine(desde_forcado, datetime.min.time(), tzinfo=TZ))
        # Sempre por data de ATUALIZACAO: uma conversa antiga que recebeu um disparo ou uma
        # resposta depois do inicio entra tambem (o filtro de periodo do dashboard e pela
        # data de criacao, entao ela nao infla os leads do periodo).
        desde = datetime.fromisoformat(marca) - timedelta(hours=self.s.sobreposicao_horas) if marca else inicio_backfill
        criadas_apos = None

        helena = HelenaClient(unidade.helena_token)
        gravadas = 0
        maior_atualizacao: datetime | None = None
        try:
            async with engine.begin() as conn:
                async for sessao in helena.sessoes(criadas_apos=criadas_apos, atualizadas_apos=desde, ordenar_por="UpdatedAt"):
                    linha = _montar_conversa(sessao, unidade.chave)
                    if linha is None:
                        continue
                    await repo.upsert_conversa(conn, linha)
                    gravadas += 1
                    atual = linha["atualizado_em_origem"] or linha["criado_em"]
                    if maior_atualizacao is None or atual > maior_atualizacao:
                        maior_atualizacao = atual
                if maior_atualizacao is not None:
                    await repo.gravar_estado(conn, chave_marca(unidade), maior_atualizacao.isoformat())
        finally:
            await helena.fechar()
        logger.info("[%s] Conversas: %s gravada(s); marca d'agua %s", unidade.chave, gravadas, maior_atualizacao)
        return {"gravadas": gravadas, "marca": maior_atualizacao.isoformat() if maior_atualizacao else marca}


    # ------------------------------------------------------------- disparos
    async def _completar_disparos(self, unidade: Unidade, limite: int) -> dict[str, Any]:
        """Disparos = mensagens de origem CAMPAIGN em QUALQUER conversa (um disparo para quem
        ja tem conversa aberta nao cria conversa nova). Varre as mensagens das conversas
        novas/alteradas desde a ultima varredura, `limite` conversas por ciclo, e grava uma
        linha por mensagem de campanha em mf_disparos, com a primeira resposta do cliente
        depois do envio."""
        if not unidade.tem_conversas:
            return {"templates": 0, "conversas": 0, "disparos": 0}
        engine = get_engine()
        helena = HelenaClient(unidade.helena_token)
        n_templates = feitos = disparos = 0
        pendentes: list[dict[str, Any]] = []
        try:
            lojas = [u.chave for u in self.s.unidades]
            templates = await helena.templates()
            nomes = {t["id"]: (t.get("name") or "") for t in templates}
            async with engine.begin() as conn:
                n_templates = await repo.upsert_templates(conn, unidade.chave, templates)
            async with engine.connect() as conn:
                pendentes = await repo.conversas_para_varrer(conn, unidade.chave, limite)
            for c in pendentes:
                agora = datetime.now(timezone.utc)
                try:
                    msgs = await helena.mensagens(c["session_id"], desde=c["disparos_verificados_em"])
                except Exception as exc:  # noqa: BLE001 - uma conversa ruim nao derruba o ciclo
                    logger.warning("[%s] varredura %s: %s", unidade.chave, c["session_id"], exc)
                    continue
                campanha = [m for m in msgs if m.get("origin") == "CAMPAIGN"]
                async with engine.begin() as conn:
                    for m in campanha:
                        enviado = _dt(m.get("createdAt"))
                        resposta = next((_dt(x.get("createdAt")) for x in msgs
                                         if x.get("direction") == "FROM_HUB" and _dt(x.get("createdAt")) > enviado), None)
                        await repo.upsert_disparo(conn, {
                            "mensagem_id": m["id"], "loja": _loja_disparo(unidade.chave, c.get("departamento"), nomes.get(m.get("templateId") or "", ""), lojas),
                            "session_id": c["session_id"], "contato_id": c.get("contato_id"), "telefone": c.get("telefone"),
                            "template_id": m.get("templateId"), "enviado_em": enviado, "respondeu_em": resposta,
                        })
                    await repo.marcar_varrida(conn, c["session_id"], agora)
                disparos += len(campanha)
                feitos += 1
                await asyncio.sleep(0.25)   # 1000 req / 5 min na API
        finally:
            await helena.fechar()
        logger.info("[%s] Disparos: %s template(s), %s conversa(s) varrida(s), %s mensagem(ns) de campanha",
                    unidade.chave, n_templates, feitos, disparos)
        return {"templates": n_templates, "conversas": feitos, "disparos": disparos, "pendentes": len(pendentes) - feitos}


def _loja_disparo(loja_conta: str, departamento: str | None, nome_template: str, lojas: list[str]) -> str:
    """Os disparos podem sair da conta de uma unidade so; o departamento ("Disparos Norte") ou
    o nome do modelo de mensagem diz de que loja e o disparo."""
    d = (departamento or "").lower()
    n = nome_template.lower()
    for loja in lojas:
        if loja in d:
            return loja
    if not d.startswith("disparos"):
        for loja in lojas:
            if loja in n:
                return loja
    return loja_conta


def _telefone(contato: dict[str, Any] | None) -> str | None:
    """'+55|11900000000' -> '11900000000' (mesmo formato de telefone_normalizado no DMS)."""
    if not contato:
        return None
    bruto = contato.get("phonenumber") or contato.get("phoneNumber") or ""
    digitos = "".join(ch for ch in bruto.split("|")[-1] if ch.isdigit())
    if bruto.startswith("+55") and "|" not in bruto and digitos.startswith("55"):
        digitos = digitos[2:]
    return digitos or None


def _dt(valor: str | None) -> datetime | None:
    if not valor:
        return None
    d = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _montar_conversa(s: dict[str, Any], loja: str) -> dict[str, Any] | None:
    if s.get("type") == "GROUP":
        return None
    utm = s.get("utm") or {}
    agente = (s.get("agentDetails") or {}).get("name")
    classificacao = (s.get("classification") or {}).get("category")
    return {
        "loja": loja, "session_id": s["id"], "numero": s.get("number"),
        "criado_em": _dt(s.get("createdAt")), "atualizado_em_origem": _dt(s.get("updatedAt")),
        "status": s.get("status"), "origem": s.get("origin"), "canal_id": s.get("channelId"),
        "departamento": (s.get("departmentDetails") or {}).get("name"),
        "user_id": s.get("userId"), "vendedor": primeiro_nome(agente), "contato_id": s.get("contactId"),
        "utm_source": utm.get("source"), "utm_campaign": utm.get("campaign"), "utm_headline": utm.get("headline"),
        "midia": midia_de(utm.get("source")),
        "primeira_resposta_em": _dt(s.get("firstResponseAt")), "ultima_msg_cliente": _dt(s.get("lastMessageIn")),
        "ultima_msg_equipe": _dt(s.get("lastMessageOut")), "encerrada_em": _dt(s.get("endAt")),
        "classificacao": classificacao if classificacao and classificacao != "UNDEFINED" else None,
        "bot_id": s.get("botId"), "telefone": _telefone(s.get("contactDetails")),
    }
