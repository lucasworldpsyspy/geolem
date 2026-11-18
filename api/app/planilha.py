"""Planilha de carteira (CSV ou Excel): uma linha por área, identificada pelo código SIGEF ou CAR.

Colunas reconhecidas (sem diferenciar maiúsculas e acentos):
  codigo (obrigatória)  | também: codigo_imovel, sigef, car, cod_imovel
  nome                  | também: fazenda, imovel
  fornecedor            | também: produtor, proprietario
  codigo_externo        | também: codigo_erp, cod_fornecedor
  reserva_legal_ha      | também: reserva_legal, rl_ha
"""
import csv
import io
import os
import unicodedata

SINONIMOS = {
    "codigo": ("codigo", "codigo_imovel", "sigef", "car", "cod_imovel", "codigo_sigef", "codigo_car"),
    "nome": ("nome", "fazenda", "imovel", "nome_imovel"),
    "fornecedor": ("fornecedor", "produtor", "proprietario"),
    "codigo_externo": ("codigo_externo", "codigo_erp", "cod_fornecedor", "codigo_fornecedor"),
    "reserva_legal_ha": ("reserva_legal_ha", "reserva_legal", "rl_ha", "rl"),
    "cultura": ("cultura", "commodity", "produto", "lavoura"),
}
MAX_LINHAS = 5000


class ErroPlanilha(ValueError):
    pass


def _chave(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().strip().lower()
    return "_".join(t.replace("(", " ").replace(")", " ").replace("-", " ").split())


def _linhas(nome_arquivo: str, conteudo: bytes) -> list[list]:
    ext = os.path.splitext(nome_arquivo.lower())[1]
    if ext == ".xlsx":
        from openpyxl import load_workbook
        try:
            ws = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True).active
        except Exception as e:
            raise ErroPlanilha(f"Não foi possível ler o Excel: {e}") from e
        return [list(l) for l in ws.iter_rows(values_only=True)]
    if ext in (".csv", ".txt"):
        for cod in ("utf-8-sig", "cp1252"):  # Excel brasileiro salva CSV em Windows-1252
            try:
                texto = conteudo.decode(cod)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialeto = csv.Sniffer().sniff(texto[:4096], delimiters=";,\t")
        except csv.Error:
            class dialeto(csv.excel):  # noqa: N801 (cópia local; não altera o csv.excel global)
                delimiter = ";" if texto.count(";") > texto.count(",") else ","
        return list(csv.reader(io.StringIO(texto), dialeto))
    raise ErroPlanilha("Envie a planilha em CSV ou Excel (.xlsx).")


def _numero(v) -> float | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(" ", "")
    if "," in t:  # formato brasileiro: 1.234,5
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        raise ErroPlanilha(f"Número inválido: {v}")


def ler_planilha(nome_arquivo: str, conteudo: bytes) -> list[dict]:
    linhas = [l for l in _linhas(nome_arquivo, conteudo) if any(c not in (None, "") for c in l)]
    if len(linhas) < 2:
        raise ErroPlanilha("A planilha está vazia. A primeira linha deve ter os títulos das colunas.")
    if len(linhas) > MAX_LINHAS + 1:
        raise ErroPlanilha(f"Limite de {MAX_LINHAS} linhas por planilha.")
    titulos = [_chave(t or "") for t in linhas[0]]
    indice = {}
    for campo, nomes in SINONIMOS.items():
        for i, t in enumerate(titulos):
            if t in nomes and campo not in indice:
                indice[campo] = i
    if "codigo" not in indice:
        raise ErroPlanilha("A planilha precisa de uma coluna 'codigo' com o código SIGEF ou CAR do imóvel.")

    saida = []
    for n, l in enumerate(linhas[1:], start=2):
        def campo(c):
            i = indice.get(c)
            v = l[i] if i is not None and i < len(l) else None
            return str(v).strip() if v not in (None, "") else None
        item = {"linha": n, "codigo": campo("codigo"), "nome": campo("nome"), "fornecedor": campo("fornecedor"),
                "codigo_externo": campo("codigo_externo"), "cultura": campo("cultura")}
        try:
            item["reserva_legal_ha"] = _numero(campo("reserva_legal_ha"))
        except ErroPlanilha as e:
            item["erro"] = str(e)
        saida.append(item)
    return saida
