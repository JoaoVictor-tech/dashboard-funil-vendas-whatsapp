"""Consultas do dashboard. Todas recebem o mesmo `Filtros` e aplicam o mesmo recorte
(unidades, datas no fuso de Sao Paulo, origem, campanha, vendedor) - KPI, grafico e tabela
de uma pagina nunca discordam.

Fontes:
- mf_conversas / mf_midia_diaria / mf_midia_diaria_total / mf_anuncios  (coletadas)
- vendas_dms  (vendas do DMS cruzadas com o contato da Helena; so leitura)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app import taxonomia as tx

FUSO = "America/Sao_Paulo"

# Valores aceitos no filtro "Origem" e como cada um se traduz nas tres fontes.
ORIGENS = [tx.MIDIA_FACEBOOK, tx.MIDIA_INSTAGRAM, tx.MIDIA_GOOGLE, tx.MIDIA_META_SEM_ID, tx.MIDIA_SEM_UTM]
_ORIGEM_VENDA = {  # rotulo da origem -> rotulo em VENDA_MIDIA
    tx.MIDIA_FACEBOOK: "Facebook", tx.MIDIA_INSTAGRAM: "Instagram", tx.MIDIA_GOOGLE: "Google",
    tx.MIDIA_META_SEM_ID: tx.MIDIA_META_SEM_ID, tx.MIDIA_SEM_UTM: tx.ORIGEM_SEM_CAMPANHA,
}


@dataclass
class Filtros:
    de: date
    ate: date
    lojas: list[str] = field(default_factory=lambda: ["centro"])
    vendedor: str | None = None   # nome, como em taxonomia.VENDEDORES
    origem: str | None = None     # um de ORIGENS
    campanha: str | None = None   # nome da campanha Meta
    datas_explicitas: bool = False  # True so quando De/Ate vieram do calendario (senao: hoje - 1 mes -> hoje)

    @property
    def vendedor_dms(self) -> str | None:
        return tx.VENDEDORES.get(self.vendedor) if self.vendedor else None

    @property
    def plataformas(self) -> list[str] | None:
        """Plataformas de midia que o filtro de origem deixa passar (None = todas)."""
        if self.origem in (tx.MIDIA_FACEBOOK, tx.MIDIA_INSTAGRAM, tx.MIDIA_META_SEM_ID):
            return ["meta"]
        if self.origem == tx.MIDIA_GOOGLE:
            return ["google"]
        if self.origem == tx.MIDIA_SEM_UTM:
            return []
        return None


def _p(f: Filtros) -> dict[str, Any]:
    return {
        "de": f.de, "ate": f.ate, "lojas": f.lojas, "vend": f.vendedor, "vend_dms": f.vendedor_dms,
        "origem": f.origem, "origem_venda": _ORIGEM_VENDA.get(f.origem) if f.origem else None,
        "campanha": f.campanha, "plataformas": f.plataformas if f.plataformas is not None else ["meta", "google"],
        "nao_ident": tx.CAMPANHA_NAO_IDENTIFICADA, "sem_utm": tx.MIDIA_SEM_UTM,
    }


# Trechos repetidos ------------------------------------------------------------------
CONV_BASE = f"""
    FROM mf_conversas c
    WHERE c.origem = 'Contato'
      AND c.loja = ANY(:lojas)
      AND (c.criado_em AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
      AND (CAST(:vend AS text) IS NULL OR c.vendedor = CAST(:vend AS text))
      AND (CAST(:origem AS text) IS NULL OR c.midia = CAST(:origem AS text))
      AND (CAST(:campanha AS text) IS NULL OR c.campanha = CAST(:campanha AS text))
"""
# Origem de uma venda, no mesmo vocabulario das conversas.
VENDA_MIDIA = """
    CASE
      WHEN v.veio_campanha AND upper(v.utm_source) = 'FACEBOOK'  THEN 'Facebook'
      WHEN v.veio_campanha AND upper(v.utm_source) = 'INSTAGRAM' THEN 'Instagram'
      WHEN v.veio_campanha AND upper(v.utm_source) = 'GOOGLE'    THEN 'Google'
      WHEN v.veio_campanha AND v.utm_source IS NOT NULL          THEN 'Meta — anúncio sem identificação'
      WHEN v.veio_campanha                                        THEN 'Disparo WhatsApp (base própria)'
      ELSE 'Sem origem de campanha'
    END
"""
VENDA_BASE = f"""
    FROM vendas_dms v
    LEFT JOIN mf_anuncios a ON a.anuncio_id = v.utm_campaign
    WHERE v.loja = ANY(:lojas)
      AND (v.data_venda AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
      AND (CAST(:vend_dms AS text) IS NULL OR v.vendedor = CAST(:vend_dms AS text))
      AND (CAST(:origem_venda AS text) IS NULL OR {VENDA_MIDIA} = CAST(:origem_venda AS text))
      AND (CAST(:campanha AS text) IS NULL OR a.campanha = CAST(:campanha AS text))
"""
VENDA_PAGA = "(v.veio_campanha AND upper(v.utm_source) IN ('FACEBOOK','INSTAGRAM','GOOGLE'))"
MIDIA_BASE = """
    FROM mf_midia_diaria m
    WHERE m.loja = ANY(:lojas) AND m.dia BETWEEN :de AND :ate
      AND m.plataforma = ANY(:plataformas)
      AND (CAST(:campanha AS text) IS NULL OR m.campanha = CAST(:campanha AS text))
"""


async def _rows(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in await conn.execute(text(sql), params)]


async def _one(conn: AsyncConnection, sql: str, params: dict[str, Any]) -> dict[str, Any]:
    r = (await conn.execute(text(sql), params)).first()
    return dict(r._mapping) if r else {}


# ===================================================================== Tela 1
async def midia_totais(conn: AsyncConnection, f: Filtros) -> dict[str, float]:
    """Totais das plataformas. Com filtro de origem, so a parcela daquela midia; com filtro
    de campanha, so a campanha (a Meta nao separa Facebook x Instagram por campanha)."""
    if f.campanha:
        col_inv, col_res = _colunas_investimento(f)
        r = await _one(conn, f"""
            SELECT COALESCE(sum({col_inv}) FILTER (WHERE m.plataforma='meta'),0) AS invest_meta,
                   COALESCE(sum(m.invest_facebook),0) AS invest_facebook, COALESCE(sum(m.invest_instagram),0) AS invest_instagram,
                   COALESCE(sum({col_res}) FILTER (WHERE m.plataforma='meta'),0) AS conversas_meta,
                   COALESCE(sum(m.resultados_facebook),0) AS conversas_facebook, COALESCE(sum(m.resultados_instagram),0) AS conversas_instagram,
                   COALESCE(sum(m.investimento) FILTER (WHERE m.plataforma='google'),0) AS invest_google,
                   0 AS leads_google, COALESCE(sum(m.resultados) FILTER (WHERE m.plataforma='google'),0) AS conversoes_google,
                   count(DISTINCT m.dia) AS dias
            {MIDIA_BASE}
        """, _p(f))
        return {k: float(v or 0) for k, v in r.items()}
    r = await _one(conn, """
        SELECT COALESCE(sum(invest_meta),0) AS invest_meta, COALESCE(sum(invest_facebook),0) AS invest_facebook,
               COALESCE(sum(invest_instagram),0) AS invest_instagram, COALESCE(sum(conversas_meta),0) AS conversas_meta,
               COALESCE(sum(conversas_facebook),0) AS conversas_facebook, COALESCE(sum(conversas_instagram),0) AS conversas_instagram,
               COALESCE(sum(invest_google),0) AS invest_google, COALESCE(sum(leads_google),0) AS leads_google,
               COALESCE(sum(conversoes_google),0) AS conversoes_google, count(DISTINCT dia) AS dias
        FROM mf_midia_diaria_total WHERE loja = ANY(:lojas) AND dia BETWEEN :de AND :ate
    """, _p(f))
    t = {k: float(v or 0) for k, v in r.items()}
    if f.origem == tx.MIDIA_FACEBOOK:
        t.update(invest_meta=t["invest_facebook"], invest_instagram=0.0, conversas_meta=t["conversas_facebook"],
                 conversas_instagram=0.0, invest_google=0.0, leads_google=0.0, conversoes_google=0.0)
    elif f.origem == tx.MIDIA_INSTAGRAM:
        t.update(invest_meta=t["invest_instagram"], invest_facebook=0.0, conversas_meta=t["conversas_instagram"],
                 conversas_facebook=0.0, invest_google=0.0, leads_google=0.0, conversoes_google=0.0)
    elif f.origem == tx.MIDIA_GOOGLE:
        t.update(invest_meta=0.0, invest_facebook=0.0, invest_instagram=0.0, conversas_meta=0.0,
                 conversas_facebook=0.0, conversas_instagram=0.0)
    elif f.origem in (tx.MIDIA_SEM_UTM, tx.MIDIA_META_SEM_ID):
        t.update({k: 0.0 for k in t if k != "dias"})
    return t


async def conversas_totais(conn: AsyncConnection, f: Filtros) -> dict[str, int]:
    r = await _one(conn, f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE c.midia IN ('Facebook','Instagram','Meta — anúncio sem identificação')) AS meta,
               count(*) FILTER (WHERE c.primeira_resposta_em IS NOT NULL) AS atendidas
        {CONV_BASE}
    """, _p(f))
    return {k: int(v or 0) for k, v in r.items()}


async def vendas_totais(conn: AsyncConnection, f: Filtros) -> dict[str, float]:
    r = await _one(conn, f"""
        SELECT count(*) AS total, COALESCE(sum(v.valor_vendido),0) AS receita,
               count(*) FILTER (WHERE {VENDA_PAGA}) AS com_anuncio,
               COALESCE(sum(v.valor_vendido) FILTER (WHERE {VENDA_PAGA}),0) AS receita_anuncio,
               count(*) FILTER (WHERE v.veio_campanha AND NOT COALESCE({VENDA_PAGA}, false)) AS disparo,
               COALESCE(sum(v.valor_vendido) FILTER (WHERE v.veio_campanha AND NOT COALESCE({VENDA_PAGA}, false)),0) AS receita_disparo
        {VENDA_BASE}
    """, _p(f))
    return {k: float(v or 0) for k, v in r.items()}


async def origem_por_midia(conn: AsyncConnection, f: Filtros) -> list[dict[str, Any]]:
    rows = await _rows(conn, f"SELECT c.midia AS rotulo, count(*) AS total {CONV_BASE} GROUP BY 1", _p(f))
    rows.sort(key=lambda r: tx.MIDIA_ORDEM.index(r["rotulo"]) if r["rotulo"] in tx.MIDIA_ORDEM else 99)
    for r in rows:
        r["cor"] = tx.MIDIA_COR.get(r["rotulo"], "#94a3b8")
    return rows


async def origem_por_campanha(conn: AsyncConnection, f: Filtros, limite: int = 999) -> list[dict[str, Any]]:
    rows = await _rows(conn, f"""
        SELECT COALESCE(c.campanha, :nao_ident) AS campanha, count(*) AS total,
               count(*) FILTER (WHERE c.midia = 'Facebook') AS facebook,
               count(*) FILTER (WHERE c.midia = 'Instagram') AS instagram
        {CONV_BASE}
          AND c.midia IN ('Facebook','Instagram','Meta — anúncio sem identificação')
        GROUP BY 1 ORDER BY 2 DESC
    """, _p(f))
    if len(rows) > limite + 1:
        cauda = rows[limite:]
        rows = rows[:limite] + [{
            "campanha": f"Outras {len(cauda)} campanhas", "total": sum(r["total"] for r in cauda),
            "facebook": sum(r["facebook"] for r in cauda), "instagram": sum(r["instagram"] for r in cauda),
            "agrupado": True,
        }]
    return rows


def _colunas_investimento(f: Filtros) -> tuple[str, str]:
    """Que coluna de gasto/resultado usar por campanha, conforme o filtro de origem."""
    if f.origem == tx.MIDIA_FACEBOOK:
        return "CASE WHEN m.plataforma = 'meta' THEN m.invest_facebook ELSE m.investimento END", \
               "CASE WHEN m.plataforma = 'meta' THEN m.resultados_facebook ELSE m.resultados END"
    if f.origem == tx.MIDIA_INSTAGRAM:
        return "CASE WHEN m.plataforma = 'meta' THEN m.invest_instagram ELSE m.investimento END", \
               "CASE WHEN m.plataforma = 'meta' THEN m.resultados_instagram ELSE m.resultados END"
    return "m.investimento", "m.resultados"


async def investimento_por_campanha(conn: AsyncConnection, f: Filtros) -> list[dict[str, Any]]:
    col_inv, col_res = _colunas_investimento(f)
    return await _rows(conn, f"""
        SELECT m.plataforma, m.campanha, max(m.tipo) AS tipo, sum({col_inv}) AS investimento,
               sum({col_res}) AS resultados, COALESCE(sum(m.cliques),0) AS cliques,
               sum(m.invest_facebook) AS invest_facebook, sum(m.invest_instagram) AS invest_instagram,
               sum(m.invest_outros) AS invest_outros,
               string_agg(DISTINCT m.loja, ', ') AS lojas
        {MIDIA_BASE}
        GROUP BY 1, 2 HAVING sum({col_inv}) >= 1 ORDER BY 4 DESC
    """, _p(f))


async def conversas_por_campanha(conn: AsyncConnection, f: Filtros) -> dict[str, dict[str, int]]:
    """campanha -> {total, facebook, instagram, comprou, perdido, encerrada, andamento}.

    "Comprou" = o contato da conversa aparece como comprador no DMS dentro do periodo
    (independente do vendedor da venda). Os demais status vem da propria conversa.
    """
    rows = await _rows(conn, f"""
        WITH compradores AS (
            SELECT DISTINCT v.helena_contact_id AS contato_id
            FROM vendas_dms v
            WHERE v.loja = ANY(:lojas) AND v.helena_contact_id IS NOT NULL
              AND (v.data_venda AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
        ),
        base AS (
            SELECT c.*, (b.contato_id IS NOT NULL) AS comprou
            {CONV_BASE.replace('FROM mf_conversas c', 'FROM mf_conversas c LEFT JOIN compradores b ON b.contato_id = c.contato_id')}
        )
        SELECT COALESCE(campanha,
                        CASE WHEN midia IN ('Facebook','Instagram','Meta — anúncio sem identificação') THEN :nao_ident
                             WHEN midia = 'Google' THEN 'Google Ads'
                             ELSE midia END) AS campanha,
               count(*) AS total,
               count(*) FILTER (WHERE midia = 'Facebook') AS facebook,
               count(*) FILTER (WHERE midia = 'Instagram') AS instagram,
               count(*) FILTER (WHERE comprou) AS comprou,
               count(*) FILTER (WHERE NOT comprou AND classificacao = 'LOST') AS perdido,
               count(*) FILTER (WHERE NOT comprou AND classificacao IS DISTINCT FROM 'LOST' AND status = 'COMPLETED') AS encerrada,
               count(*) FILTER (WHERE NOT comprou AND classificacao IS DISTINCT FROM 'LOST' AND status IS DISTINCT FROM 'COMPLETED') AS andamento
        FROM base GROUP BY 1 ORDER BY 2 DESC
    """, _p(f))
    return {r["campanha"]: {k: int(r[k]) for k in ("total", "facebook", "instagram", "comprou", "perdido", "encerrada", "andamento")} for r in rows}


async def vendas_por_origem(conn: AsyncConnection, f: Filtros) -> list[dict[str, Any]]:
    rows = await _rows(conn, f"""
        SELECT {VENDA_MIDIA} AS rotulo, count(*) AS total, COALESCE(sum(v.valor_vendido),0) AS receita
        {VENDA_BASE} GROUP BY 1
    """, _p(f))
    rows.sort(key=lambda r: tx.MIDIA_ORDEM.index(r["rotulo"]) if r["rotulo"] in tx.MIDIA_ORDEM else 99)
    for r in rows:
        r["cor"] = tx.MIDIA_COR.get(r["rotulo"], "#94a3b8")
        r["receita"] = float(r["receita"])
    return rows


async def vendas_por_campanha(conn: AsyncConnection, f: Filtros) -> dict[str, dict[str, Any]]:
    """Vendas originadas em anuncio pago, agrupadas pela campanha do anuncio (mf_anuncios)."""
    rows = await _rows(conn, f"""
        SELECT CASE WHEN upper(v.utm_source) = 'GOOGLE' THEN 'Google Ads' ELSE COALESCE(a.campanha, :nao_ident) END AS campanha,
               count(*) AS vendas,
               COALESCE(sum(v.valor_vendido),0) AS receita,
               count(*) FILTER (WHERE upper(v.utm_source) = 'FACEBOOK') AS facebook,
               count(*) FILTER (WHERE upper(v.utm_source) = 'INSTAGRAM') AS instagram
        {VENDA_BASE}
          AND {VENDA_PAGA}
        GROUP BY 1
    """, _p(f))
    return {r["campanha"]: {"vendas": int(r["vendas"]), "receita": float(r["receita"]),
                            "facebook": int(r["facebook"]), "instagram": int(r["instagram"])} for r in rows}


def _origem_meta(c: dict[str, int], s: dict[str, Any]) -> str:
    """Rotulo da coluna "Origem": de onde vieram as conversas (e vendas) da campanha Meta."""
    fb, ig = c.get("facebook", 0), c.get("instagram", 0)
    if not (fb or ig):
        return "Meta (FB + IG)"
    partes = []
    if ig:
        partes.append(f"IG {round(100 * ig / (fb + ig))}%")
    if fb:
        partes.append(f"FB {round(100 * fb / (fb + ig))}%")
    return " · ".join(partes)


async def tabela_campanhas(conn: AsyncConnection, f: Filtros) -> dict[str, Any]:
    """Uma unica passada que alimenta investimento, CPL (plataforma e Helena), CAC e ROI."""
    inv = await investimento_por_campanha(conn, f)
    conv = await conversas_por_campanha(conn, f)
    vendas = await vendas_por_campanha(conn, f)
    totais = await midia_totais(conn, f)
    ctot = await conversas_totais(conn, f)

    linhas = []
    for r in inv:
        campanha = r["campanha"]
        meta = r["plataforma"] == "meta"
        c = conv.get(campanha, {}) if meta else {}
        s = vendas.get(campanha, {}) if meta else {}
        investimento = float(r["investimento"])
        resultados = float(r["resultados"] or 0)
        engajamento = meta and (r["tipo"] or "").lower().startswith("post engagement")
        linhas.append({
            "plataforma": "Meta" if meta else "Google", "campanha": campanha, "tipo": r["tipo"], "lojas": r["lojas"],
            "origem": _origem_meta(c, s) if meta else "Google Ads",
            "investimento": investimento,
            "invest_facebook": float(r["invest_facebook"] or 0) if meta else 0.0,
            "invest_instagram": float(r["invest_instagram"] or 0) if meta else 0.0,
            "invest_outros": float(r["invest_outros"] or 0) if meta else 0.0,
            "leads_plataforma": None if engajamento else resultados,
            "cpl_plataforma": (investimento / resultados) if resultados and not engajamento else None,
            "conversas": c.get("total", 0) if meta else None,
            "cpl_conversas": (investimento / c["total"]) if meta and c.get("total") else None,
            "vendas": s.get("vendas", 0) if meta else None,
            "receita": s.get("receita", 0.0) if meta else None,
            "cac": (investimento / s["vendas"]) if meta and s.get("vendas") else None,
            "retorno": (s["receita"] / investimento) if meta and s.get("receita") and investimento else None,
        })

    no_periodo = {l["campanha"] for l in linhas if l["plataforma"] == "Meta"}
    google_vendas = vendas.get("Google Ads", {"vendas": 0, "receita": 0.0})
    nao_ident_vendas = vendas.get(tx.CAMPANHA_NAO_IDENTIFICADA, {"vendas": 0, "receita": 0.0})
    fora = {k: v for k, v in vendas.items() if k not in no_periodo and k not in ("Google Ads", tx.CAMPANHA_NAO_IDENTIFICADA)}
    # chaves que sao campanhas Meta de verdade (nao os rotulos "Sem UTM", "Google Ads", "Outros")
    rotulos_nao_campanha = {tx.CAMPANHA_NAO_IDENTIFICADA, tx.MIDIA_SEM_UTM, "Google Ads", tx.MIDIA_OUTROS}
    conv_fora = sum(v["total"] for k, v in conv.items() if k not in no_periodo and k not in rotulos_nao_campanha)
    conv_nao_ident = conv.get(tx.CAMPANHA_NAO_IDENTIFICADA, {}).get("total", 0)

    invest_meta = totais["invest_meta"]
    vendas_meta_periodo = sum(l["vendas"] or 0 for l in linhas if l["plataforma"] == "Meta")
    receita_meta_periodo = sum(l["receita"] or 0 for l in linhas if l["plataforma"] == "Meta")
    return {
        "linhas": linhas,
        "fora_do_periodo": {
            "campanhas": list(fora),
            "vendas": sum(v["vendas"] for v in fora.values()),
            "receita": sum(v["receita"] for v in fora.values()),
            "conversas": conv_fora, "conversas_nao_identificadas": conv_nao_ident,
        },
        # vendas de anuncio Meta cujo ID nao esta no mapa anuncio -> campanha (ex.: unidade sem Meta no Reportei)
        "nao_identificadas": nao_ident_vendas,
        # a soma das campanhas difere do total da conta: campanhas otimizadas para engajamento
        # nao reportam "conversas" na tabela por campanha, e gasto residual (< R$ 1) fica de fora
        "meta_soma_campanhas": sum(l["leads_plataforma"] or 0 for l in linhas if l["plataforma"] == "Meta"),
        "meta": {
            "investimento": invest_meta, "leads_plataforma": totais["conversas_meta"],
            "cpl_plataforma": (invest_meta / totais["conversas_meta"]) if totais["conversas_meta"] else None,
            "conversas": ctot["meta"], "cpl_conversas": (invest_meta / ctot["meta"]) if ctot["meta"] else None,
            "vendas": vendas_meta_periodo, "receita": receita_meta_periodo,
            "cac": (invest_meta / vendas_meta_periodo) if vendas_meta_periodo else None,
            "retorno": (receita_meta_periodo / invest_meta) if invest_meta and receita_meta_periodo else None,
        },
        "google": {
            "investimento": totais["invest_google"], "leads_plataforma": totais["leads_google"],
            "cpl_plataforma": (totais["invest_google"] / totais["leads_google"]) if totais["leads_google"] else None,
            "conversoes": totais["conversoes_google"],
            "vendas": google_vendas["vendas"], "receita": google_vendas["receita"],
            "cac": (totais["invest_google"] / google_vendas["vendas"]) if google_vendas["vendas"] else None,
            "retorno": (google_vendas["receita"] / totais["invest_google"]) if totais["invest_google"] and google_vendas["receita"] else None,
        },
    }


async def serie_diaria(conn: AsyncConnection, f: Filtros) -> dict[str, list[Any]]:
    """Leads das plataformas e conversas por dia, para o grafico de linha."""
    if f.campanha:
        midia = await _rows(conn, f"""
            SELECT m.dia, sum(m.investimento) AS investimento, sum(m.resultados) AS leads_plataforma
            {MIDIA_BASE} GROUP BY 1 ORDER BY 1
        """, _p(f))
    else:
        col_leads = {tx.MIDIA_FACEBOOK: "conversas_facebook", tx.MIDIA_INSTAGRAM: "conversas_instagram",
                     tx.MIDIA_GOOGLE: "leads_google"}.get(f.origem, "conversas_meta + leads_google")
        if f.origem in (tx.MIDIA_SEM_UTM, tx.MIDIA_META_SEM_ID):
            col_leads = "0"
        midia = await _rows(conn, f"""
            SELECT dia, sum(invest_meta + invest_google) AS investimento, sum({col_leads}) AS leads_plataforma
            FROM mf_midia_diaria_total WHERE loja = ANY(:lojas) AND dia BETWEEN :de AND :ate GROUP BY 1 ORDER BY 1
        """, _p(f))
    conv = await _rows(conn, f"""
        SELECT (c.criado_em AT TIME ZONE '{FUSO}')::date AS dia, count(*) AS conversas {CONV_BASE} GROUP BY 1
    """, _p(f))
    por_dia = {r["dia"]: r for r in midia}
    conv_dia = {r["dia"]: int(r["conversas"]) for r in conv}
    dias = sorted(set(por_dia) | set(conv_dia))
    return {
        "dias": [d.strftime("%d/%m") for d in dias],
        "leads_plataforma": [round(float(por_dia.get(d, {}).get("leads_plataforma") or 0)) for d in dias],
        "conversas": [conv_dia.get(d, 0) for d in dias],
    }


# ===================================================================== Tela 2
DISPAROS_BASE = f"""
    FROM mf_disparos d
    WHERE (d.enviado_em AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
"""
# Venda atribuida ao ULTIMO disparo (de qualquer loja) que o telefone/contato recebeu antes da
# compra; cada venda conta uma vez. Deterministico: telefone igual ou mesmo contato, venda >= envio.
ATRIB_VENDAS = f"""
    atrib AS (
        SELECT DISTINCT ON (v.venda_row_id) v.venda_row_id, d.mensagem_id, v.valor_vendido
        FROM mf_disparos d
        JOIN vendas_dms v
          ON (v.telefone_normalizado = d.telefone OR (d.contato_id IS NOT NULL AND v.helena_contact_id = d.contato_id))
         AND v.data_venda >= d.enviado_em
         AND (v.data_venda AT TIME ZONE '{FUSO}')::date <= :ate
        WHERE (d.enviado_em AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
        ORDER BY v.venda_row_id, d.enviado_em DESC
    )
"""


async def disparos_resumo(conn: AsyncConnection, f: Filtros) -> dict[str, Any]:
    """Disparos de WhatsApp (mensagens de campanha) no periodo: enviados, respondidos, vendas
    e situacao das conversas de quem respondeu. Disparo nao tem origem de midia nem campanha
    de anuncio, entao com esses filtros o bloco fica vazio."""
    vazio = {"enviados": 0, "respondidos": 0, "comprou": 0, "perdido": 0, "encerrada": 0, "andamento": 0, "vendas": 0, "receita": 0.0}
    if f.origem or f.campanha:
        return vazio
    r = await _one(conn, f"""
        WITH {ATRIB_VENDAS},
        compradores AS (
            SELECT DISTINCT v.helena_contact_id AS contato_id
            FROM vendas_dms v
            WHERE v.helena_contact_id IS NOT NULL
              AND (v.data_venda AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
        ),
        base AS (
            SELECT d.*, c.status, c.classificacao, (b.contato_id IS NOT NULL) AS comprou
            FROM mf_disparos d
            LEFT JOIN mf_conversas c ON c.session_id = d.session_id
            LEFT JOIN compradores b ON b.contato_id = d.contato_id
            WHERE (d.enviado_em AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
              AND d.loja = ANY(:lojas)
              AND (CAST(:vend AS text) IS NULL OR c.vendedor = CAST(:vend AS text))
        )
        SELECT count(*) AS enviados,
               count(*) FILTER (WHERE respondeu_em IS NOT NULL) AS respondidos,
               (SELECT count(*) FROM atrib a JOIN base x ON x.mensagem_id = a.mensagem_id) AS vendas,
               (SELECT COALESCE(sum(a.valor_vendido), 0) FROM atrib a JOIN base x ON x.mensagem_id = a.mensagem_id) AS receita,
               count(*) FILTER (WHERE respondeu_em IS NOT NULL AND comprou) AS comprou,
               count(*) FILTER (WHERE respondeu_em IS NOT NULL AND NOT comprou AND classificacao = 'LOST') AS perdido,
               count(*) FILTER (WHERE respondeu_em IS NOT NULL AND NOT comprou AND classificacao IS DISTINCT FROM 'LOST' AND status = 'COMPLETED') AS encerrada,
               count(*) FILTER (WHERE respondeu_em IS NOT NULL AND NOT comprou AND classificacao IS DISTINCT FROM 'LOST' AND status IS DISTINCT FROM 'COMPLETED') AS andamento
        FROM base
    """, _p(f))
    return {k: (float(v or 0) if k == "receita" else int(v or 0)) for k, v in r.items()}


async def campanhas_disparo(conn: AsyncConnection, f: Filtros) -> dict[str, Any]:
    """Todos os disparos do periodo, um por modelo de mensagem e dia de envio (o mais perto
    da "campanha" que a API deixa ver), com enviados, respostas, vendas e receita."""
    vazio = {"linhas": [], "total_linhas": 0, "pendentes": 0, "enviados": 0, "responderam": 0, "vendas": 0, "receita": 0.0}
    if f.origem or f.campanha:
        return vazio
    rows = await _rows(conn, f"""
        WITH {ATRIB_VENDAS},
        vendas AS (SELECT mensagem_id, count(*) AS n, sum(valor_vendido) AS receita FROM atrib GROUP BY 1)
        SELECT COALESCE(t.nome, CASE WHEN d.template_id IS NULL THEN '(sem modelo)' ELSE '(modelo ' || left(d.template_id, 8) || ')' END) AS nome,
               (d.enviado_em AT TIME ZONE '{FUSO}')::date AS dia, string_agg(DISTINCT d.loja, ', ') AS lojas,
               count(*) AS enviados,
               count(*) FILTER (WHERE d.respondeu_em IS NOT NULL) AS responderam,
               COALESCE(sum(v.n), 0) AS vendas, COALESCE(sum(v.receita), 0) AS receita
        FROM mf_disparos d
        LEFT JOIN mf_templates t ON t.id = d.template_id
        LEFT JOIN vendas v ON v.mensagem_id = d.mensagem_id
        LEFT JOIN mf_conversas c ON c.session_id = d.session_id
        WHERE (d.enviado_em AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate
          AND d.loja = ANY(:lojas)
          AND (CAST(:vend AS text) IS NULL OR c.vendedor = CAST(:vend AS text))
        GROUP BY 1, 2 ORDER BY vendas DESC, dia DESC, enviados DESC
    """, _p(f))
    pend = await _one(conn, """
        SELECT count(*) AS pendentes FROM mf_conversas
        WHERE loja = ANY(:lojas) AND (disparos_verificados_em IS NULL OR atualizado_em_origem > disparos_verificados_em)
    """, _p(f))
    for r in rows:
        r["receita"] = float(r["receita"] or 0)
    return {"linhas": rows, "total_linhas": len(rows), "pendentes": int(pend.get("pendentes") or 0),
            "enviados": sum(int(r["enviados"]) for r in rows), "responderam": sum(int(r["responderam"]) for r in rows),
            "vendas": sum(int(r["vendas"]) for r in rows), "receita": sum(r["receita"] for r in rows)}


async def vendas_digitais(conn: AsyncConnection, f: Filtros) -> dict[str, Any]:
    """Vendas que tiveram anuncio OU disparo antes da compra, sem contar duas vezes a venda
    que teve os dois. E a soma "anuncio + disparo" que o painel mostra."""
    if f.origem or f.campanha:
        vt = await vendas_totais(conn, f)
        return {"total": int(vt["com_anuncio"]), "receita": float(vt["receita_anuncio"]), "anuncio": int(vt["com_anuncio"]), "disparo": 0, "ambos": 0}
    r = await _one(conn, f"""
        WITH {ATRIB_VENDAS},
        -- vendas de disparo: mesmo recorte do quadro de disparos (loja do disparo e vendedor da
        -- conversa), onde quer que o carro tenha sido faturado
        disparo AS (
            SELECT a.venda_row_id FROM atrib a
            JOIN mf_disparos d ON d.mensagem_id = a.mensagem_id
            LEFT JOIN mf_conversas c ON c.session_id = d.session_id
            WHERE d.loja = ANY(:lojas) AND (CAST(:vend AS text) IS NULL OR c.vendedor = CAST(:vend AS text))
        ),
        vendas_unidade AS (
            SELECT v.venda_row_id, v.valor_vendido, {VENDA_PAGA} AS anuncio, (v.venda_row_id IN (SELECT venda_row_id FROM disparo)) AS disparo
            {VENDA_BASE}
        ),
        base AS (
            SELECT * FROM vendas_unidade
            UNION ALL
            -- venda de disparo que nao esta no recorte da unidade (faturada na outra loja, ou por outro vendedor)
            SELECT v.venda_row_id, v.valor_vendido, false AS anuncio, true AS disparo
            FROM vendas_dms v
            WHERE v.venda_row_id IN (SELECT venda_row_id FROM disparo)
              AND v.venda_row_id NOT IN (SELECT venda_row_id FROM vendas_unidade)
        )
        SELECT count(*) FILTER (WHERE anuncio OR disparo) AS total,
               COALESCE(sum(valor_vendido) FILTER (WHERE anuncio OR disparo), 0) AS receita,
               count(*) FILTER (WHERE anuncio) AS anuncio,
               count(*) FILTER (WHERE disparo) AS disparo,
               count(*) FILTER (WHERE anuncio AND disparo) AS ambos
        FROM base
    """, _p(f))
    return {"total": int(r.get("total") or 0), "receita": float(r.get("receita") or 0), "anuncio": int(r.get("anuncio") or 0),
            "disparo": int(r.get("disparo") or 0), "ambos": int(r.get("ambos") or 0)}


async def leads_que_compraram(conn: AsyncConnection, f: Filtros) -> int:
    r = await _one(conn, f"""
        SELECT count(*) AS n {CONV_BASE}
          AND c.contato_id IN (
              SELECT v.helena_contact_id FROM vendas_dms v
              WHERE v.loja = ANY(:lojas) AND v.helena_contact_id IS NOT NULL
                AND (v.data_venda AT TIME ZONE '{FUSO}')::date BETWEEN :de AND :ate)
    """, _p(f))
    return int(r.get("n") or 0)


async def top_vendedores_vendas(conn: AsyncConnection, f: Filtros, limite: int = 5) -> list[dict[str, Any]]:
    rows = await _rows(conn, f"""
        SELECT v.vendedor AS codigo, count(*) AS vendas, COALESCE(sum(v.valor_vendido),0) AS receita
        {VENDA_BASE.replace('CAST(:vend_dms AS text) IS NULL OR v.vendedor = CAST(:vend_dms AS text)', 'TRUE')}
        GROUP BY 1 ORDER BY 2 DESC, 3 DESC LIMIT :limite
    """, {**_p(f), "limite": limite})
    for r in rows:
        r["nome"] = tx.nome_vendedor(r["codigo"])
        r["receita"] = float(r["receita"])
    return rows


async def top_vendedores_atendimento(conn: AsyncConnection, f: Filtros, limite: int = 5) -> list[dict[str, Any]]:
    rows = await _rows(conn, f"""
        SELECT c.vendedor AS nome, count(*) AS conversas,
               count(*) FILTER (WHERE c.primeira_resposta_em IS NOT NULL) AS atendidas
        {CONV_BASE.replace('CAST(:vend AS text) IS NULL OR c.vendedor = CAST(:vend AS text)', 'TRUE')}
          AND c.vendedor = ANY(:vendedores)
        GROUP BY 1 ORDER BY 3 DESC LIMIT :limite
    """, {**_p(f), "vendedores": list(tx.VENDEDORES), "limite": limite})
    return rows


async def campanhas_disponiveis(conn: AsyncConnection, f: Filtros) -> list[str]:
    rows = await _rows(conn, """
        SELECT DISTINCT campanha FROM mf_midia_diaria
        WHERE loja = ANY(:lojas) AND plataforma = 'meta' AND dia BETWEEN :de AND :ate ORDER BY 1
    """, _p(f))
    return [r["campanha"] for r in rows]
