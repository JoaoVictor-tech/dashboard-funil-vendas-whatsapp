"""Escrita nas tabelas mf_ (UPSERTs idempotentes) e estado do coletor."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def ler_estado(conn: AsyncConnection, chave: str) -> str | None:
    row = (await conn.execute(text("SELECT valor FROM mf_monitor_estado WHERE chave = :c"), {"c": chave})).first()
    return row[0] if row else None


async def gravar_estado(conn: AsyncConnection, chave: str, valor: str) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO mf_monitor_estado (chave, valor, atualizado_em) VALUES (:c, :v, now())
            ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now()
            """
        ),
        {"c": chave, "v": valor},
    )


async def abrir_execucao(conn: AsyncConnection, gatilho: str) -> int:
    row = (await conn.execute(
        text("INSERT INTO mf_monitor_execucoes (gatilho) VALUES (:g) RETURNING id"), {"g": gatilho}
    )).first()
    return int(row[0])


async def fechar_execucao(conn: AsyncConnection, execucao_id: int, **campos: Any) -> None:
    campos.setdefault("status", "ok")
    if "detalhes" in campos and not isinstance(campos["detalhes"], str):
        campos["detalhes"] = json.dumps(campos["detalhes"], ensure_ascii=False, default=str)
    sets = ", ".join(f"{k} = :{k}" for k in campos)
    await conn.execute(
        text(f"UPDATE mf_monitor_execucoes SET terminado_em = now(), {sets} WHERE id = :id"),
        {"id": execucao_id, **campos},
    )


# ------------------------------------------------------------------ conversas
async def upsert_conversa(conn: AsyncConnection, c: dict[str, Any]) -> None:
    await conn.execute(
        text(
            """
            INSERT INTO mf_conversas (
                loja, session_id, numero, criado_em, atualizado_em_origem, status, origem, canal_id,
                departamento, user_id, vendedor, contato_id, utm_source, utm_campaign, utm_headline,
                midia, primeira_resposta_em, ultima_msg_cliente, ultima_msg_equipe, encerrada_em,
                classificacao, bot_id, telefone, coletado_em
            ) VALUES (
                :loja, :session_id, :numero, :criado_em, :atualizado_em_origem, :status, :origem, :canal_id,
                :departamento, :user_id, :vendedor, :contato_id, :utm_source, :utm_campaign, :utm_headline,
                :midia, :primeira_resposta_em, :ultima_msg_cliente, :ultima_msg_equipe, :encerrada_em,
                :classificacao, :bot_id, :telefone, now()
            )
            ON CONFLICT (session_id) DO UPDATE SET
                loja = EXCLUDED.loja, numero = EXCLUDED.numero, atualizado_em_origem = EXCLUDED.atualizado_em_origem,
                status = EXCLUDED.status, origem = EXCLUDED.origem, canal_id = EXCLUDED.canal_id,
                departamento = EXCLUDED.departamento, user_id = EXCLUDED.user_id, vendedor = EXCLUDED.vendedor,
                contato_id = EXCLUDED.contato_id, utm_source = EXCLUDED.utm_source,
                utm_campaign = EXCLUDED.utm_campaign, utm_headline = EXCLUDED.utm_headline,
                midia = EXCLUDED.midia, primeira_resposta_em = EXCLUDED.primeira_resposta_em,
                ultima_msg_cliente = EXCLUDED.ultima_msg_cliente, ultima_msg_equipe = EXCLUDED.ultima_msg_equipe,
                encerrada_em = EXCLUDED.encerrada_em, classificacao = EXCLUDED.classificacao,
                bot_id = EXCLUDED.bot_id, telefone = COALESCE(EXCLUDED.telefone, mf_conversas.telefone),
                coletado_em = now()
            """
        ),
        c,
    )


# ------------------------------------------------------------------ disparos
async def conversas_para_varrer(conn: AsyncConnection, loja: str, limite: int) -> list[dict[str, Any]]:
    """Conversas cujas mensagens ainda nao foram varridas (ou mudaram desde a ultima varredura)."""
    rows = await conn.execute(
        text("""
            SELECT session_id, contato_id, telefone, departamento, disparos_verificados_em
            FROM mf_conversas
            WHERE loja = :l
              AND (disparos_verificados_em IS NULL OR atualizado_em_origem > disparos_verificados_em)
            ORDER BY (disparos_verificados_em IS NULL) DESC, criado_em DESC
            LIMIT :n
        """),
        {"l": loja, "n": limite},
    )
    return [dict(r._mapping) for r in rows]


async def upsert_disparo(conn: AsyncConnection, d: dict[str, Any]) -> None:
    await conn.execute(
        text("""
            INSERT INTO mf_disparos (mensagem_id, loja, session_id, contato_id, telefone, template_id, enviado_em, respondeu_em, coletado_em)
            VALUES (:mensagem_id, :loja, :session_id, :contato_id, :telefone, :template_id, :enviado_em, :respondeu_em, now())
            ON CONFLICT (mensagem_id) DO UPDATE SET
                loja = EXCLUDED.loja, telefone = COALESCE(EXCLUDED.telefone, mf_disparos.telefone),
                template_id = COALESCE(EXCLUDED.template_id, mf_disparos.template_id),
                respondeu_em = COALESCE(EXCLUDED.respondeu_em, mf_disparos.respondeu_em), coletado_em = now()
        """),
        d,
    )


async def marcar_varrida(conn: AsyncConnection, session_id: str, quando: datetime) -> None:
    await conn.execute(
        text("UPDATE mf_conversas SET disparos_verificados_em = :q WHERE session_id = :s"),
        {"q": quando, "s": session_id},
    )


async def upsert_templates(conn: AsyncConnection, loja: str, templates: Iterable[dict[str, Any]]) -> int:
    n = 0
    for t in templates:
        if not t.get("id") or not t.get("name"):
            continue
        await conn.execute(
            text("""
                INSERT INTO mf_templates (id, loja, nome, canal_id, tipo, atualizado_em)
                VALUES (:id, :loja, :nome, :canal, :tipo, now())
                ON CONFLICT (id) DO UPDATE SET nome = EXCLUDED.nome, canal_id = EXCLUDED.canal_id,
                    tipo = EXCLUDED.tipo, atualizado_em = now()
            """),
            {"id": t["id"], "loja": loja, "nome": t["name"], "canal": t.get("channelId"), "tipo": t.get("type")},
        )
        n += 1
    return n


async def resolver_campanhas(conn: AsyncConnection) -> int:
    """Liga cada conversa a campanha Meta pelo ID do anuncio (utm.campaign)."""
    r = await conn.execute(
        text(
            """
            UPDATE mf_conversas c SET campanha = a.campanha
            FROM mf_anuncios a
            WHERE c.utm_campaign = a.anuncio_id AND c.campanha IS DISTINCT FROM a.campanha
            """
        )
    )
    return r.rowcount or 0


# ------------------------------------------------------------------ midia
async def substituir_midia_dia(conn: AsyncConnection, loja: str, dia: date, plataforma: str, linhas: Iterable[dict[str, Any]]) -> int:
    """O dia e reescrito por inteiro: campanha que sumiu do Reportei some daqui tambem."""
    await conn.execute(
        text("DELETE FROM mf_midia_diaria WHERE loja = :l AND dia = :d AND plataforma = :p"), {"l": loja, "d": dia, "p": plataforma}
    )
    n = 0
    for l in linhas:
        await conn.execute(
            text(
                """
                INSERT INTO mf_midia_diaria (loja, dia, plataforma, campanha, campanha_id, tipo, investimento,
                    resultados, alcance, impressoes, cliques, invest_facebook, invest_instagram, invest_outros,
                    resultados_facebook, resultados_instagram, coletado_em)
                VALUES (:loja, :dia, :plataforma, :campanha, :campanha_id, :tipo, :investimento, :resultados,
                    :alcance, :impressoes, :cliques, :invest_facebook, :invest_instagram, :invest_outros,
                    :resultados_facebook, :resultados_instagram, now())
                ON CONFLICT (loja, dia, plataforma, campanha) DO UPDATE SET
                    investimento = mf_midia_diaria.investimento + EXCLUDED.investimento,
                    resultados = mf_midia_diaria.resultados + EXCLUDED.resultados,
                    impressoes = COALESCE(mf_midia_diaria.impressoes, 0) + COALESCE(EXCLUDED.impressoes, 0),
                    invest_facebook = mf_midia_diaria.invest_facebook + EXCLUDED.invest_facebook,
                    invest_instagram = mf_midia_diaria.invest_instagram + EXCLUDED.invest_instagram,
                    invest_outros = mf_midia_diaria.invest_outros + EXCLUDED.invest_outros,
                    resultados_facebook = mf_midia_diaria.resultados_facebook + EXCLUDED.resultados_facebook,
                    resultados_instagram = mf_midia_diaria.resultados_instagram + EXCLUDED.resultados_instagram
                """
            ),
            {
                "loja": loja, "dia": dia, "plataforma": plataforma, "campanha": l["campanha"],
                "campanha_id": l.get("campanha_id"), "tipo": l.get("tipo"),
                "investimento": round(l.get("investimento", 0.0), 2), "resultados": l.get("resultados", 0.0),
                "alcance": l.get("alcance"), "impressoes": l.get("impressoes"), "cliques": l.get("cliques"),
                "invest_facebook": round(l.get("invest_facebook", 0.0), 2),
                "invest_instagram": round(l.get("invest_instagram", 0.0), 2),
                "invest_outros": round(l.get("invest_outros", 0.0), 2),
                "resultados_facebook": l.get("resultados_facebook", 0.0),
                "resultados_instagram": l.get("resultados_instagram", 0.0),
            },
        )
        n += 1
    return n


async def upsert_midia_total(conn: AsyncConnection, loja: str, dia: date, t: dict[str, Any]) -> None:
    t = dict(t)
    t["acoes_google"] = json.dumps(t.get("acoes_google") or {}, ensure_ascii=False)
    await conn.execute(
        text(
            """
            INSERT INTO mf_midia_diaria_total (loja, dia, invest_meta, invest_facebook, invest_instagram,
                conversas_meta, conversas_facebook, conversas_instagram, invest_google, conversoes_google,
                leads_google, acoes_google, coletado_em)
            VALUES (:loja, :dia, :invest_meta, :invest_facebook, :invest_instagram, :conversas_meta,
                :conversas_facebook, :conversas_instagram, :invest_google, :conversoes_google,
                :leads_google, CAST(:acoes_google AS jsonb), now())
            ON CONFLICT (loja, dia) DO UPDATE SET
                invest_meta = EXCLUDED.invest_meta, invest_facebook = EXCLUDED.invest_facebook,
                invest_instagram = EXCLUDED.invest_instagram, conversas_meta = EXCLUDED.conversas_meta,
                conversas_facebook = EXCLUDED.conversas_facebook, conversas_instagram = EXCLUDED.conversas_instagram,
                invest_google = EXCLUDED.invest_google, conversoes_google = EXCLUDED.conversoes_google,
                leads_google = EXCLUDED.leads_google, acoes_google = EXCLUDED.acoes_google, coletado_em = now()
            """
        ),
        {"loja": loja, "dia": dia, **t},
    )


async def upsert_anuncios(conn: AsyncConnection, loja: str, dia: date, anuncios: Iterable[dict[str, Any]]) -> int:
    n = 0
    for a in anuncios:
        await conn.execute(
            text(
                """
                INSERT INTO mf_anuncios (loja, anuncio_id, anuncio_nome, campanha, primeiro_dia, ultimo_dia, atualizado_em)
                VALUES (:loja, :id, :nome, :campanha, :dia, :dia, now())
                ON CONFLICT (anuncio_id) DO UPDATE SET
                    loja = EXCLUDED.loja,
                    anuncio_nome = COALESCE(EXCLUDED.anuncio_nome, mf_anuncios.anuncio_nome),
                    campanha = EXCLUDED.campanha,
                    primeiro_dia = LEAST(mf_anuncios.primeiro_dia, EXCLUDED.primeiro_dia),
                    ultimo_dia = GREATEST(mf_anuncios.ultimo_dia, EXCLUDED.ultimo_dia),
                    atualizado_em = now()
                """
            ),
            {"loja": loja, "id": a["anuncio_id"], "nome": a.get("anuncio_nome"), "campanha": a["campanha"], "dia": dia},
        )
        n += 1
    return n


async def ultimo_dia_midia(conn: AsyncConnection, loja: str) -> date | None:
    row = (await conn.execute(text("SELECT max(dia) FROM mf_midia_diaria_total WHERE loja = :l"), {"l": loja})).first()
    return row[0] if row else None


def _dt(valor: str | None) -> datetime | None:
    if not valor:
        return None
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))
