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
├── docker-compose.yml      PostGIS + API (que também serve o frontend) + worker da carteira
├── docker-compose.prod.yml produção: Caddy com HTTPS automático
├── deploy/                 Caddyfile e scripts de instalação, envio e backup
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
│   │   ├── contas.py       empresas, usuários, senhas, sessões e auditoria (+ linha de comando)
│   │   ├── carteira.py     carteira de fornecedores, importação em lote e alertas
│   │   ├── worker.py       fila de análises e monitoramento semanal
│   │   ├── avisos.py       resumo de alertas por e-mail
│   │   ├── planilha.py     leitura da planilha da carteira (CSV/Excel)
│   │   ├── licencas.py     planos, validade e limites de cada empresa
│   │   ├── painel.py       dashboard da carteira (evolução, motivos, municípios, mapa)
│   │   ├── observatorio.py números públicos da região e relatório por município
│   │   ├── rotas_publico.py observatório: consulta grátis, relatório e boletim (captura de leads)
│   │   ├── rotas_admin.py  admin da plataforma: CRM, clientes, licenças e leads
│   │   └── arquivos.py     leitura de KML, KMZ, SHP (zip), GeoJSON e GPKG
│   ├── ingest/             carga das bases públicas
│   │   ├── fontes.py       catálogo de fontes e mapeamento de campos
│   │   ├── clientes.py     clientes paginados WFS (GeoServer) e ArcGIS
│   │   ├── carregar.py     carga transacional com registro em geo.cargas
│   │   └── car.py          importador do shapefile do SICAR
│   └── tests/              testes das regras
├── web/                    frontend: painel, carteira, análise, laudos, equipe, admin, observatório, login e documentos
│   └── verificar.html      página pública de verificação de laudo (destino do QR code)
├── dados/                  arquivos baixados manualmente (CAR); fora do git
├── vendas/                 apresentação: laudo de exemplo e roteiro de reunião
└── prototipo/              protótipo da fase 1 com dados fictícios
```

## Bases públicas

| Fonte (`python -m ingest <id>`) | Origem | Uso |
|---|---|---|
| `prodes_cerrado` | INPE / TerraBrasilis (WFS) | desmatamento anual, com data da imagem |
| `prodes_cerrado_2000` | INPE / TerraBrasilis (WFS) | desmatamento acumulado até 2000 |
| `deter_cerrado` | INPE / TerraBrasilis (WFS) | alertas rápidos de desmatamento (atualização semanal) |
| `mapbiomas_alerta` | MapBiomas Alerta (API, conta gratuita) | alertas de desmatamento validados (opcional) |
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
3.592 alertas DETER (2018 a 2026), 165 embargos, 3.683 imóveis SIGEF, 5 UCs (todas vindas da base do INPE) e 9 assentamentos do INCRA. As camadas de UC
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
| Alerta MapBiomas (validado) detectado após 31/12/2020 | **Não conforme** |
| Alerta DETER detectado após 31/12/2020 | **Atenção** (alerta rápido, ainda sem confirmação) |
| Embargo IBAMA | **Não conforme** |
| UC de Proteção Integral / Terra Indígena | **Não conforme** |
| UC de Uso Sustentável | **Atenção** |
| Território quilombola reconhecido (portaria, decreto ou título) | **Não conforme** |
| Território quilombola em estudo (RTID) | **Atenção** |
| Assentamento da reforma agrária | **Atenção** |
| Reserva Legal abaixo de 20% | **Atenção**, com o déficit em ha |
| Elegibilidade RenovaBio | **Inelegível** se houver supressão de vegetação nativa após 27/11/2018 (fora do resultado geral) |
| Base não carregada | **Atenção**: o sistema nunca diz "Conforme" sem ter consultado a base |

O ano PRODES vai de agosto a julho, então o sistema usa a **data da imagem** quando ela existe.

**RenovaBio:** a Resolução ANP 984/2025 (que substituiu a 758/2018) só aceita como elegível a biomassa de área sem
supressão de vegetação nativa desde a vigência da 758/2018 (27/11/2018) e com CAR ativo ou pendente. O laudo mostra
essa elegibilidade à parte, sem mudar o resultado geral: uma área desmatada em 2019 pode passar no EUDR (corte em
2020) e ainda assim não servir para gerar CBIOs. Uniões das sobreposições de PRODES, DETER e MapBiomas posteriores ao
corte são calculadas no PostGIS para não contar a mesma área duas vezes. Interessa a usinas de etanol de milho e de
cana e a produtores de biodiesel que compram grão da região.
O PRODES é anual e sai com atraso; o **DETER** cobre os meses que o PRODES ainda não publicou (um desmatamento de
março aparece no laudo na mesma semana em que o INPE publica o alerta).

**MapBiomas Alerta (opcional):** crie uma conta gratuita em https://plataforma.alerta.mapbiomas.org e rode
`bash deploy/configurar-mapbiomas.sh` (pede o e-mail e a senha sem mostrar na tela e grava no `.env`); depois
`docker compose up -d api worker` e `docker compose exec api python -m ingest mapbiomas_alerta`. Carga de 17/11/2026:
1.792 alertas validados na região (2019 a junho de 2026), em cerca de 1,5 minuto. A carga `todas` passa a incluir a fonte, e a verificação aparece no
laudo. Sem a conta, essa verificação não aparece (o laudo não marca "Atenção" por uma base opcional).
Sobreposições menores que `AREA_MINIMA_HA` (padrão 0,1 ha) são ignoradas, porque costumam ser ruído de borda entre bases.

## Como rodar

Requisito: [Docker Desktop](https://www.docker.com/products/docker-desktop/) instalado e aberto.

```bash
cd geolem
cp .env.example .env            # e troque a senha
docker compose up -d --build    # sobe o PostGIS e a API
docker compose exec api python -m ingest todas    # carrega as bases públicas (alguns minutos)
```

Crie a primeira empresa e o seu usuário (a senha provisória aparece uma vez; você cria a sua no primeiro acesso):

```bash
docker compose exec api python -m app.contas nova-empresa "Minha Empresa" --admin-nome "Seu Nome" --admin-email voce@exemplo.com.br
```

Abra http://localhost:8000. A documentação da API fica em http://localhost:8000/docs (com login).

### Contas e acesso
- Cada cliente é uma **empresa**, com usuários **administradores** (gerenciam a equipe e veem o registro de atividades)
  e **analistas**. Cada empresa vê só a própria carteira e os próprios laudos.
- Senhas com scrypt; senha provisória com troca obrigatória no primeiro acesso; sessão expira após 12 h sem uso
  (e no máximo em 7 dias); limite de tentativas de login por IP e por e-mail; proteção contra CSRF.
- **Registro de atividades** (`app.auditoria`): logins (inclusive os recusados), laudos, carteira e mudanças de acesso,
  com data, usuário e IP. Aparece na página Equipe.
- Públicos sem login: a tela de entrada, os termos, a privacidade e a verificação de laudos pelo QR code.
- Linha de comando: `python -m app.contas nova-empresa [--operadora] | novo-usuario | redefinir-senha | superadmin | listar`.

### Painel da carteira (`/painel.html`)
Mapa dos fornecedores colorido pela situação (clique abre o laudo), motivos das ocorrências, evolução semanal
(reconstruída a partir do histórico de laudos), concentração por município e por fornecedor e idade da última análise.
É a tela inicial depois do login.

### Observatório público e leads (`/observatorio.html`)
Página sem login com números agregados da região (nunca de um imóvel identificado): % de imóveis SIGEF com
desmatamento PRODES após 2020, desmatamento por ano, alertas DETER por mês e ranking de municípios. Três formas de
captar contato, todas com consentimento registrado (texto, data e IP):
- **Consulta grátis:** resultado resumido de um imóvel pelo código SIGEF/CAR (sem PDF nem protocolo). Limite de
  5 consultas por IP e 3 por e-mail por dia.
- **Relatório do município:** PDF de uma página com os números do município.
- **Boletim semanal:** e-mail toda segunda com os alertas DETER publicados no município, com link de descadastro.
  Enviado por `python -m app.worker boletim` (no cron, depois da carga) quando o SMTP estiver configurado.

### Admin da plataforma (`/admin.html`, só para você)
- **Visão geral:** receita mensal (MRR), clientes ativos e em teste, licenças vencendo, valor em negociação, leads e uso.
- **Possíveis clientes (CRM):** funil por etapa (novo → contato → reunião → piloto → proposta → cliente/perdido),
  próximo passo com data, histórico de contatos e importação de planilha. Um botão cria a conta piloto do prospect.
- **Clientes e licenças:** plano, situação (teste, ativa, suspensa, cancelada), validade, limites (áreas, usuários,
  laudos avulsos por mês) e valor mensal. Licença suspensa ou vencida bloqueia o login na hora; o limite atingido
  aparece para o cliente com a mensagem para falar com a GeoLEM.
- **Leads do site:** quem consultou o quê, com botão para levar ao CRM e exclusão a pedido do titular (LGPD).

Planos sugeridos (`api/app/licencas.py`, limites editáveis empresa a empresa): Piloto (30 dias, 20 áreas,
3 usuários), Essencial (100 áreas), Profissional (500 áreas, 10 usuários) e Corporativo (sem limites).
Para dar acesso ao admin: `python -m app.contas superadmin seu@email.com.br` (a instalação já faz isso com a sua conta).

### Carteira e monitoramento
- **Importar em lote** pela página Carteira: planilha CSV/Excel com o código SIGEF ou CAR (colunas `codigo`, `nome`,
  `fornecedor`, `codigo_erp`, `reserva_legal_ha`), ou arquivo geográfico em que cada polígono vira uma área. Linhas com
  problema são listadas com o motivo, sem travar o resto.
- As análises entram numa **fila** processada pelo serviço `worker` (cerca de 5 s por área, com as imagens de satélite).
- **Monitoramento:** toda segunda, depois da carga das bases, `python -m app.worker monitorar` reanalisa todas as áreas.
  Se a situação piorar (ex.: Conforme → Não conforme), nasce um **alerta** na carteira e, com `SMTP_*` configurado no
  `.env`, os administradores recebem um resumo por e-mail quando a fila da empresa termina.
- Exportações: planilha da carteira (abre no Excel) e um GeoJSON do EUDR com todas as áreas Conformes.

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

O sistema roda num servidor Linux com Docker. O Caddy emite o certificado HTTPS sozinho. Todo o sistema
pede login, **menos a verificação de laudos** (`/verificar/...` e a leitura de um protocolo), que precisa ser pública
para o QR code funcionar no celular de quem recebe o laudo. O banco e a API não ficam expostos à internet.

**O que contratar:**
- Um servidor (VPS) com **Ubuntu 24.04, 2 GB de RAM e 25 GB de disco**. Prefira um com datacenter em São Paulo.
- Um **domínio** (por exemplo, no registro.br). Crie um registro **A** apontando o domínio, ou um subdomínio como
  `laudos.seudominio.com.br`, para o IP do servidor.

**Primeira instalação** (no seu computador, no Git Bash, dentro da pasta `geolem`):
```bash
bash deploy/enviar.sh root@IP_DO_SERVIDOR
ssh root@IP_DO_SERVIDOR "bash /opt/geolem/deploy/instalar.sh laudos.seudominio.com.br voce@seuemail.com.br 'Seu Nome'"
```
O `instalar.sh` instala o Docker, liga o firewall (só SSH, HTTP e HTTPS), gera a senha do banco no `.env`, sobe o
sistema, carrega as bases, cria a sua conta de administrador e agenda as tarefas.
**Ele mostra a sua senha provisória uma única vez: anote.** Para cada cliente, crie a empresa e o primeiro
administrador com `python -m app.contas nova-empresa` (o comando completo aparece no fim da instalação).

**Atualizar** depois de mudar o código: faça o commit e rode de novo `bash deploy/enviar.sh root@IP_DO_SERVIDOR`.
Só vai o que está no git; o `.env`, o banco e os backups do servidor não são tocados. Tabelas novas do
`db/init/01_schema.sql` são criadas automaticamente (o script só cria o que ainda não existe).

**Tarefas agendadas** (`/etc/cron.d/geolem`): bases públicas atualizadas toda segunda às 3h, seguidas do
monitoramento de todas as carteiras e do boletim do DETER (log em `/var/log/geolem-bases.log`), e backup do banco
todo dia às 2h.

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
| GET | `/api/analises` | laudos emitidos pela empresa |
| POST | `/api/auth/login` · `/api/auth/sair` · `/api/auth/senha` | entrar, sair e trocar a senha |
| GET/POST/PATCH | `/api/equipe` | usuários da empresa (administradores) |
| GET | `/api/auditoria` | registro de atividades (administradores) |
| GET | `/api/carteira` | resumo e áreas da carteira |
| POST | `/api/carteira/importar` | importação em lote (planilha ou arquivo geográfico) |
| POST/DELETE | `/api/carteira/areas` | adicionar ou remover uma área |
| POST | `/api/carteira/reanalisar` | pôr áreas na fila de novo |
| GET | `/api/carteira/alertas` | alertas de mudança de situação |
| GET | `/api/carteira/exportar.csv` · `/api/carteira/eudr.geojson` | exportações da carteira |
| GET | `/verificar/{protocolo}` | página pública de verificação (destino do QR code) |
| POST | `/api/arquivos/geometria` | converte KML/KMZ/SHP/GeoJSON/GPKG em GeoJSON |
| GET | `/api/tiles/{camada}/{z}/{x}/{y}.pbf` | vector tiles para o mapa |

## Roteiro
- **Fase 2 (esta):** bases reais, PostGIS, API, importação de arquivos, laudo com protocolo
- **Feito na fase 2:** laudo em PDF gerado no servidor, com QR code e código de integridade; imagens de satélite de
  antes e depois; territórios quilombolas e assentamentos; exportação GeoJSON para o EUDR
- **Feito:** contas por empresa com auditoria; carteira com importação em lote, monitoramento semanal e alertas
- **Feito:** alertas recentes do DETER Cerrado e integração com o MapBiomas Alerta
- **Feito:** painel da carteira, observatório público com captura de leads, admin com CRM, clientes e licenças
- **Próximos passos:** APP por hidrografia; gráfico de NDVI no laudo;
  integração com ERPs
- **Fase 3 (produto):** login, empresas e carteiras de fornecedores, monitoramento contínuo com alertas, integração com ERPs do agro (Siagri, TOTVS, SAP), frontend em React

## Modelo de negócio (hipótese)
- Laudo avulso por imóvel
- Assinatura mensal por carteira de áreas monitoradas (armazéns, revendas, tradings)
- Consultoria e implantação com parceiros (agrimensores, consultorias ambientais, agrônomos)
