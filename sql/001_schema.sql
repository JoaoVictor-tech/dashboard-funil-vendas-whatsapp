-- =====================================================================================
-- Monitor de Funil - schema base
-- =====================================================================================
-- Prefixo mf_ para nao colidir com outras tabelas quando o banco e compartilhado.
-- Toda tabela coletada tem `loja` (a unidade).
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- Conversas da Helena (1 linha por sessao). Origem "Contato" = o cliente escreveu
-- (lead); "Campanha" = disparo em massa (fica gravado, mas o dashboard nao conta como lead).
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mf_conversas (
    session_id              text PRIMARY KEY,
    loja                    text NOT NULL,
    numero                  text,
    criado_em               timestamptz NOT NULL,
    atualizado_em_origem    timestamptz,
    status                  text,
    origem                  text,
    canal_id                text,
    departamento            text,
    user_id                 text,
    vendedor                text,
    contato_id              text,
    telefone                text,            -- so digitos (DDD + numero), para casar com o DMS
    utm_source              text,
    utm_campaign            text,            -- ID do anuncio Meta
    utm_headline            text,
    midia                   text NOT NULL,   -- Facebook | Instagram | Google | Meta s/ id | Sem UTM | Outros
    campanha                text,            -- nome da campanha Meta, resolvido por mf_anuncios
    primeira_resposta_em    timestamptz,
    ultima_msg_cliente      timestamptz,
    ultima_msg_equipe       timestamptz,
    encerrada_em            timestamptz,
    classificacao           text,            -- WON | LOST | OTHER | null
    bot_id                  text,
    disparos_verificados_em timestamptz,     -- ate quando as mensagens ja foram varridas em busca de disparos
    coletado_em             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mf_conversas_loja_idx     ON mf_conversas (loja, origem, criado_em);
CREATE INDEX IF NOT EXISTS mf_conversas_contato_idx  ON mf_conversas (contato_id);
CREATE INDEX IF NOT EXISTS mf_conversas_telefone_idx ON mf_conversas (telefone);

-- -------------------------------------------------------------------------------------
-- Midia paga, por dia e campanha (Reportei -> Meta Ads e Google Ads)
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mf_midia_diaria (
    loja                 text NOT NULL,
    dia                  date NOT NULL,
    plataforma           text NOT NULL,        -- meta | google
    campanha             text NOT NULL,
    campanha_id          text,
    tipo                 text,                 -- Google: Search/Display/...; Meta: tipo do resultado
    investimento         numeric(12,2) NOT NULL DEFAULT 0,
    resultados           numeric(12,2) NOT NULL DEFAULT 0,  -- Meta: conversas iniciadas; Google: conversoes
    alcance              bigint,
    impressoes           bigint,
    cliques              bigint,
    -- Meta separada por Facebook x Instagram (dimensao publisher_platform). "outros" =
    -- WhatsApp / Messenger / Audience Network.
    invest_facebook      numeric(12,2) NOT NULL DEFAULT 0,
    invest_instagram     numeric(12,2) NOT NULL DEFAULT 0,
    invest_outros        numeric(12,2) NOT NULL DEFAULT 0,
    resultados_facebook  numeric(12,2) NOT NULL DEFAULT 0,
    resultados_instagram numeric(12,2) NOT NULL DEFAULT 0,
    coletado_em          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (loja, dia, plataforma, campanha)
);

-- Totais do dia por plataforma (o que so existe agregado: gasto por Facebook x Instagram,
-- acoes de conversao de contato do Google).
CREATE TABLE IF NOT EXISTS mf_midia_diaria_total (
    loja                text NOT NULL,
    dia                 date NOT NULL,
    invest_meta         numeric(12,2) NOT NULL DEFAULT 0,
    invest_facebook     numeric(12,2) NOT NULL DEFAULT 0,
    invest_instagram    numeric(12,2) NOT NULL DEFAULT 0,
    conversas_meta      integer NOT NULL DEFAULT 0,
    conversas_facebook  integer NOT NULL DEFAULT 0,
    conversas_instagram integer NOT NULL DEFAULT 0,
    invest_google       numeric(12,2) NOT NULL DEFAULT 0,
    conversoes_google   numeric(12,2) NOT NULL DEFAULT 0,  -- todas as acoes
    leads_google        numeric(12,2) NOT NULL DEFAULT 0,  -- so acoes de contato (WhatsApp, formulario)
    acoes_google        jsonb,
    coletado_em         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (loja, dia)
);

-- Anuncio Meta -> campanha. E a ponte entre o utm.campaign da Helena (ID do anuncio) e o
-- nome da campanha que aparece no investimento. O ID e unico entre contas.
CREATE TABLE IF NOT EXISTS mf_anuncios (
    anuncio_id    text PRIMARY KEY,
    loja          text NOT NULL,
    anuncio_nome  text,
    campanha      text NOT NULL,
    primeiro_dia  date,
    ultimo_dia    date,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------------------------------
-- Disparos de WhatsApp: 1 linha por MENSAGEM de campanha (origem CAMPAIGN), em qualquer
-- conversa - um disparo para quem ja tem conversa aberta nao cria conversa nova.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mf_templates (
    id            text PRIMARY KEY,
    loja          text NOT NULL,
    nome          text NOT NULL,
    canal_id      text,
    tipo          text,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mf_disparos (
    mensagem_id   text PRIMARY KEY,
    loja          text NOT NULL,          -- loja do disparo (departamento / nome do modelo / conta)
    session_id    text NOT NULL,
    contato_id    text,
    telefone      text,
    template_id   text,
    enviado_em    timestamptz NOT NULL,
    respondeu_em  timestamptz,            -- primeira mensagem do cliente depois do disparo (na mesma conversa)
    coletado_em   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mf_disparos_loja_dia_idx ON mf_disparos (loja, enviado_em);
CREATE INDEX IF NOT EXISTS mf_disparos_sessao_idx   ON mf_disparos (session_id);
CREATE INDEX IF NOT EXISTS mf_disparos_telefone_idx ON mf_disparos (telefone);

-- -------------------------------------------------------------------------------------
-- Operacao do coletor
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mf_monitor_execucoes (
    id            bigserial PRIMARY KEY,
    iniciado_em   timestamptz NOT NULL DEFAULT now(),
    terminado_em  timestamptz,
    gatilho       text,
    loja          text,
    status        text NOT NULL DEFAULT 'rodando',
    conversas     integer,
    dias_midia    integer,
    anuncios      integer,
    erro          text,
    detalhes      jsonb
);

CREATE TABLE IF NOT EXISTS mf_monitor_estado (
    chave         text PRIMARY KEY,
    valor         text,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);
