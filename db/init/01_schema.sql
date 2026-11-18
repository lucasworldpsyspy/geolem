-- GeoLEM: esquema do banco geográfico
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS geo;

-- Normaliza qualquer geometria para MultiPolygon válido em EPSG:4326
CREATE OR REPLACE FUNCTION geo.normalizar(g geometry)
RETURNS geometry(MultiPolygon, 4326)
LANGUAGE sql STABLE STRICT AS $$
  SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_Force2D(
           CASE WHEN ST_SRID(g) = 0 THEN ST_SetSRID(g, 4326)
                WHEN ST_SRID(g) <> 4326 THEN ST_Transform(g, 4326)
                ELSE g END)), 3))::geometry(MultiPolygon, 4326)
$$;

-- Registro de cada carga de base pública (rastreabilidade do laudo)
CREATE TABLE IF NOT EXISTS geo.cargas (
  id            serial PRIMARY KEY,
  fonte         text NOT NULL,
  tabela        text NOT NULL,
  descricao     text,
  url           text,
  iniciado_em   timestamptz NOT NULL DEFAULT now(),
  concluido_em  timestamptz,
  registros     integer,
  status        text NOT NULL DEFAULT 'executando',  -- executando | ok | erro
  erro          text
);

CREATE TABLE IF NOT EXISTS geo.desmatamento (
  id           bigserial PRIMARY KEY,
  fonte        text NOT NULL,
  carga_id     integer REFERENCES geo.cargas(id),
  id_origem    text,
  ano          integer,
  data_imagem  date,
  classe       text,
  area_km2     double precision,
  satelite     text,
  geom         geometry(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS geo.embargos (
  id                     bigserial PRIMARY KEY,
  fonte                  text NOT NULL,
  carga_id               integer REFERENCES geo.cargas(id),
  id_origem              text,
  numero_tad             text,
  serie_tad              text,
  data_embargo           date,
  municipio              text,
  uf                     text,
  nome_imovel            text,
  infracao               text,
  situacao_desmatamento  text,
  area_embargada_ha      double precision,
  geom                   geometry(MultiPolygon, 4326) NOT NULL
);
-- Por LGPD, nome e CPF/CNPJ do embargado NÃO são armazenados.

CREATE TABLE IF NOT EXISTS geo.unidades_conservacao (
  id         bigserial PRIMARY KEY,
  fonte      text NOT NULL,
  carga_id   integer REFERENCES geo.cargas(id),
  id_origem  text,
  nome       text,
  categoria  text,
  grupo      text CHECK (grupo IN ('PI', 'US')),  -- Proteção Integral | Uso Sustentável
  esfera     text,
  geom       geometry(MultiPolygon, 4326) NOT NULL
);

CREATE TABLE IF NOT EXISTS geo.terras_indigenas (
  id         bigserial PRIMARY KEY,
  fonte      text NOT NULL,
  carga_id   integer REFERENCES geo.cargas(id),
  id_origem  text,
  nome       text,
  etnia      text,
  fase       text,
  geom       geometry(MultiPolygon, 4326) NOT NULL
);

-- Territórios de povos e comunidades: quilombolas e assentamentos da reforma agrária (INCRA)
CREATE TABLE IF NOT EXISTS geo.territorios (
  id         bigserial PRIMARY KEY,
  fonte      text NOT NULL,
  carga_id   integer REFERENCES geo.cargas(id),
  tipo       text NOT NULL CHECK (tipo IN ('quilombola', 'assentamento')),
  id_origem  text,
  nome       text,
  municipio  text,
  uf         text,
  fase       text,
  familias   integer,
  geom       geometry(MultiPolygon, 4326) NOT NULL
);

-- Imóveis rurais: SIGEF (INCRA) e CAR (SICAR)
CREATE TABLE IF NOT EXISTS geo.imoveis (
  id                bigserial PRIMARY KEY,
  fonte             text NOT NULL,           -- sigef_privado | car_ba | ...
  tipo              text NOT NULL,           -- SIGEF | CAR
  carga_id          integer REFERENCES geo.cargas(id),
  codigo            text,
  nome              text,
  municipio         text,
  uf                text,
  situacao          text,
  area_ha           double precision,
  reserva_legal_ha  double precision,
  geom              geometry(MultiPolygon, 4326) NOT NULL
);

-- Histórico de análises (o protocolo permite verificar um laudo depois)
CREATE TABLE IF NOT EXISTS geo.analises (
  protocolo      text PRIMARY KEY,
  criado_em      timestamptz NOT NULL DEFAULT now(),
  nome_area      text,
  imovel_id      bigint,
  area_ha        double precision,
  resultado      text NOT NULL,
  detalhes       jsonb NOT NULL,
  geom           geometry(MultiPolygon, 4326) NOT NULL
);

-- Imagens de satélite (antes e depois) de cada análise. O SHA-256 de cada PNG fica em detalhes->imagens,
-- coberto pelo código de integridade do laudo.
CREATE TABLE IF NOT EXISTS geo.analises_imagens (
  protocolo  text NOT NULL REFERENCES geo.analises(protocolo) ON DELETE CASCADE,
  tipo       text NOT NULL,  -- antes | depois
  png        bytea NOT NULL,
  PRIMARY KEY (protocolo, tipo)
);

CREATE INDEX IF NOT EXISTS desmatamento_geom_idx ON geo.desmatamento USING gist (geom);
CREATE INDEX IF NOT EXISTS desmatamento_fonte_idx ON geo.desmatamento (fonte);
CREATE INDEX IF NOT EXISTS embargos_geom_idx ON geo.embargos USING gist (geom);
CREATE INDEX IF NOT EXISTS ucs_geom_idx ON geo.unidades_conservacao USING gist (geom);
CREATE INDEX IF NOT EXISTS tis_geom_idx ON geo.terras_indigenas USING gist (geom);
CREATE INDEX IF NOT EXISTS territorios_geom_idx ON geo.territorios USING gist (geom);
CREATE INDEX IF NOT EXISTS imoveis_geom_idx ON geo.imoveis USING gist (geom);
CREATE INDEX IF NOT EXISTS imoveis_codigo_idx ON geo.imoveis (codigo);
CREATE INDEX IF NOT EXISTS imoveis_nome_trgm_idx ON geo.imoveis USING gin (nome gin_trgm_ops);
CREATE INDEX IF NOT EXISTS analises_geom_idx ON geo.analises USING gist (geom);

-- =====================================================================
-- Aplicação: empresas, usuários, sessões, auditoria, carteira e fila
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.empresas (
  id         serial PRIMARY KEY,
  nome       text NOT NULL,
  cnpj       text,
  ativa      boolean NOT NULL DEFAULT true,
  criado_em  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app.usuarios (
  id             serial PRIMARY KEY,
  empresa_id     integer NOT NULL REFERENCES app.empresas(id),
  nome           text NOT NULL,
  email          text NOT NULL,
  senha_hash     text NOT NULL,
  papel          text NOT NULL DEFAULT 'analista' CHECK (papel IN ('admin', 'analista')),
  ativo          boolean NOT NULL DEFAULT true,
  trocar_senha   boolean NOT NULL DEFAULT true,   -- senha provisória: troca obrigatória no 1º acesso
  criado_em      timestamptz NOT NULL DEFAULT now(),
  ultimo_acesso  timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS usuarios_email_idx ON app.usuarios (lower(email));

-- Só o SHA-256 do token fica no banco: um vazamento da tabela não dá acesso a ninguém
CREATE TABLE IF NOT EXISTS app.sessoes (
  token_hash  text PRIMARY KEY,
  usuario_id  integer NOT NULL REFERENCES app.usuarios(id) ON DELETE CASCADE,
  criado_em   timestamptz NOT NULL DEFAULT now(),
  visto_em    timestamptz NOT NULL DEFAULT now(),
  expira_em   timestamptz NOT NULL,
  ip          text,
  agente      text
);
CREATE INDEX IF NOT EXISTS sessoes_usuario_idx ON app.sessoes (usuario_id);

-- Quem fez o quê e quando (login, análises, carteira, usuários)
CREATE TABLE IF NOT EXISTS app.auditoria (
  id          bigserial PRIMARY KEY,
  em          timestamptz NOT NULL DEFAULT now(),
  empresa_id  integer,
  usuario_id  integer,
  acao        text NOT NULL,
  alvo        text,
  detalhes    jsonb,
  ip          text
);
CREATE INDEX IF NOT EXISTS auditoria_empresa_idx ON app.auditoria (empresa_id, em DESC);

-- Carteira: áreas (fornecedores) que cada empresa monitora
CREATE TABLE IF NOT EXISTS app.areas (
  id                bigserial PRIMARY KEY,
  empresa_id        integer NOT NULL REFERENCES app.empresas(id),
  nome              text NOT NULL,
  fornecedor        text,
  codigo_externo    text,              -- código do fornecedor no ERP do cliente
  imovel_id         bigint,
  municipio         text,
  reserva_legal_ha  double precision,
  origem            text,              -- arquivo de onde veio a área
  area_ha           double precision,
  geom              geometry(MultiPolygon, 4326) NOT NULL,
  ativa             boolean NOT NULL DEFAULT true,
  criado_em         timestamptz NOT NULL DEFAULT now(),
  criado_por        integer,
  ultimo_protocolo  text,
  ultimo_status     text,
  status_anterior   text,
  analisado_em      timestamptz
);
CREATE INDEX IF NOT EXISTS areas_empresa_idx ON app.areas (empresa_id) WHERE ativa;

-- Fila de análises processada pelo worker (cadastro, reanálise manual e monitoramento semanal)
CREATE TABLE IF NOT EXISTS app.tarefas (
  id            bigserial PRIMARY KEY,
  empresa_id    integer NOT NULL,
  area_id       bigint NOT NULL REFERENCES app.areas(id) ON DELETE CASCADE,
  usuario_id    integer,
  motivo        text NOT NULL DEFAULT 'cadastro',  -- cadastro | manual | monitoramento
  estado        text NOT NULL DEFAULT 'pendente' CHECK (estado IN ('pendente', 'executando', 'ok', 'erro')),
  tentativas    integer NOT NULL DEFAULT 0,
  erro          text,
  criado_em     timestamptz NOT NULL DEFAULT now(),
  iniciado_em   timestamptz,
  concluido_em  timestamptz
);
CREATE INDEX IF NOT EXISTS tarefas_pendentes_idx ON app.tarefas (id) WHERE estado = 'pendente';
CREATE INDEX IF NOT EXISTS tarefas_area_idx ON app.tarefas (area_id);

-- Mudança de situação de uma área da carteira (ex.: Conforme -> Não conforme)
CREATE TABLE IF NOT EXISTS app.alertas (
  id           bigserial PRIMARY KEY,
  empresa_id   integer NOT NULL,
  area_id      bigint NOT NULL REFERENCES app.areas(id) ON DELETE CASCADE,
  criado_em    timestamptz NOT NULL DEFAULT now(),
  de           text,
  para         text NOT NULL,
  protocolo    text,
  resumo       text,
  lido_em      timestamptz,
  notificado   boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS alertas_empresa_idx ON app.alertas (empresa_id, criado_em DESC);

-- Cada análise sabe de que empresa, usuário e área da carteira ela é
ALTER TABLE geo.analises ADD COLUMN IF NOT EXISTS empresa_id integer,
                         ADD COLUMN IF NOT EXISTS usuario_id integer,
                         ADD COLUMN IF NOT EXISTS area_id bigint;
CREATE INDEX IF NOT EXISTS analises_empresa_idx ON geo.analises (empresa_id, criado_em DESC);

-- Alertas de desmatamento recentes: DETER Cerrado (INPE) e MapBiomas Alerta
CREATE TABLE IF NOT EXISTS geo.alertas_desmatamento (
  id               bigserial PRIMARY KEY,
  fonte            text NOT NULL,           -- deter_cerrado | mapbiomas_alerta
  carga_id         integer REFERENCES geo.cargas(id),
  id_origem        text,
  data_deteccao    date,
  data_publicacao  date,
  area_ha          double precision,
  classe           text,
  situacao         text,
  satelite         text,
  geom             geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS alertas_desm_geom_idx ON geo.alertas_desmatamento USING gist (geom);
CREATE INDEX IF NOT EXISTS alertas_desm_fonte_idx ON geo.alertas_desmatamento (fonte);
