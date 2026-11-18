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
├── docker-compose.prod.yml produção: Caddy com HTTPS automático e senha de acesso
├── deploy/                 Caddyfile e scripts de instalação, envio, backup e troca de senha
├── .env.example            configurações (copie para .env)
├── db/init/01_schema.sql   tabelas, índices espaciais e função de normalização
├── api/
│   ├── app/                API FastAPI
│   │   ├── main.py         rotas
│   │   ├── analise.py      cruzamentos espaciais no PostGIS
│   │   ├── regras.py       regras de conformidade (EUDR, Código Florestal, RL)
│   │   ├── tiles.py        vector tiles (MVT) direto do banco
│   │   ├── laudo_pdf.py    laudo em PDF com mapa, QR code e código de integridade
│   │   ├── imagens.py      imagens Sentinel-2 de antes e depois da área
│   │   ├── eudr.py         exportação GeoJSON para a declaração do EUDR
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
├── vendas/                 apresentação: laudo de exemplo e roteiro de reunião
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
| `quilombolas_incra` | INCRA via IBAMA (ArcGIS) | territórios quilombolas |
| `assentamentos_incra` | INCRA via IBAMA (ArcGIS) | assentamentos da reforma agrária |
| `sigef_privado` | INCRA via IBAMA (ArcGIS) | imóveis certificados |
| CAR (manual) | SICAR | imóveis e reserva legal declarada |

Cada carga fica registrada em `geo.cargas` com data, origem e número de registros. O laudo lista essas datas.

Carga de 17/11/2026, na região padrão (LEM e entorno): 21.304 polígonos PRODES anuais e 1.643 do acumulado até 2000,
165 embargos, 3.683 imóveis SIGEF, 5 UCs (todas vindas da base do INPE) e 9 assentamentos do INCRA. As camadas de UC
do ICMBio e do SFB, a de Terras Indígenas e a de territórios quilombolas não têm feições na região padrão (o quilombo
mais próximo, Lajeado, fica em Dianópolis/TO). A carga completa leva cerca de 2 minutos.

Algumas camadas do INCRA chegam com letras acentuadas corrompidas na origem; a carga corrige isso
(`_reparar_acentos` em `ingest/fontes.py`).

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
| Território quilombola reconhecido (portaria, decreto ou título) | **Não conforme** |
| Território quilombola em estudo (RTID) | **Atenção** |
| Assentamento da reforma agrária | **Atenção** |
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

### Imagens de satélite: antes e depois
Cada análise busca duas imagens Sentinel-2 da área (10 m, cor natural):
- **Antes:** a melhor cena do período seco de 2020 (junho a setembro), anterior à data de corte do EUDR.
- **Depois:** a melhor cena do período seco mais recente.

As cenas vêm do catálogo público do Microsoft Planetary Computer, sem custo e sem chave. Os dados Copernicus permitem
uso comercial com atribuição, que o laudo imprime. Cada imagem é montada com cenas de uma única passagem do satélite,
então tem uma data só. O PNG fica gravado em `geo.analises_imagens`, e o SHA-256 dele entra no código de integridade:
o laudo de amanhã mostra exatamente a mesma imagem. A busca leva de 3 a 6 segundos. Se o serviço estiver fora do ar,
a análise sai normalmente e o laudo informa que a imagem está indisponível. Para desligar, use `IMAGENS_SATELITE=0`.

### Exportação para o EUDR
`GET /api/analises/{protocolo}/eudr.geojson` devolve a geolocalização da área no formato GeoJSON usado na declaração
de diligência do sistema TRACES da União Europeia: WGS84, 6 casas decimais, com as propriedades `ProductionPlace`,
`ProducerCountry` e `Area` (ha). No painel, é o botão "Geolocalização para o EUDR".
Antes do primeiro uso real, confira com o importador se o sistema dele exige alguma propriedade a mais.

### Testes
```bash
docker compose exec api python -m pytest -q
```

### Atualizar bases
Rode `python -m ingest todas` de novo. Cada fonte é substituída numa transação, então uma falha não apaga a carga anterior.
Em produção, isso vira uma tarefa agendada semanal.

## Colocar no ar (servidor)

O sistema roda num servidor Linux com Docker. O Caddy emite o certificado HTTPS sozinho e pede usuário e senha
para tudo, **menos a verificação de laudos** (`/verificar/...` e a leitura de um protocolo), que precisa ser pública
para o QR code funcionar no celular de quem recebe o laudo. O banco e a API não ficam expostos à internet.

**O que contratar:**
- Um servidor (VPS) com **Ubuntu 24.04, 2 GB de RAM e 25 GB de disco**. Prefira um com datacenter em São Paulo.
- Um **domínio** (por exemplo, no registro.br). Crie um registro **A** apontando o domínio, ou um subdomínio como
  `laudos.seudominio.com.br`, para o IP do servidor.

**Primeira instalação** (no seu computador, no Git Bash, dentro da pasta `geolem`):
```bash
bash deploy/enviar.sh root@IP_DO_SERVIDOR
ssh root@IP_DO_SERVIDOR "bash /opt/geolem/deploy/instalar.sh laudos.seudominio.com.br"
```
O `instalar.sh` instala o Docker, liga o firewall (só SSH, HTTP e HTTPS), gera senhas fortes no `.env`, sobe o sistema,
carrega as bases e agenda as tarefas. **Ele mostra a senha de acesso uma única vez: anote.**
O usuário é `geolem`; para trocar a senha, rode `bash /opt/geolem/deploy/trocar-senha.sh` no servidor.

**Atualizar** depois de mudar o código: faça o commit e rode de novo `bash deploy/enviar.sh root@IP_DO_SERVIDOR`.
Só vai o que está no git; o `.env`, o banco e os backups do servidor não são tocados. Tabelas novas do
`db/init/01_schema.sql` são criadas automaticamente (o script só cria o que ainda não existe).

**Tarefas agendadas** (`/etc/cron.d/geolem`): bases públicas atualizadas toda segunda às 3h
(log em `/var/log/geolem-bases.log`) e backup do banco todo dia às 2h.

### Backups
Ficam em `/opt/geolem/backups`, com 14 dias guardados (cerca de 8 MB cada). Os laudos emitidos são o que não dá para
refazer, então copie de vez em quando para fora do servidor:
```bash
scp "root@IP_DO_SERVIDOR:/opt/geolem/backups/*.sql.gz" .
```
Para restaurar num banco vazio:
`gunzip -c ARQUIVO.sql.gz | docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db psql -U geolem -d geolem`.

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
| GET | `/api/analises/{protocolo}/imagens/{antes\|depois}.png` | imagens de satélite gravadas com a análise |
| GET | `/api/analises/{protocolo}/eudr.geojson` | geolocalização no formato da declaração do EUDR |
| GET | `/verificar/{protocolo}` | página pública de verificação (destino do QR code) |
| POST | `/api/arquivos/geometria` | converte KML/KMZ/SHP/GeoJSON/GPKG em GeoJSON |
| GET | `/api/tiles/{camada}/{z}/{x}/{y}.pbf` | vector tiles para o mapa |

## Roteiro
- **Fase 2 (esta):** bases reais, PostGIS, API, importação de arquivos, laudo com protocolo
- **Feito na fase 2:** laudo em PDF gerado no servidor, com QR code e código de integridade; imagens de satélite de
  antes e depois; territórios quilombolas e assentamentos; exportação GeoJSON para o EUDR
- **Próximos passos:** análise em lote e monitoramento da carteira com alertas; MapBiomas Alerta, DETER Cerrado e APP
  por hidrografia
- **Fase 3 (produto):** login, empresas e carteiras de fornecedores, monitoramento contínuo com alertas, integração com ERPs do agro (Siagri, TOTVS, SAP), frontend em React

## Modelo de negócio (hipótese)
- Laudo avulso por imóvel
- Assinatura mensal por carteira de áreas monitoradas (armazéns, revendas, tradings)
- Consultoria e implantação com parceiros (agrimensores, consultorias ambientais, agrônomos)
