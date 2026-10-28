"""Regras puras do monitor: classificacao de midia, leads do Google, vendedores, leitura da
conversa da Helena e formatacao do painel. Rodam sem banco e sem API."""

import os
from datetime import date

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x@localhost/x")

from app import taxonomia as tx  # noqa: E402
from app.dashboard.routes import _brl, _curta, _pct, _um_mes_antes  # noqa: E402
from app.pipeline import _loja_disparo, _montar_conversa, _telefone  # noqa: E402


def test_midia_pela_utm():
    assert tx.midia_de("FACEBOOK") == tx.MIDIA_FACEBOOK
    assert tx.midia_de(" instagram ") == tx.MIDIA_INSTAGRAM
    assert tx.midia_de("GOOGLE") == tx.MIDIA_GOOGLE
    assert tx.midia_de("AD - 1234") == tx.MIDIA_META_SEM_ID
    assert tx.midia_de(None) == tx.MIDIA_SEM_UTM
    assert tx.midia_de("tiktok") == tx.MIDIA_OUTROS


def test_acao_google_que_conta_como_lead():
    assert tx.acao_google_e_lead("Contato - botão WhatsApp")
    assert tx.acao_google_e_lead("Lead form - Submit")
    assert not tx.acao_google_e_lead("Ligação do anúncio")
    assert not tx.acao_google_e_lead("Rotas para a loja")


def test_vendedor_nome_e_login():
    assert tx.primeiro_nome("ana paula souza") == "Ana"
    assert tx.primeiro_nome("João Pedro Alves") == "João P."
    assert tx.primeiro_nome(None) is None
    assert tx.nome_vendedor("BRUNO.S") == "Bruno"
    assert tx.nome_vendedor("NOVO.VENDEDOR") == "Novo Vendedor"   # fora do mapa: nome tirado do login
    assert tx.nome_vendedor(None) == "—"


def test_telefone_normalizado():
    assert _telefone({"phonenumber": "+55|11900000000"}) == "11900000000"
    assert _telefone({"phoneNumber": "+5511900000000"}) == "11900000000"
    assert _telefone(None) is None


def test_loja_do_disparo():
    lojas = ["centro", "norte"]
    assert _loja_disparo("centro", "Disparos Norte", "", lojas) == "norte"
    assert _loja_disparo("centro", None, "revisao_norte", lojas) == "norte"
    assert _loja_disparo("centro", "Disparos Centro", "lancamento_norte", lojas) == "centro"   # departamento manda
    assert _loja_disparo("norte", "Vendas", "promo_geral", lojas) == "norte"                  # sem pista: conta


def test_conversa_da_helena():
    sessao = {
        "id": "s1", "createdAt": "2026-10-01T13:00:00Z", "updatedAt": "2026-10-01T15:00:00Z",
        "status": "COMPLETED", "origin": "Contato", "contactId": "c1",
        "utm": {"source": "INSTAGRAM", "campaign": "120200000000000001"},
        "agentDetails": {"name": "Carla Mendes"}, "classification": {"category": "UNDEFINED"},
        "contactDetails": {"phonenumber": "+55|11900000001"},
    }
    c = _montar_conversa(sessao, "centro")
    assert c["midia"] == tx.MIDIA_INSTAGRAM
    assert c["vendedor"] == "Carla"
    assert c["classificacao"] is None
    assert c["telefone"] == "11900000001"
    assert c["criado_em"].tzinfo is not None
    assert _montar_conversa({**sessao, "type": "GROUP"}, "centro") is None


def test_formatacao_do_painel():
    assert _brl(1234567.8) == "R$ 1.234.568"
    assert _brl(15.5, 2) == "R$ 15,50"
    assert _brl(None) == "—"
    assert _pct(1, 4) == "25,0%"
    assert _pct(1, 0) == "—"
    assert _curta("AutoVale_Norte_SUV_Lancamento") == "Norte SUV Lancamento"


def test_periodo_padrao_um_mes():
    assert _um_mes_antes(date(2026, 10, 28)) == date(2026, 9, 28)
    assert _um_mes_antes(date(2026, 3, 31)) == date(2026, 2, 28)
    assert _um_mes_antes(date(2026, 1, 15)) == date(2025, 12, 15)
