# Monitor de funil de vendas: do anúncio à venda, passando pelo WhatsApp

Dashboard que responde à pergunta que todo gestor de concessionária faz: **quanto custou cada
venda que veio do digital, e de qual campanha ela veio?**

Ele cruza três fontes que normalmente não conversam entre si:

- **Mídia paga:** o que Meta Ads e Google Ads gastaram e quantos leads reportaram, por dia e por campanha (via API do [Reportei](https://reportei.com)).
- **Conversas no WhatsApp:** o que de fato virou conversa, coletado da API da Helena (plataforma de atendimento e CRM). A UTM do clique liga cada conversa ao anúncio e à campanha.
- **Vendas:** os carros faturados no DMS (sistema de gestão da concessionária), ligados à conversa pelo telefone e pelo contato do comprador.

Foi desenvolvido durante meu estágio para uma concessionária real com duas lojas. Esta é uma
versão de portfólio: a empresa (**Auto Vale**, unidades **Centro** e **Zona Norte**), as
campanhas, os vendedores e todos os números são fictícios, gerados por `app/demo.py`.

![Origem dos leads, investimento e ROI](docs/origem-investimento-roi.png)

<details>
<summary>Página "Funil e time"</summary>

![Funil e time](docs/funil-e-time.png)

</details>

## O que o painel mostra

| Página | Responde |
| --- | --- |
| **Origem, investimento e ROI** | De onde vieram os leads (Facebook, Instagram, Google, sem UTM), quanto cada mídia e cada campanha custou, o CPL segundo a plataforma × o CPL pelas conversas reais, a origem das vendas, o CAC e o ROI por campanha, e os resultados dos disparos de WhatsApp. |
| **Funil e time** | Leads reportados × conversas iniciadas × conversas com UTM, quantos leads compraram, a situação das conversas por campanha (comprou, perdido, encerrada, em andamento) e o ranking de vendedores em atendimento e em vendas. |

Os filtros do topo valem para a página inteira: **unidade**, **período** (por calendário),
**origem**, **campanha** e **vendedor**. Clicar numa fatia ou barra de gráfico aplica o filtro
daquela origem ou campanha, e todo bloco tem um **?** com a explicação em uma frase. Tem
tema claro e escuro.

## Arquitetura

```
              ┌──────────────── a cada 60 min (APScheduler), por unidade ────────────────┐
  Helena   ───┤  app/pipeline.py                                                         │
  (conversas, │    ├─ clients/helena.py    conversas por data de atualização (marca d'água) │
   1 conta    │    │                       e mensagens de disparo (origem CAMPAIGN)      │
   por loja)  │    ├─ clients/reportei.py  1 dia por vez: campanhas, anúncios e totais   │
  Reportei ───┤    │                       da Meta; campanhas e conversões do Google     │
  (Meta +     │    └─ repository.py        UPSERT idempotente nas tabelas mf_*           │
   Google)    └──────────────────────────────────────────────────────────────────────────┘
                                       │
  DMS (vendas_dms) ────────────────────┤  lida direto pelo dashboard (mesmo banco)
                                       │
                 app/dashboard/  ──────┴──────  FastAPI + Jinja2 + Chart.js
```

Um único processo serve o dashboard **e** roda o coletor. Não tem fila, Redis nem n8n: só
FastAPI, Postgres e um agendador.

## Como cada número é montado

| Métrica | Regra |
| --- | --- |
| **Investimento** | Reportei, por dia, campanha e unidade. A Meta vem separada em Facebook × Instagram × outros canais (dimensão `publisher_platform`). |
| **Leads das plataformas** | Meta: "conversas iniciadas por mensagem". Google: só as ações de conversão de **contato** (botão de WhatsApp, formulário). Ligação e rota ficam de fora (`taxonomia.acao_google_e_lead`). |
| **Conversas** | Conversas da Helena com origem "Contato" (o cliente escreveu primeiro). Disparos em massa não contam como lead. |
| **Campanha da conversa** | A `utm.campaign` é o **ID do anúncio** Meta. A tabela `mf_anuncios`, montada a partir do relatório de anúncios do Reportei, liga o ID ao nome da campanha. |
| **Vendas** | `vendas_dms`: DMS cruzado com o contato da Helena pelo telefone. "Originada em anúncio" = o comprador chegou com UTM de Facebook, Instagram ou Google. |
| **Vendas de disparo** | A venda é atribuída ao **último** disparo que o telefone recebeu antes da compra, e cada venda conta uma vez. |
| **CAC / ROI** | Investimento da campanha no período ÷ vendas atribuídas a ela. O ROI usa a receita **bruta** (valor do carro), não a margem. |

## Decisões de projeto

- **O mesmo recorte em tudo:** todas as consultas recebem o mesmo objeto `Filtros`, então o KPI, o gráfico e a tabela de uma página nunca discordam.
- **Plataforma × realidade lado a lado:** o painel mostra o que a Meta diz que gerou e o que de fato virou conversa, porque a diferença entre os dois (cliques que não chegam ao WhatsApp) é informação para o gestor.
- **Os últimos 7 dias de mídia são sempre recoletados**, porque a Meta reatribui "conversas iniciadas" por até 7 dias.
- **Coleta incremental com marca d'água:** as conversas são buscadas por data de atualização com uma sobreposição de 12 h. Assim, uma conversa antiga que foi encerrada ou marcada como perdida é reescrita.
- **Disparo é mensagem, não conversa:** um disparo para quem já tem conversa aberta não cria conversa nova. Por isso o coletor varre as mensagens de origem CAMPAIGN em qualquer conversa.
- **Unidade sem fonte não quebra a tela:** se uma loja não tem o token da Helena ou a integração da Meta, a barra de filtros avisa e o painel segue com o que existe.
- **Limites das APIs respeitados:** o Reportei aceita 100 chamadas por minuto e a Helena 1000 requisições a cada 5 minutos. O coletor pausa entre as chamadas e repete em caso de 429.
- **Migrações em SQL puro** (`sql/`, aplicadas por `app/migrate.py` na subida), para que o mesmo script possa ser colado num console do Postgres.

## Como rodar (demonstração, sem nenhuma API)

```bash
cp .env.example .env                         # já vem com MODO_DEMO=true
docker compose up -d --build                 # Postgres + API
docker compose exec api python -m app.cli demo   # 120 dias de dados fictícios
```

Abra http://127.0.0.1:8076/dashboard com o usuário e a senha do `.env`.

Com `MODO_DEMO=true`, o coletor fica desligado e o comando `demo` recusa rodar fora desse
modo, porque ele apaga as tabelas do monitor.

### Com dados reais

Coloque `MODO_DEMO=false`, preencha os tokens da Helena e os IDs das integrações do Reportei
no `.env` e suba de novo. O coletor faz o backfill sozinho a partir de `BACKFILL_INICIO`.

```bash
docker compose exec api python -m app.cli testar-conexoes   # Postgres, Helena e Reportei
docker compose exec api python -m app.cli coletar --dias-midia 120
```

## Testes

```bash
pip install -r requirements-dev.txt
pytest                                                  # regras puras (sem banco)
TEST_DATABASE_URL=postgresql+asyncpg://usuario:senha@localhost:5432/funil_teste pytest
```

Os testes de regras cobrem a classificação de mídia pela UTM, as ações do Google que contam
como lead, o cruzamento de vendedores, a leitura da conversa da Helena e a formatação do painel.

Com `TEST_DATABASE_URL`, os testes também rodam as consultas contra um Postgres de verdade,
populado pelo gerador de demo. Eles conferem que os números fecham entre si:
- as fatias das roscas somam o total;
- a situação das conversas soma o total de cada campanha;
- uma venda com anúncio **e** disparo conta uma vez só;
- CAC e ROI batem com investimento, vendas e receita.

O banco indicado é apagado.

## Estrutura

```
app/
  main.py               FastAPI: dashboard, /health, /admin/coletar e o agendador
  pipeline.py           coletor (Helena + Reportei, por unidade)
  clients/helena.py     API da Helena: conversas, mensagens, modelos de mensagem
  clients/reportei.py   API do Reportei: Meta Ads e Google Ads, por dia
  repository.py         UPSERTs idempotentes e estado do coletor
  taxonomia.py          vocabulário fechado: mídias, situações, vendedores
  dashboard/queries.py  todas as consultas do painel (um único Filtros)
  dashboard/routes.py   páginas, filtros, login HTTP Basic
  dashboard/templates/  Jinja2 + Chart.js
  demo.py               gerador de dados fictícios
  cli.py                coletar, testar-conexoes, mapear-anuncios, demo
sql/                    migrações
tests/                  regras puras e consultas no Postgres
```

## Tecnologias

Python 3.12 · FastAPI · Jinja2 · Chart.js · PostgreSQL · SQLAlchemy (async) + asyncpg ·
APScheduler · httpx · API da Helena · API do Reportei (Meta Ads e Google Ads) · Docker Compose ·
pytest

## Autor

João Victor Lopez de Arruda · [GitHub](https://github.com/JoaoVictor-tech) · [LinkedIn](https://linkedin.com/in/joao-victor-arruda)
