import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

CACHE_ARQUIVO = "cache_empresas.json"
CACHE_VALIDADE_DIAS = 30


# ─── Cache simples em arquivo JSON (zero custo, zero infra extra) ───────────
def ler_cache() -> dict:
    if os.path.exists(CACHE_ARQUIVO):
        try:
            with open(CACHE_ARQUIVO, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def gravar_cache(dados: dict):
    with open(CACHE_ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)


def chave_cache(texto: str) -> str:
    return re.sub(r'\D', '', texto) if eh_cnpj(texto) else normalizar_texto(texto)


def buscar_no_cache(chave: str) -> dict:
    cache = ler_cache()
    entrada = cache.get(chave)
    if not entrada:
        return None
    idade_dias = (time.time() - entrada.get("timestamp", 0)) / 86400
    if idade_dias > CACHE_VALIDADE_DIAS:
        return None
    return entrada.get("dados")


def salvar_no_cache(chave: str, dados: dict):
    cache = ler_cache()
    cache[chave] = {"timestamp": time.time(), "dados": dados}
    gravar_cache(cache)


# ─── Utilitários ──────────────────────────────────────────────────────────
def limpar_cnpj(cnpj: str) -> str:
    return re.sub(r'\D', '', cnpj)


def eh_cnpj(texto: str) -> bool:
    return len(limpar_cnpj(texto)) == 14


def normalizar_texto(texto: str) -> str:
    import unicodedata
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]', '', texto.lower())


def normalizar_telefone(tel: str) -> str:
    return re.sub(r'\D', '', tel)


def formatar_telefone(ddd: str, numero: str) -> str:
    if not ddd or not numero:
        return None
    if len(numero) == 9:
        return f"({ddd}) {numero[:5]}-{numero[5:]}"
    return f"({ddd}) {numero[:4]}-{numero[4:]}"


# ─── Fonte 1: BrasilAPI (oficial, gratuita, sem chave, sem limite agressivo) ─
def buscar_brasilapi(cnpj: str) -> dict:
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15
        )
        if r.status_code != 200:
            return {}
        data = r.json()

        telefone = None
        if data.get("ddd_telefone_1"):
            tel = data["ddd_telefone_1"]
            telefone = tel if "(" in tel else f"({tel[:2]}) {tel[2:]}"

        return {
            "razao_social": data.get("razao_social"),
            "nome_fantasia": data.get("nome_fantasia") or data.get("razao_social"),
            "telefone": telefone,
            "email": data.get("email"),
            "situacao": data.get("descricao_situacao_cadastral"),
            "endereco": f"{data.get('logradouro', '')}, {data.get('municipio', '')} - {data.get('uf', '')}",
            "socios": [s.get("nome_socio") for s in data.get("qsa", []) if s.get("nome_socio")]
        }
    except Exception:
        return {}


# ─── Fonte 2: scraping direto do site institucional, sem API paga ──────────
def gerar_variacoes_slug(empresa: str) -> list:
    """Gera várias hipóteses de slug a partir do nome da empresa,
    incluindo só a primeira palavra (caso comum: 'Grunox Equipamentos' -> 'grunox')"""
    palavras = re.sub(r'[^a-zA-Z0-9\s]', '', empresa).split()
    palavras_uteis = [p for p in palavras if normalizar_texto(p) not in
                      ['ltda', 'sa', 'eireli', 'me', 'epp', 'equipamentos', 'comercio',
                       'industria', 'servicos', 'solucoes', 'grupo', 'brasil']]

    slugs = []
    if palavras_uteis:
        slugs.append(normalizar_texto(palavras_uteis[0]))  # só a primeira palavra relevante
    if len(palavras_uteis) >= 2:
        slugs.append(normalizar_texto(palavras_uteis[0] + palavras_uteis[1]))  # duas primeiras
    slugs.append(normalizar_texto(empresa))  # nome completo, por garantia

    return list(dict.fromkeys(slugs))  # remove duplicatas mantendo ordem


def descobrir_site_tentativa_direta(empresa: str) -> str:
    """Tenta adivinhar o domínio testando várias hipóteses de slug, sem custo."""
    for slug in gerar_variacoes_slug(empresa):
        if len(slug) < 3:
            continue
        candidatos = [
            f"https://www.{slug}.com.br",
            f"https://{slug}.com.br",
            f"https://www.{slug}.com",
            f"https://{slug}.com",
        ]
        for url in candidatos:
            try:
                r = requests.head(url, timeout=5, allow_redirects=True)
                if r.status_code < 400:
                    return url
            except Exception:
                continue
    return None


def descobrir_site_via_duckduckgo(empresa: str) -> str:
    """Fallback gratuito: busca o domínio via DuckDuckGo HTML (sem JS, sem chave, sem custo).
    Usado só quando a tentativa direta de adivinhar o slug falha."""
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{empresa} site oficial"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10
        )
        if r.status_code != 200:
            return None

        links = re.findall(r'href="(https?://[^"]+)"', r.text)
        bloqueados = ["duckduckgo.com", "linkedin.com", "facebook.com", "instagram.com",
                      "youtube.com", "indeed.com", "glassdoor", "wikipedia.org",
                      "google.com", "econodata", "cnpj", "consultas.plus", "datanyze"]

        empresa_slug = normalizar_texto(empresa)
        primeira_palavra = normalizar_texto(empresa.split()[0]) if empresa.split() else ""

        for link in links:
            link_norm = normalizar_texto(link)
            if any(b in link.lower() for b in bloqueados):
                continue
            if primeira_palavra and len(primeira_palavra) >= 4 and primeira_palavra in link_norm:
                dominio = re.match(r'https?://(?:www\.)?([^/]+)', link)
                if dominio:
                    return f"https://{dominio.group(1)}"
        return None
    except Exception:
        return None


def descobrir_site_por_busca_simples(empresa: str) -> str:
    """Combina tentativa direta (rápida, sem custo) com fallback via DuckDuckGo
    (também sem custo, mas mais lento) quando a primeira falha."""
    site = descobrir_site_tentativa_direta(empresa)
    if site:
        return site
    return descobrir_site_via_duckduckgo(empresa)


PREFIXOS_DEPARTAMENTO = {
    "financeiro": ["financeiro", "contas", "cobranca", "billing", "faturamento"],
    "rh": ["rh", "recursoshumanos", "recrutamento", "vagas", "talentos", "people"],
    "compras": ["compras", "suprimentos", "procurement", "fornecedores"],
    "comercial": ["comercial", "vendas", "sales", "atendimento", "contato"],
}


def classificar_email_por_departamento(email: str) -> str:
    usuario = email.split("@")[0].lower()
    for depto, prefixos in PREFIXOS_DEPARTAMENTO.items():
        if any(p == usuario or usuario.startswith(p + ".") or usuario.startswith(p + "-") for p in prefixos):
            return depto
    return "geral"


def sugerir_emails_departamentais(dominio: str, emails_confirmados: list) -> list:
    """Gera sugestões de email por departamento baseado no padrão do domínio.
    NÃO são emails confirmados — são palpites de formato, marcados como tal."""
    if not dominio:
        return []
    dominio_limpo = re.sub(r'https?://(www\.)?', '', dominio).rstrip('/')
    confirmados_normalizados = [e.split("@")[0].lower() for e in emails_confirmados]

    sugestoes = []
    principais = {"financeiro": "financeiro", "rh": "rh", "compras": "compras"}
    for depto, prefixo in principais.items():
        candidato = f"{prefixo}@{dominio_limpo}"
        if prefixo not in confirmados_normalizados:
            sugestoes.append({"departamento": depto, "email_sugerido": candidato})
    return sugestoes


def extrair_emails_telefones_do_site(url_base: str) -> dict:
    """Visita as páginas mais prováveis de contato e extrai dados com regex,
    sem nenhuma API paga — só requests + regex."""
    paginas = ["", "/contato", "/fale-conosco", "/sobre", "/atendimento", "/contact",
               "/trabalhe-conosco", "/carreiras", "/financeiro", "/fornecedores"]
    emails, telefones = [], []

    for pagina in paginas:
        try:
            r = requests.get(
                url_base.rstrip("/") + pagina,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8
            )
            if r.status_code != 200:
                continue
            texto = r.text

            padrao_email = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
            achados_email = re.findall(padrao_email, texto)
            ignorar = ['png', 'jpg', 'gif', 'sentry', 'wixpress', '.css', '.js', 'example']
            emails += [e.lower() for e in achados_email if not any(i in e.lower() for i in ignorar)]

            padroes_tel = [
                r'\(\d{2}\)\s?\d{4,5}-?\d{4}',
                r'0800\s?\d{3}\s?\d{4}',
            ]
            for p in padroes_tel:
                telefones += re.findall(p, texto)

        except Exception:
            continue

    return {
        "emails": list(dict.fromkeys(emails))[:5],
        "telefones": list(dict.fromkeys(telefones))[:5]
    }


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/buscar", methods=["POST"])
def buscar_lead():
    data = request.json
    entrada = data.get("empresa", "").strip()
    numeros_conhecidos = data.get("numeros_conhecidos", "").strip()

    if not entrada:
        return jsonify({"erro": "Nome, site ou CNPJ é obrigatório"}), 400

    chave = chave_cache(entrada)
    cache_hit = buscar_no_cache(chave)

    if cache_hit:
        resultado = dict(cache_hit)
        resultado["veio_do_cache"] = True
    else:
        try:
            emails_encontrados = []
            telefones_encontrados = []
            site = None
            empresa_nome = entrada
            fonte_receita = False
            socios = []

            if eh_cnpj(entrada):
                dados = buscar_brasilapi(entrada)
                if dados:
                    empresa_nome = dados.get("nome_fantasia") or entrada
                    fonte_receita = True
                    socios = dados.get("socios", [])
                    if dados.get("telefone"):
                        telefones_encontrados.append(dados["telefone"])
                    if dados.get("email"):
                        emails_encontrados.append(dados["email"])

            termo_busca = empresa_nome if empresa_nome != entrada else entrada
            site = descobrir_site_por_busca_simples(termo_busca)

            if site:
                extra = extrair_emails_telefones_do_site(site)
                emails_encontrados += extra["emails"]
                telefones_encontrados += extra["telefones"]

            telefones_encontrados = list(dict.fromkeys(telefones_encontrados))
            emails_encontrados = list(dict.fromkeys(emails_encontrados))

            # Classifica cada email confirmado por departamento
            emails_classificados = [
                {"email": e, "departamento": classificar_email_por_departamento(e)}
                for e in emails_encontrados[:5]
            ]

            # Sugere formatos prováveis para departamentos que não foram confirmados
            sugestoes_departamento = sugerir_emails_departamentais(site, emails_encontrados)

            nao_encontrado = "Não encontrado em fonte pública"

            resultado = {
                "empresa": empresa_nome,
                "site": site or nao_encontrado,
                "telefones": telefones_encontrados[:5],
                "emails": emails_classificados,
                "emails_sugeridos": sugestoes_departamento,
                "socios": socios[:5],
                "fonte_receita_federal": fonte_receita,
                "veio_do_cache": False
            }

            salvar_no_cache(chave, resultado)

        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    # Compara com o que o BDR já tem no HubSpot
    numeros_conhecidos_norm = [normalizar_telefone(n) for n in re.split(r'[,;\n]', numeros_conhecidos) if n.strip()]
    telefones_novos = [t for t in resultado["telefones"] if normalizar_telefone(t) not in numeros_conhecidos_norm]
    telefones_repetidos = [t for t in resultado["telefones"] if normalizar_telefone(t) in numeros_conhecidos_norm]

    resultado["telefones_novos"] = telefones_novos
    resultado["telefones_ja_conhecidos"] = telefones_repetidos

    return jsonify(resultado)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
