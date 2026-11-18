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
