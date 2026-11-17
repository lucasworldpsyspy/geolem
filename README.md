# GeoLEM

Plataforma de **compliance socioambiental de áreas rurais** para o oeste da Bahia, a partir de Luís Eduardo Magalhães.
"GeoLEM" é o nome provisório do projeto.

## Problema
Tradings, armazéns, revendas, bancos e produtores precisam provar que uma área não tem desmatamento recente, embargo,
sobreposição com Unidade de Conservação ou Terra Indígena, e que cumpre a Reserva Legal. Isso vale para o EUDR,
o crédito rural e o ESG. Hoje essa checagem é manual, lenta e cara.

## Estrutura

```
geolem/
├── docker-compose.yml      PostGIS + API (a API também serve o frontend)
├── .env.example            configurações (copie para .env)
├── db/init/01_schema.sql   tabelas, índices espaciais e função de normalização
├── api/
│   ├── app/                API FastAPI
│   │   ├── main.py         rotas
│   │   ├── analise.py      cruzamentos espaciais no PostGIS
│   │   ├── regras.py       regras de conformidade (EUDR, Código Florestal, RL)
│   │   ├── tiles.py        vector tiles (MVT) direto do banco
│   │   ├── laudo_pdf.py    laudo em PDF com mapa, QR code e código de integridade
│   │   └── arquivos.py     leitura de KML, KMZ, SHP (zip), GeoJSON e GPKG
│   ├── ingest/             carga das bases públicas
│   │   ├── fontes.py       catálogo de fontes e mapeamento de campos
│   │   ├── clientes.py     clientes paginados WFS (GeoServer) e ArcGIS
│   │   ├── carregar.py     carga transacional com registro em geo.cargas
│   │   └── car.py          importador do shapefile do SICAR
│   └── tests/              testes das regras
├── web/index.html          frontend (MapLibre)
├── web/verificar.html      página pública de verificação de laudo (destino do QR code)
├── dados/                  arquivos baixados manualmente (CAR); fora do git
└── prototipo/              protótipo da fase 1 com dados fictícios
```

## Bases públicas

| Fonte (`python -m ingest <id>`) | Origem | Uso |
|---|---|---|
| `prodes_cerrado` | INPE / TerraBrasilis (WFS) | desmatamento anual, com data da imagem |
| `prodes_cerrado_2000` | INPE / TerraBrasilis (WFS) | desmatamento acumulado até 2000 |
| `embargos_ibama` | IBAMA (ArcGIS) | áreas embargadas |
| `ucs_cerrado_inpe` | INPE / TerraBrasilis (WFS) | Unidades de Conservação no Cerrado |
| `ucs_federais_icmbio` | ICMBio via IBAMA (ArcGIS) | UCs federais |
| `ucs_estaduais_sfb` | SFB via IBAMA (ArcGIS) | UCs estaduais |
| `tis_funai` | FUNAI via IBAMA (ArcGIS) | Terras Indígenas |
| `sigef_privado` | INCRA via IBAMA (ArcGIS) | imóveis certificados |
| CAR (manual) | SICAR | imóveis e reserva legal declarada |

Cada carga fica registrada em `geo.cargas` com data, origem e número de registros. O laudo lista essas datas.

Carga de 17/11/2026, na região padrão (LEM e entorno): 21.304 polígonos PRODES anuais e 1.643 do acumulado até 2000,
165 embargos, 3.683 imóveis SIGEF e 5 UCs (todas vindas da base do INPE). As camadas de UC do ICMBio e do SFB e a de
Terras Indígenas não têm feições na região. A carga completa leva cerca de 2 minutos.

**LGPD:** o nome e o CPF/CNPJ dos embargados não são lidos nem guardados.

## Regras de conformidade (`api/app/regras.py`)

| Verificação | Resultado |
|---|---|
| Desmatamento com imagem após 31/12/2020 | **Não conforme** (data de corte do EUDR) |
| Desmatamento entre 22/07/2008 e 31/12/2020 | **Atenção** (marco do Código Florestal) |
| Supressão até 22/07/2008 | Informativo (área consolidada) |
| Embargo IBAMA | **Não conforme** |
| UC de Proteção Integral / Terra Indígena | **Não conforme** |
| UC de Uso Sustentável | **Atenção** |
| Reserva Legal abaixo de 20% | **Atenção**, com o déficit em ha |
| Base não carregada | **Atenção**: o sistema nunca diz "Conforme" sem ter consultado a base |

O ano PRODES vai de agosto a julho, então o sistema usa a **data da imagem** quando ela existe.
Sobreposições menores que `AREA_MINIMA_HA` (padrão 0,1 ha) são ignoradas, porque costumam ser ruído de borda entre bases.

## Como rodar

Requisito: [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado e aberto.

```bash
cd geolem
cp .env.example .env            # e troque a senha
docker compose up -d --build    # sobe o PostGIS e a API
docker compose exec api python -m ingest todas    # carrega as bases públicas (alguns minutos)
```

Abra http://localhost:8000. A documentação da API fica em http://localhost:8000/docs.

### CAR (manual)
O SICAR não oferece serviço aberto para a Bahia. Baixe em https://consultapublica.car.gov.br/publico/estados/downloads
as camadas `AREA_IMOVEL` e `RESERVA_LEGAL` do município, salve em `geolem/dados/` e rode:

```bash
docker compose exec api python -m ingest.car dados/AREA_IMOVEL.zip --reserva-legal dados/RESERVA_LEGAL.zip --fonte car_ba_lem
```

### Laudo em PDF e verificação
O PDF é gerado no servidor sempre a partir da análise gravada, nunca de dados enviados pelo navegador. Ele traz o mapa
da área com as sobreposições, as verificações, as bases com data de atualização, os critérios e um QR code.

O QR code leva a `/verificar/{protocolo}`, uma página pública feita para celular que mostra os dados do laudo.
Cada análise grava um **código de integridade (SHA-256)** do seu conteúdo, impresso no PDF. A verificação recalcula
esse código, e se alguém alterar o laudo no banco a página mostra "Conteúdo divergente".

Em produção, defina `URL_PUBLICA` no `.env` com o domínio real (ex.: `https://laudos.seudominio.com.br`).
Com `localhost`, o QR code só abre no próprio computador.

### Testes
```bash
docker compose exec api python -m pytest -q
```

### Atualizar bases
Rode `python -m ingest todas` de novo. Cada fonte é substituída numa transação, então uma falha não apaga a carga anterior.
Em produção, isso vira uma tarefa agendada semanal.

## API

| Método | Rota | Para quê |
|---|---|---|
| GET | `/api/bases` | bases carregadas e datas |
| GET | `/api/imoveis?q=` | busca imóvel por nome ou código |
| GET | `/api/imoveis/no-ponto?lon=&lat=` | imóveis num ponto |
| GET | `/api/imoveis/{id}` | geometria do imóvel (GeoJSON) |
| POST | `/api/analises` | analisa uma área (`geometria` e/ou `imovel_id`, `reserva_legal_ha`) |
| GET | `/api/analises/{protocolo}` | confere um laudo emitido (inclui `integridade_conferida`) |
| GET | `/api/analises/{protocolo}/laudo.pdf` | laudo em PDF (`?baixar=true` força o download) |
| GET | `/verificar/{protocolo}` | página pública de verificação (destino do QR code) |
| POST | `/api/arquivos/geometria` | converte KML/KMZ/SHP/GeoJSON/GPKG em GeoJSON |
| GET | `/api/tiles/{camada}/{z}/{x}/{y}.pbf` | vector tiles para o mapa |

## Roteiro
- **Fase 2 (esta):** bases reais, PostGIS, API, importação de arquivos, laudo com protocolo
- **Feito na fase 2:** laudo em PDF gerado no servidor, com QR code e código de integridade
- **Próximos passos da fase 2:** DETER Cerrado (alertas recentes), MapBiomas Alerta, APP por hidrografia
- **Fase 3 (produto):** login, empresas e carteiras de fornecedores, monitoramento contínuo com alertas, integração com ERPs do agro (Siagri, TOTVS, SAP), frontend em React

## Modelo de negócio (hipótese)
- Laudo avulso por imóvel
- Assinatura mensal por carteira de áreas monitoradas (armazéns, revendas, tradings)
- Consultoria e implantação com parceiros (agrimensores, consultorias ambientais, agrônomos)
