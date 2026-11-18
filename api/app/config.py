import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://geolem:geolem@localhost:5432/geolem")

# Região coberta pelas cargas: lon_min, lat_min, lon_max, lat_max
REGIAO_BBOX = tuple(float(v) for v in os.getenv("REGIAO_BBOX", "-46.7,-12.9,-45.0,-11.3").split(","))

# Sobreposições menores que isto são ignoradas (ruído de borda entre bases)
AREA_MINIMA_HA = float(os.getenv("AREA_MINIMA_HA", "0.1"))

# Limite de tamanho da área analisada, para proteger o banco
AREA_MAXIMA_HA = float(os.getenv("AREA_MAXIMA_HA", "200000"))

# Endereço público do sistema: vai no QR code do laudo. Em produção, o domínio real (https://...).
URL_PUBLICA = os.getenv("URL_PUBLICA", "http://localhost:8000").rstrip("/")

# Imagens de satélite de antes e depois no laudo (precisa de internet no servidor). 0 desliga.
IMAGENS_SATELITE = os.getenv("IMAGENS_SATELITE", "1") == "1"

WEB_DIR = os.getenv("WEB_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "web"))

TAMANHO_MAX_ARQUIVO = 20 * 1024 * 1024  # 20 MB
