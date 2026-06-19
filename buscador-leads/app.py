import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")

GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CACHE_ARQUIVO = "cache_empresas.json"
QUOTA_ARQUIVO = "quota_uso.json"
CACHE_VALIDADE_DIAS = 30

# Tetos com margem de segurança (abaixo do limite real de cada plano)
LIMITES_MES = {
    "serpapi": 90,    # plano free é 100/mês, deixamos 10 de colchão
    "apify": 40,      # dentro dos $5 grátis em créditos
    "groq": 800,      # bem abaixo do limite diário do free tier
}


# ─── Cache de resultado por empresa (evita até precisar gastar qualquer cota) ─
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


# ─── Controle de cota por fonte, persistido em arquivo ──────────────────────
def ler_quotas() -> dict:
    hoje = __import__("datetime").date.today()
    mes_atual = f"{hoje.year}-{hoje.month:02d}"
    if os.path.exists(QUOTA_ARQUIVO):
        with open(QUOTA_ARQUIVO, "r") as f:
            dados = json.load(f)
    else:
        dados = {}
    for fonte in LIMITES_MES:
        if fonte not in dados or dados[fonte].get("mes") != mes_atual:
            dados[fonte] = {"mes": mes_atual, "usos": 0}
    return dados


def gravar_quotas(dados: dict):
    with open(QUOTA_ARQUIVO, "w") as f:
        json.dump(dados, f)


def pode_usar(fonte: str) -> bool:
    quotas = ler_quotas()
    return quotas[fonte]["usos"] < LIMITES_MES[fonte]


def registrar_uso(fonte: str):
    quotas = ler_quotas()
    quotas[fonte]["usos"] += 1
    gravar_quotas(quotas)


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


def chave_cache(texto: str) -> str:
    return limpar_cnpj(texto) if eh_cnpj(texto) else normalizar_texto(texto)


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


# ─── NÍVEL 0 — gratuito e ilimitado: BrasilAPI (Receita Federal) ────────────
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
            "endereco": f"{data.get('logradouro', '')}, {data.get('municipio', '')} - {data.get('uf', '')}",
            "socios": [s.get("nome_socio") for s in data.get("qsa", []) if s.get("nome_socio")]
        }
    except Exception:
        return {}


# ─── NÍVEL 0 — gratuito: tentativa direta de domínio + DuckDuckGo ───────────
def gerar_variacoes_slug(empresa: str) -> list:
    palavras = re.sub(r'[^a-zA-Z0-9\s]', '', empresa).split()
    palavras_uteis = [p for p in palavras if normalizar_texto(p) not in
                      ['ltda', 'sa', 'eireli', 'me', 'epp', 'equipamentos', 'comercio',
                       'industria', 'servicos', 'solucoes', 'grupo', 'brasil']]
    slugs = []
    if palavras_uteis:
        slugs.append(normalizar_texto(palavras_uteis[0]))
    if len(palavras_uteis) >= 2:
        slugs.append(normalizar_texto(palavras_uteis[0] + palavras_uteis[1]))
    slugs.append(normalizar_texto(empresa))
    return list(dict.fromkeys(slugs))


def descobrir_site_tentativa_direta(empresa: str) -> str:
    for slug in gerar_variacoes_slug(empresa):
        if len(slug) < 3:
            continue
        for url in [f"https://www.{slug}.com.br", f"https://{slug}.com.br",
                    f"https://www.{slug}.com", f"https://{slug}.com"]:
            try:
                r = requests.head(url, timeout=5, allow_redirects=True)
                if r.status_code < 400:
                    return url
            except Exception:
                continue
    return None


def descobrir_site_via_duckduckgo(empresa: str) -> str:
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
                      "youtube.com", "indeed.com", "glassdoor", "wikipedia.org", "google.com",
                      "econodata", "cnpj", "consultas.plus", "datanyze"]
        primeira_palavra = normalizar_texto(empresa.split()[0]) if empresa.split() else ""
        for link in links:
            if any(b in link.lower() for b in bloqueados):
                continue
            if primeira_palavra and len(primeira_palavra) >= 4 and primeira_palavra in normalizar_texto(link):
                dominio = re.match(r'https?://(?:www\.)?([^/]+)', link)
                if dominio:
                    return f"https://{dominio.group(1)}"
        return None
    except Exception:
        return None


def descobrir_site(empresa: str) -> str:
    return descobrir_site_tentativa_direta(empresa) or descobrir_site_via_duckduckgo(empresa)


def extrair_emails_telefones_do_site(url_base: str) -> dict:
    paginas = ["", "/contato", "/fale-conosco", "/sobre", "/atendimento", "/contact",
               "/trabalhe-conosco", "/carreiras", "/financeiro", "/fornecedores",
               "/contatos", "/quem-somos", "/institucional"]
    emails, telefones = [], []
    for pagina in paginas:
        try:
            r = requests.get(url_base.rstrip("/") + pagina,
                              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                              timeout=8)
            if r.status_code != 200:
                continue
            texto = r.text
            padrao_email = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
            achados = re.findall(padrao_email, texto)
            ignorar = ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'sentry', 'wixpress',
                       '.css', '.js', 'example', 'schema.org', 'w3.org', 'gravatar']
            emails += [e.lower() for e in achados if not any(i in e.lower() for i in ignorar)]
            for p in [r'\(\d{2}\)\s?\d{4,5}-?\d{4}', r'\+55\s?\d{2}\s?\d{4,5}[-\s]?\d{4}',
                      r'\b\d{2}\s\d{4,5}-?\d{4}\b', r'0800\s?\d{3}\s?\d{4}']:
                telefones += re.findall(p, texto)
            whats = re.findall(r'(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?(\d{10,13})', texto)
            for numero in whats:
                num = numero[-11:] if numero.startswith("55") else numero
                if len(num) >= 10:
                    ddd, resto = num[:2], num[2:]
                    telefones.append(f"({ddd}) {resto[:5]}-{resto[5:]}" if len(resto) == 9 else f"({ddd}) {resto[:4]}-{resto[4:]}")
        except Exception:
            continue
    return {"emails": list(dict.fromkeys(emails))[:5], "telefones": list(dict.fromkeys(telefones))[:5]}


def sugerir_emails_departamentais(dominio: str, emails_confirmados: list) -> list:
    if not dominio:
        return []
    dominio_limpo = re.sub(r'https?://(www\.)?', '', dominio).rstrip('/')
    confirmados_norm = [e.split("@")[0].lower() for e in emails_confirmados]
    sugestoes = []
    for depto, prefixo in {"financeiro": "financeiro", "rh": "rh", "compras": "compras"}.items():
        candidato = f"{prefixo}@{dominio_limpo}"
        if prefixo not in confirmados_norm:
            sugestoes.append({"departamento": depto, "email_sugerido": candidato})
    return sugestoes


# ─── NÍVEL 1 — pago com cota: SerpAPI (busca real no Google) ────────────────
def buscar_serpapi(query: str) -> list:
    try:
        r = requests.get("https://serpapi.com/search", params={
            "q": query, "api_key": SERPAPI_KEY, "engine": "google",
            "num": 5, "hl": "pt", "gl": "br"
        }, timeout=15)
        return r.json().get("organic_results", [])
    except Exception:
        return []


def texto_resultados(resultados: list) -> str:
    return " ".join([(r.get("title", "") + " " + r.get("snippet", "") + " " + r.get("link", "")) for r in resultados])


def extrair_pessoa_linkedin(resultados: list, empresa: str, termos_cargo: list) -> dict:
    for r in resultados:
        link = r.get("link", "")
        if "linkedin.com/in/" not in link:
            continue
        titulo, snippet = r.get("title", ""), r.get("snippet", "")
        texto_completo = f"{titulo} {snippet}"
        if normalizar_texto(empresa) not in normalizar_texto(texto_completo):
            continue
        tem_cargo = any(normalizar_texto(t) in normalizar_texto(texto_completo) for t in termos_cargo)
        if not tem_cargo:
            continue
        nome = titulo.split(" - ")[0].split(" | ")[0].strip()
        cargo_match = next((t for t in termos_cargo if normalizar_texto(t) in normalizar_texto(texto_completo)), None)
        return {"nome_cargo": f"{nome} - {cargo_match}" if cargo_match else nome, "linkedin": link}
    return None


def extrair_linkedin_empresa(resultados: list) -> str:
    for r in resultados:
        if "linkedin.com/company/" in r.get("link", ""):
            return r.get("link")
    return None


# ─── NÍVEL 2 — pago com cota: Apify (lista real de funcionários) ────────────
def buscar_apify_funcionarios(linkedin_empresa_url: str) -> list:
    if not APIFY_TOKEN or not linkedin_empresa_url:
        return []
    try:
        url = "https://api.apify.com/v2/acts/apt_marble~linkedin-company-employees-scraper/run-sync-get-dataset-items"
        r = requests.post(url, params={"token": APIFY_TOKEN},
                           json={"companies": [linkedin_empresa_url]}, timeout=90)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def filtrar_cargo_na_lista(funcionarios: list, termos_cargo: list) -> dict:
    for f in funcionarios:
        titulo = f.get("title", "") or f.get("headline", "") or f.get("position", "")
        nome = f.get("name", "") or f.get("fullName", "")
        link = f.get("profileUrl", "") or f.get("url", "") or f.get("link", "")
        if not titulo or not nome:
            continue
        if any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos_cargo):
            cargo = next((t for t in termos_cargo if normalizar_texto(t) in normalizar_texto(titulo)), titulo)
            return {"nome_cargo": f"{nome} - {cargo}", "linkedin": link}
    return None


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
            emails_encontrados, telefones_encontrados, socios = [], [], []
            site, empresa_nome, fonte_receita = None, entrada, False
            linkedin_empresa, pessoa_rh, pessoa_fin = None, None, None
            niveis_usados = ["gratuito"]

            # NÍVEL 0 — sempre tenta primeiro, sem custo
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
            site = descobrir_site(termo_busca)
            if site:
                extra = extrair_emails_telefones_do_site(site)
                emails_encontrados += extra["emails"]
                telefones_encontrados += extra["telefones"]

            # NÍVEL 1 — só entra se faltou telefone OU email, e se ainda há cota
            precisa_mais = not telefones_encontrados or not emails_encontrados
            if precisa_mais and pode_usar("serpapi"):
                r1 = buscar_serpapi(f'"{termo_busca}" telefone contato email')
                registrar_uso("serpapi")
                niveis_usados.append("serpapi")
                texto1 = texto_resultados(r1)
                if not site:
                    site = extrair_linkedin_empresa(r1)  # fallback bem fraco, raramente usado
                padrao_email = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
                novos_emails = re.findall(padrao_email, texto1)
                emails_encontrados += [e.lower() for e in novos_emails if normalizar_texto(termo_busca)[:6] in normalizar_texto(e)]
                novos_tel = re.findall(r'\(\d{2}\)\s?\d{4,5}-?\d{4}', texto1)
                telefones_encontrados += novos_tel

                if pode_usar("serpapi"):
                    r_li = buscar_serpapi(f'"{termo_busca}" site:linkedin.com/company')
                    registrar_uso("serpapi")
                    linkedin_empresa = extrair_linkedin_empresa(r_li)

                if linkedin_empresa and pode_usar("serpapi"):
                    termos_rh = ["RH", "Recursos Humanos", "Gerente de RH", "Diretor de RH", "Head de RH"]
                    termos_fin = ["Financeiro", "CFO", "Diretor Financeiro", "Gerente Financeiro", "Controller"]
                    r_rh = buscar_serpapi(f'"{termo_busca}" (gerente OR diretor OR head) RH site:linkedin.com/in')
                    registrar_uso("serpapi")
                    pessoa_rh = extrair_pessoa_linkedin(r_rh, termo_busca, termos_rh)

                    if pode_usar("serpapi"):
                        r_fin = buscar_serpapi(f'"{termo_busca}" (diretor OR gerente OR CFO) financeiro site:linkedin.com/in')
                        registrar_uso("serpapi")
                        niveis_usados.append("serpapi")
                        pessoa_fin = extrair_pessoa_linkedin(r_fin, termo_busca, termos_fin)

            # NÍVEL 2 — só entra se ainda faltou o Financeiro, e se há cota de Apify
            if not pessoa_fin and linkedin_empresa and pode_usar("apify"):
                funcionarios = buscar_apify_funcionarios(linkedin_empresa)
                if funcionarios:
                    registrar_uso("apify")
                    niveis_usados.append("apify")
                    termos_fin = ["Financeiro", "CFO", "Diretor Financeiro", "Gerente Financeiro", "Controller"]
                    termos_rh = ["RH", "Recursos Humanos", "Gerente de RH", "Diretor de RH"]
                    pessoa_fin = filtrar_cargo_na_lista(funcionarios, termos_fin)
                    if not pessoa_rh:
                        pessoa_rh = filtrar_cargo_na_lista(funcionarios, termos_rh)

            telefones_encontrados = list(dict.fromkeys(telefones_encontrados))
            emails_encontrados = list(dict.fromkeys(emails_encontrados))
            emails_classificados = [{"email": e, "departamento": classificar_email_por_departamento(e)} for e in emails_encontrados[:5]]
            sugestoes = sugerir_emails_departamentais(site, emails_encontrados)

            nao_encontrado = "Não encontrado em fonte pública"
            resultado = {
                "empresa": empresa_nome,
                "site": site or nao_encontrado,
                "telefones": telefones_encontrados[:5],
                "emails": emails_classificados,
                "emails_sugeridos": sugestoes,
                "socios": socios[:5],
                "fonte_receita_federal": fonte_receita,
                "linkedin_empresa": linkedin_empresa or nao_encontrado,
                "linkedin_rh": (pessoa_rh["nome_cargo"] + " (a confirmar)") if pessoa_rh else nao_encontrado,
                "linkedin_rh_url": pessoa_rh["linkedin"] if pessoa_rh else None,
                "linkedin_financeiro": (pessoa_fin["nome_cargo"] + " (a confirmar)") if pessoa_fin else nao_encontrado,
                "linkedin_financeiro_url": pessoa_fin["linkedin"] if pessoa_fin else None,
                "niveis_usados": list(dict.fromkeys(niveis_usados)),
                "veio_do_cache": False
            }
            salvar_no_cache(chave, resultado)

        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    numeros_conhecidos_norm = [normalizar_telefone(n) for n in re.split(r'[,;\n]', numeros_conhecidos) if n.strip()]
    resultado["telefones_novos"] = [t for t in resultado["telefones"] if normalizar_telefone(t) not in numeros_conhecidos_norm]
    resultado["telefones_ja_conhecidos"] = [t for t in resultado["telefones"] if normalizar_telefone(t) in numeros_conhecidos_norm]

    quotas = ler_quotas()
    resultado["quotas"] = {fonte: {"usos": quotas[fonte]["usos"], "limite": LIMITES_MES[fonte]} for fonte in LIMITES_MES}

    return jsonify(resultado)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
