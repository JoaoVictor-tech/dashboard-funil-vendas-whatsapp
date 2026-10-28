"""Rotas do dashboard (HTML + JSON).

Autenticacao: HTTP Basic em TODAS as rotas (o painel expoe vendas e receita).
Filtros: um unico `Filtros` (unidade, de, ate, origem, campanha, vendedor) e montado da
query string e vale para a pagina inteira - KPI, grafico e tabela usam o mesmo recorte;
as abas preservam os filtros. Clicar numa fatia de grafico aplica o filtro correspondente.
"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import taxonomia
from app.config import TZ, get_settings
from app.dashboard import queries as q
from app.db import get_engine

router = APIRouter()
seguranca = HTTPBasic()
templates = Jinja2Templates(directory="app/dashboard/templates")


# ------------------------------------------------------------------ filtros Jinja
def _br(valor: Any, formato: str = "%d/%m/%Y %H:%M") -> str:
    if valor is None:
        return "—"
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            valor = valor.replace(tzinfo=timezone.utc)
        valor = valor.astimezone(TZ)
    return valor.strftime(formato)


def _brl(valor: Any, casas: int = 0) -> str:
    if valor is None:
        return "—"
    s = f"{float(valor):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {s}"


def _num(valor: Any, casas: int = 0) -> str:
    if valor is None:
        return "—"
    return f"{float(valor):,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(parte: Any, todo: Any, casas: int = 1) -> str:
    if not todo:
        return "—"
    return f"{100 * float(parte or 0) / float(todo):.{casas}f}".replace(".", ",") + "%"


def _vezes(valor: Any) -> str:
    return "—" if valor is None else f"{float(valor):.1f}".replace(".", ",") + "×"


def _curta(campanha: str) -> str:
    """Tira o prefixo repetido dos nomes de campanha para caber na tela
    ("AutoVale_Centro_Seminovos_Leads" -> "Centro Seminovos Leads"). A unidade fica no nome:
    com "Todas as unidades", campanhas iguais das duas lojas precisam continuar distintas."""
    prefixo = get_settings().campanha_prefixo
    nome = campanha or ""
    if prefixo and nome.startswith(prefixo):
        nome = nome[len(prefixo):]
    return nome.replace("_", " ")


templates.env.filters.update(br=_br, brl=_brl, num=_num, pct=_pct, vezes=_vezes, curta=_curta)


# ------------------------------------------------------------------ auth
def exigir_login(credenciais: HTTPBasicCredentials = Depends(seguranca)) -> str:
    s = get_settings()
    ok_user = secrets.compare_digest(credenciais.username, s.dashboard_username)
    ok_pass = secrets.compare_digest(credenciais.password, s.dashboard_password)
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas", headers={"WWW-Authenticate": "Basic"})
    return credenciais.username


# ------------------------------------------------------------------ filtros
def montar_filtros(
    unidade: str | None = Query(None), de: str | None = Query(None), ate: str | None = Query(None),
    vendedor: str | None = Query(None), origem: str | None = Query(None), campanha: str | None = Query(None),
) -> q.Filtros:
    s = get_settings()
    hoje = datetime.now(TZ).date()
    explicitas = bool(_data(de) or _data(ate))
    inicio = _data(de) or _um_mes_antes(hoje)            # padrao: 1 mes de diferenca entre De e Ate
    fim = min(_data(ate) or hoje, hoje)          # o calendario nao passa de hoje
    inicio = max(inicio, s.backfill_inicio)
    if inicio > fim:
        inicio, fim = fim, inicio
    chaves = [u.chave for u in s.unidades]
    lojas = chaves if unidade == "todas" else ([unidade] if unidade in chaves else [chaves[0]])
    vend = vendedor.strip() if vendedor and vendedor.strip() in taxonomia.VENDEDORES else None
    orig = origem.strip() if origem and origem.strip() in q.ORIGENS else None
    camp = campanha.strip() if campanha and campanha.strip() else None
    return q.Filtros(de=inicio, ate=fim, lojas=lojas, vendedor=vend, origem=orig, campanha=camp, datas_explicitas=explicitas)


def _um_mes_antes(d: date) -> date:
    """Mesmo dia do mes anterior (16/09 -> 16/08); se o mes anterior for mais curto, o ultimo dia dele."""
    ano, mes = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    ultimo = (date(ano + (mes == 12), (mes % 12) + 1, 1) - timedelta(days=1)).day
    return date(ano, mes, min(d.day, ultimo))


def _data(texto: str | None) -> date | None:
    try:
        return date.fromisoformat(texto) if texto else None
    except ValueError:
        return None


def unidade_param(f: q.Filtros) -> str:
    return "todas" if len(f.lojas) > 1 else f.lojas[0]


def querystring(f: q.Filtros, **extra: Any) -> str:
    # De/Ate so entram no link quando foram escolhidas no calendario; sem elas, o link abre
    # sempre em "hoje - 1 mes -> hoje", mesmo se for salvo nos favoritos.
    base = {"unidade": unidade_param(f),
            "de": f.de.isoformat() if f.datas_explicitas else None,
            "ate": f.ate.isoformat() if f.datas_explicitas else None,
            "vendedor": f.vendedor, "origem": f.origem, "campanha": f.campanha}
    base.update(extra)
    return urlencode({k: v for k, v in base.items() if v not in (None, "")})


# Versao dos arquivos estaticos: muda quando o CSS muda, para o navegador nao usar um app.css
# antigo do cache depois de uma publicacao (sem isso, uma classe nova de layout nao aparece).
import hashlib
import pathlib


def _versao_estatico() -> str:
    try:
        return hashlib.md5(pathlib.Path("app/dashboard/static/app.css").read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


VERSAO_ESTATICO = _versao_estatico()


async def _contexto(request: Request, f: q.Filtros, conn: Any) -> dict[str, Any]:
    s = get_settings()
    selecionadas = [u for u in s.unidades if u.chave in f.lojas]
    avisos = []
    for u in selecionadas:
        if not u.tem_conversas:
            avisos.append(f"{u.nome}: conversas ainda não conectadas (falta o token da conta da Helena de {u.nome}) — "
                          "leads, CPL por conversa e funil não incluem esta unidade.")
        if not u.tem_meta:
            avisos.append(f"{u.nome}: Meta Ads sem dados no Reportei (integração desconectada) — "
                          "investimento e leads de Facebook/Instagram não incluem esta unidade.")
    return {
        "request": request, "titulo": s.dashboard_titulo, "empresa": s.empresa_nome, "filtros": f, "qs": querystring(f),
        "unidade_param": unidade_param(f), "unidades": s.unidades, "selecionadas": selecionadas,
        "nome_unidades": " + ".join(u.nome for u in selecionadas), "avisos": avisos,
        "opcoes": {"vendedores": sorted(taxonomia.VENDEDORES), "origens": q.ORIGENS,
                   "campanhas": await q.campanhas_disponiveis(conn, f)},
        "tx": taxonomia, "hoje": datetime.now(TZ).date(), "agora": datetime.now(TZ),
        "dias": (f.ate - f.de).days + 1, "backfill_inicio": s.backfill_inicio, "versao": VERSAO_ESTATICO,
    }


def _js(dados: Any) -> str:
    return json.dumps(dados, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ paginas
@router.get("/", response_class=HTMLResponse)
async def origem_roi(request: Request, f: q.Filtros = Depends(montar_filtros), _: str = Depends(exigir_login)):
    async with get_engine().connect() as conn:
        ctx = await _contexto(request, f, conn)
        midia = await q.midia_totais(conn, f)
        conversas = await q.conversas_totais(conn, f)
        vendas = await q.vendas_totais(conn, f)
        por_midia = await q.origem_por_midia(conn, f)
        por_campanha = await q.origem_por_campanha(conn, f)
        campanhas = await q.tabela_campanhas(conn, f)
        vendas_origem = await q.vendas_por_origem(conn, f)
        serie = await q.serie_diaria(conn, f)
        disparos = await q.disparos_resumo(conn, f)
        campanhas_disparo = await q.campanhas_disparo(conn, f)
        digitais = await q.vendas_digitais(conn, f)
    ctx.update(
        digitais=digitais,
        midia=midia, conversas=conversas, vendas=vendas, por_midia=por_midia, por_campanha=por_campanha,
        campanhas=campanhas, vendas_origem=vendas_origem, serie=serie, disparos=disparos,
        campanhas_disparo=campanhas_disparo,
        graficos=_js({
            "por_midia": por_midia, "por_campanha": por_campanha, "vendas_origem": vendas_origem, "serie": serie,
            "campanha_barras": [{"rotulo": _curta(r["campanha"]), "campanha": r["campanha"], "total": r["total"], "agrupado": r.get("agrupado", False)} for r in por_campanha],
            "cpl_plat": [{"rotulo": _curta(l["campanha"]), "campanha": l["campanha"], "plataforma": l["plataforma"], "cpl": round(l["cpl_plataforma"], 2), "leads": l["leads_plataforma"]}
                         for l in sorted([x for x in campanhas["linhas"] if x["cpl_plataforma"]], key=lambda x: x["cpl_plataforma"])],
            "cpl_conv": [{"rotulo": _curta(l["campanha"]), "campanha": l["campanha"], "cpl": round(l["cpl_conversas"], 2), "leads": l["conversas"]}
                        for l in sorted([x for x in campanhas["linhas"] if x["cpl_conversas"]], key=lambda x: x["cpl_conversas"])],
            "invest_midia": [
                {"rotulo": "Instagram (Meta Ads)", "origem": taxonomia.MIDIA_INSTAGRAM, "total": midia["invest_instagram"], "cor": taxonomia.MIDIA_COR[taxonomia.MIDIA_INSTAGRAM]},
                {"rotulo": "Facebook (Meta Ads)", "origem": taxonomia.MIDIA_FACEBOOK, "total": midia["invest_facebook"], "cor": taxonomia.MIDIA_COR[taxonomia.MIDIA_FACEBOOK]},
                # WhatsApp, Messenger e Audience Network: a Meta cobra, mas nao e Facebook nem Instagram
                {"rotulo": "Meta — outros canais", "origem": None, "total": max(midia["invest_meta"] - midia["invest_facebook"] - midia["invest_instagram"], 0.0), "cor": "#94a3b8"},
                {"rotulo": "Google Ads", "origem": taxonomia.MIDIA_GOOGLE, "total": midia["invest_google"], "cor": taxonomia.MIDIA_COR[taxonomia.MIDIA_GOOGLE]},
            ],
            "invest_campanha": [
                {"rotulo": _curta(l["campanha"]), "campanha": l["campanha"], "total": l["investimento"], "plataforma": l["plataforma"],
                 "facebook": l["invest_facebook"] if f.origem in (None, taxonomia.MIDIA_FACEBOOK) else 0.0,
                 "instagram": l["invest_instagram"] if f.origem in (None, taxonomia.MIDIA_INSTAGRAM) else 0.0,
                 "outros": l["invest_outros"] if f.origem is None else 0.0,
                 "google": l["investimento"] if l["plataforma"] == "Google" else 0.0}
                for l in campanhas["linhas"]
            ],
        }),
    )
    return templates.TemplateResponse(request, "origem_roi.html", ctx)


@router.get("/funil", response_class=HTMLResponse)
async def funil(request: Request, f: q.Filtros = Depends(montar_filtros), _: str = Depends(exigir_login)):
    s = get_settings()
    async with get_engine().connect() as conn:
        ctx = await _contexto(request, f, conn)
        midia = await q.midia_totais(conn, f)
        conversas = await q.conversas_totais(conn, f)
        vendas = await q.vendas_totais(conn, f)
        compraram = await q.leads_que_compraram(conn, f)
        por_campanha = await q.conversas_por_campanha(conn, f)
        top_vendas = await q.top_vendedores_vendas(conn, f)
        top_atend = await q.top_vendedores_atendimento(conn, f)
        disparos = await q.disparos_resumo(conn, f)
        campanhas_disparo = await q.campanhas_disparo(conn, f)
        digitais = await q.vendas_digitais(conn, f)
    qualificacao = sorted(por_campanha.items(), key=lambda kv: kv[1]["total"], reverse=True)[:12]
    if disparos["respondidos"]:
        qualificacao.append((f"Disparos WhatsApp — {disparos['respondidos']} responderam de {disparos['enviados']} enviados",
                             {"total": disparos["respondidos"], **{k: disparos[k] for k in ("comprou", "perdido", "encerrada", "andamento")}}))
    ctx.update(
        midia=midia, conversas=conversas, vendas=vendas, compraram=compraram, qualificacao=qualificacao,
        top_vendas=top_vendas, top_atend=top_atend, disparos=disparos, digitais=digitais,
        campanhas_disparo=campanhas_disparo,
        graficos=_js({
            "qualificacao": [{"campanha": _curta(k), "nome": k, **v} for k, v in qualificacao],
            "situacoes": taxonomia.SITUACOES, "cores": taxonomia.SITUACAO_COR,
        }),
    )
    return templates.TemplateResponse(request, "funil.html", ctx)


@router.get("/api/resumo")
async def api_resumo(f: q.Filtros = Depends(montar_filtros), _: str = Depends(exigir_login)) -> JSONResponse:
    async with get_engine().connect() as conn:
        dados = {
            "filtros": {"unidades": f.lojas, "de": f.de, "ate": f.ate, "vendedor": f.vendedor, "origem": f.origem, "campanha": f.campanha},
            "midia": await q.midia_totais(conn, f), "conversas": await q.conversas_totais(conn, f),
            "vendas": await q.vendas_totais(conn, f), "campanhas": await q.tabela_campanhas(conn, f),
        }
    return JSONResponse(json.loads(_js(dados)))
