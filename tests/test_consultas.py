"""Consultas do painel contra um Postgres de verdade, com os dados de demonstracao.

Pulados se TEST_DATABASE_URL nao estiver definido. ATENCAO: o banco indicado e apagado.

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/funil_teste pytest
"""

import asyncio
import os
from datetime import timedelta

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nao definido")
if URL:
    os.environ["DATABASE_URL"] = URL
    os.environ["MODO_DEMO"] = "true"


@pytest.fixture(scope="module")
def dados():
    from app import demo, migrate
    from app.config import TZ
    from app.dashboard import queries as q
    from app.db import dispose_engine, get_engine
    from datetime import datetime

    async def rodar():
        await migrate.aplicar()
        await demo.popular(dias=60, semente=7)
        hoje = datetime.now(TZ).date()
        f = q.Filtros(de=hoje - timedelta(days=29), ate=hoje, lojas=["centro", "norte"])
        async with get_engine().connect() as conn:
            r = {
                "midia": await q.midia_totais(conn, f), "conversas": await q.conversas_totais(conn, f),
                "vendas": await q.vendas_totais(conn, f), "por_midia": await q.origem_por_midia(conn, f),
                "vendas_origem": await q.vendas_por_origem(conn, f), "campanhas": await q.tabela_campanhas(conn, f),
                "por_campanha": await q.conversas_por_campanha(conn, f), "serie": await q.serie_diaria(conn, f),
                "digitais": await q.vendas_digitais(conn, f), "disparos": await q.disparos_resumo(conn, f),
            }
            f_ig = q.Filtros(de=f.de, ate=f.ate, lojas=f.lojas, origem="Instagram")
            r["midia_ig"] = await q.midia_totais(conn, f_ig)
            r["conversas_ig"] = await q.conversas_totais(conn, f_ig)
        await dispose_engine()
        return r

    return asyncio.run(rodar())


def test_rosca_de_midia_soma_o_total_de_conversas(dados):
    assert sum(r["total"] for r in dados["por_midia"]) == dados["conversas"]["total"] > 0


def test_rosca_de_vendas_soma_o_total_de_vendas(dados):
    assert sum(r["total"] for r in dados["vendas_origem"]) == dados["vendas"]["total"] > 0


def test_situacoes_das_conversas_fecham_o_total(dados):
    for situacao in dados["por_campanha"].values():
        partes = situacao["comprou"] + situacao["perdido"] + situacao["encerrada"] + situacao["andamento"]
        assert partes == situacao["total"]


def test_investimento_da_meta_e_facebook_mais_instagram_mais_outros(dados):
    m = dados["midia"]
    assert m["invest_facebook"] + m["invest_instagram"] <= m["invest_meta"]
    assert m["invest_meta"] - m["invest_facebook"] - m["invest_instagram"] < 0.05 * m["invest_meta"]


def test_serie_diaria_bate_com_as_conversas(dados):
    assert sum(dados["serie"]["conversas"]) == dados["conversas"]["total"]


def test_venda_com_anuncio_e_disparo_conta_uma_vez(dados):
    d = dados["digitais"]
    assert d["total"] == d["anuncio"] + d["disparo"] - d["ambos"]
    assert d["anuncio"] == dados["vendas"]["com_anuncio"]


def test_filtro_de_origem_recorta_midia_e_conversas(dados):
    assert dados["midia_ig"]["invest_meta"] == dados["midia"]["invest_instagram"]
    assert dados["midia_ig"]["invest_google"] == 0
    assert 0 < dados["conversas_ig"]["total"] < dados["conversas"]["total"]


def test_cac_e_retorno_das_campanhas(dados):
    for l in dados["campanhas"]["linhas"]:
        if l["cac"] is not None:
            assert l["cac"] == pytest.approx(l["investimento"] / l["vendas"])
        if l["retorno"] is not None:
            assert l["retorno"] == pytest.approx(l["receita"] / l["investimento"])
