-- =====================================================================================
-- 002 - vendas do DMS cruzadas com o contato da Helena
-- =====================================================================================
-- Em producao esta tabela e alimentada por outra integracao (DMS da concessionaria ->
-- banco, casando o telefone do comprador com o contato da Helena) e o monitor so LE.
-- Ela e criada aqui para o projeto rodar sozinho (modo demo e testes).
CREATE TABLE IF NOT EXISTS vendas_dms (
    venda_row_id         bigserial PRIMARY KEY,
    loja                 text NOT NULL,
    data_venda           timestamptz NOT NULL,
    vendedor             text,                 -- login do vendedor no DMS
    valor_vendido        numeric(12,2) NOT NULL,
    telefone_normalizado text,                 -- so digitos (DDD + numero)
    helena_contact_id    text,                 -- contato da Helena com o mesmo telefone (null = nao achou)
    veio_campanha        boolean NOT NULL DEFAULT false,  -- o contato chegou por anuncio ou disparo
    utm_source           text,
    utm_campaign         text                  -- ID do anuncio Meta, quando houver
);
CREATE INDEX IF NOT EXISTS vendas_dms_loja_dia_idx ON vendas_dms (loja, data_venda);
CREATE INDEX IF NOT EXISTS vendas_dms_contato_idx  ON vendas_dms (helena_contact_id);
CREATE INDEX IF NOT EXISTS vendas_dms_telefone_idx ON vendas_dms (telefone_normalizado);
