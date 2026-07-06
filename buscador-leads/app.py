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


# ─── NOVO: validação de confiança (filtro anti-contador) ────────────────────
# O cadastro da Receita é preenchido, na maioria das vezes, pelo contador da
# empresa. E-mail/telefone declarados podem ser do escritório contábil, não
# do lead. Estas funções classificam a confiança de cada dado antes de
# entregá-lo ao BDR.

PADROES_CONTADOR = [
    "contab", "contabil", "contadores", "contador", "assessoria",
    "escritorio", "fiscal", "tributar", "tributos", "bpo",
    "consultoriacontabil", "accounting", "despachante", "certificadora",
]

DOMINIOS_GENERICOS = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br",
    "uol.com.br", "bol.com.br", "terra.com.br", "ig.com.br", "globo.com",
    "live.com", "msn.com", "icloud.com", "r7.com", "oi.com.br", "zipmail.com.br",
}


def extrair_dominio_de_url(url: str) -> str:
    """https://www.empresa.com.br/contato -> empresa.com.br"""
    if not url:
        return ""
    m = re.match(r'https?://(?:www\.)?([^/]+)', url.strip())
    return m.group(1).lower() if m else url.strip().lower().replace("www.", "")


def extrair_dominio_de_email(email: str) -> str:
    return email.split("@")[-1].lower().strip() if "@" in email else ""


def parece_contador(texto: str) -> bool:
    """Detecta padrões de escritório contábil no e-mail inteiro ou domínio."""
    t = normalizar_texto(texto)
    return any(p in t for p in PADROES_CONTADOR)


def avaliar_confianca_email(email: str, dominio_site: str, razao_social: str = "") -> dict:
    """
    Retorna {"nivel": "alta"|"media"|"baixa", "motivo": str, "possivel_contador": bool}

    Regras (na ordem):
    1. Padrão contábil no e-mail        -> baixa, possível contador
    2. Domínio do e-mail == domínio site -> alta (é da própria empresa)
    3. Domínio genérico (gmail, uol...)  -> media (pode ser da empresa pequena,
                                            mas também do contador; não dá pra saber)
    4. Domínio próprio != site           -> media (domínio corporativo, mas não
                                            bate com o site conhecido — pode ser
                                            grupo/holding ou terceiro)
    5. Sem site descoberto p/ comparar   -> media se domínio próprio parecido com
                                            a razão social, baixa caso contrário
    """
    dom_email = extrair_dominio_de_email(email)

    if parece_contador(email):
        return {"nivel": "baixa", "motivo": "padrão de escritório contábil no endereço",
                "possivel_contador": True}

    if dominio_site and dom_email == dominio_site:
        return {"nivel": "alta", "motivo": "domínio bate com o site da empresa",
                "possivel_contador": False}

    if dom_email in DOMINIOS_GENERICOS:
        return {"nivel": "media", "motivo": "domínio genérico — impossível confirmar dono",
                "possivel_contador": False}

    if dominio_site and dom_email != dominio_site:
        # domínio corporativo, mas diferente do site: holding? contador com domínio neutro?
        return {"nivel": "media", "motivo": f"domínio próprio ({dom_email}) difere do site ({dominio_site})",
                "possivel_contador": False}

    # sem site para comparar: usa semelhança com a razão social como heurística
    if razao_social:
        raiz = normalizar_texto(razao_social)[:6]
        if raiz and raiz in normalizar_texto(dom_email):
            return {"nivel": "media", "motivo": "domínio parecido com a razão social (site não confirmado)",
                    "possivel_contador": False}

    return {"nivel": "baixa", "motivo": "sem site para confirmar e domínio não bate com a razão social",
            "possivel_contador": False}


def avaliar_confianca_telefone(telefone: str, origem: str, telefones_site: list) -> dict:
    """
    Cross-check: telefone da Receita confirmado pelo site = alta.
    Só da Receita = media (pode ser do contador, mas ligar ainda tem valor).
    Só do site = alta (publicado pela própria empresa).
    """
    tel_norm = normalizar_telefone(telefone)
    no_site = any(normalizar_telefone(t) == tel_norm for t in telefones_site)

    if origem == "receita" and no_site:
        return {"nivel": "alta", "motivo": "consta na Receita E no site da empresa"}
    if origem == "site":
        return {"nivel": "alta", "motivo": "publicado no site da própria empresa"}
    if origem == "receita":
        return {"nivel": "media", "motivo": "somente na Receita — pode ser do contador; se atender, pedir o contato da empresa"}
    return {"nivel": "media", "motivo": f"origem: {origem}"}


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


# ─── NÍVEL 0 — fallback gratuito: CNPJá open (mesma fonte Receita) ──────────
# Mesmo dado da BrasilAPI (cadastro da Receita), então o risco de contador é
# idêntico. Serve como redundância quando a BrasilAPI cai/limita, e captura
# múltiplos telefones/e-mails quando declarados (a BrasilAPI só traz o 1º tel).
# Limite do endpoint aberto: ~5 consultas/min, sem chave.
def buscar_cnpja(cnpj: str) -> dict:
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(
            f"https://open.cnpja.com/office/{cnpj_limpo}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        company = data.get("company", {}) or {}

        telefones = []
        for ph in (data.get("phones") or []):
            area, numero = ph.get("area", ""), ph.get("number", "")
            if area and numero:
                tel = f"({area}) {numero[:5]}-{numero[5:]}" if len(numero) == 9 else f"({area}) {numero[:4]}-{numero[4:]}"
                if telefone_plausivel(tel):
                    telefones.append(tel)

        emails = [e.get("address", "").lower() for e in (data.get("emails") or []) if e.get("address")]

        addr = data.get("address", {}) or {}
        return {
            "razao_social": company.get("name"),
            "nome_fantasia": data.get("alias") or company.get("name"),
            "telefone": telefones[0] if telefones else None,
            "telefones_extras": telefones[1:],
            "email": emails[0] if emails else None,
            "emails_extras": emails[1:],
            "endereco": f"{addr.get('street', '')}, {addr.get('city', '')} - {addr.get('state', '')}",
            "socios": [m.get("person", {}).get("name") for m in (company.get("members") or []) if m.get("person", {}).get("name")]
        }
    except Exception:
        return {}


def buscar_receita(cnpj: str) -> dict:
    """Nível 0 com redundância: BrasilAPI primeiro, CNPJá se ela falhar."""
    dados = buscar_brasilapi(cnpj)
    if dados:
        dados["fonte_cadastro"] = "brasilapi"
        return dados
    dados = buscar_cnpja(cnpj)
    if dados:
        dados["fonte_cadastro"] = "cnpja"
    return dados


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


DDDS_VALIDOS = {
    '11','12','13','14','15','16','17','18','19',  # SP
    '21','22','24',                                  # RJ
    '27','28',                                        # ES
    '31','32','33','34','35','37','38',               # MG
    '41','42','43','44','45','46',                    # PR
    '47','48','49',                                    # SC
    '51','53','54','55',                                # RS
    '61',                                                # DF
    '62','64',                                           # GO
    '63',                                                 # TO
    '65','66',                                            # MT
    '67',                                                  # MS
    '68',                                                   # AC
    '69',                                                    # RO
    '71','73','74','75','77',                                # BA
    '79',                                                      # SE
    '81','87',                                                  # PE
    '82',                                                        # AL
    '83',                                                         # PB
    '84',                                                          # RN
    '85','88',                                                      # CE
    '86','89',                                                       # PI
    '91','93','94',                                                   # PA
    '92','97',                                                         # AM
    '95',                                                               # RR
    '96',                                                                # AP
    '98','99',                                                           # MA
}


def ddd_valido(telefone: str) -> bool:
    """Extrai o DDD de um telefone formatado e verifica se é um DDD real do Brasil"""
    digitos = re.sub(r'\D', '', telefone)
    if len(digitos) < 10:
        return False
    ddd = digitos[:2]
    return ddd in DDDS_VALIDOS


def telefone_plausivel(telefone: str) -> bool:
    """Validação final: DDD real + quantidade de dígitos correta (10 ou 11, sem contar DDD do país)"""
    digitos = re.sub(r'\D', '', telefone)
    # remove código do país se presente
    if digitos.startswith('55') and len(digitos) > 11:
        digitos = digitos[2:]
    if len(digitos) not in (10, 11):
        return False
    return digitos[:2] in DDDS_VALIDOS


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

            # Telefones: regex captura candidatos, validação de DDD descarta lixo
            candidatos_tel = []
            for p in [r'\(\d{2}\)\s?\d{4,5}-?\d{4}', r'\+55\s?\d{2}\s?\d{4,5}[-\s]?\d{4}',
                      r'\b\d{2}\s\d{4,5}-?\d{4}\b', r'0800\s?\d{3}\s?\d{4}']:
                candidatos_tel += re.findall(p, texto)
            telefones += [t for t in candidatos_tel if t.startswith('0800') or telefone_plausivel(t)]

            # WhatsApp — regex restrito a 12-13 dígitos (55 + DDD + número), evita pegar lixo
            whats = re.findall(r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\+?55\d{10,11})', texto)
            for numero in whats:
                num = re.sub(r'\D', '', numero)
                if num.startswith('55'):
                    num = num[2:]
                if telefone_plausivel(num):
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

            # NOVO: rastreio de origem para o cálculo de confiança
            razao_social = ""
            email_receita, telefone_receita = None, None
            telefones_receita_extras, emails_receita_extras = [], []
            telefones_site, emails_site = [], []

            # NÍVEL 0 — sempre tenta primeiro, sem custo
            if eh_cnpj(entrada):
                dados = buscar_receita(entrada)  # BrasilAPI com fallback CNPJá
                if dados:
                    empresa_nome = dados.get("nome_fantasia") or entrada
                    razao_social = dados.get("razao_social") or ""
                    fonte_receita = True
                    socios = dados.get("socios", [])
                    if dados.get("telefone"):
                        telefone_receita = dados["telefone"]
                        telefones_encontrados.append(telefone_receita)
                    # CNPJá pode trazer telefones/e-mails adicionais declarados
                    for tel_extra in dados.get("telefones_extras", []):
                        telefones_encontrados.append(tel_extra)
                        telefones_receita_extras.append(tel_extra)
                    if dados.get("email"):
                        email_receita = dados["email"].lower()
                        emails_encontrados.append(email_receita)
                    for em_extra in dados.get("emails_extras", []):
                        emails_encontrados.append(em_extra.lower())
                        emails_receita_extras.append(em_extra.lower())

            termo_busca = empresa_nome if empresa_nome != entrada else entrada
            site = descobrir_site(termo_busca)
            if site:
                extra = extrair_emails_telefones_do_site(site)
                emails_site = extra["emails"]
                telefones_site = extra["telefones"]
                emails_encontrados += emails_site
                telefones_encontrados += telefones_site

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
                novos_tel = [t for t in novos_tel if telefone_plausivel(t)]
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

            # ── NOVO: classificação de confiança ──────────────────────────
            dominio_site = extrair_dominio_de_url(site) if site else ""

            emails_classificados = []
            emails_nao_verificados = []
            for e in emails_encontrados[:8]:
                confianca = avaliar_confianca_email(e, dominio_site, razao_social)
                registro = {
                    "email": e,
                    "departamento": classificar_email_por_departamento(e),
                    "confianca": confianca["nivel"],
                    "motivo": confianca["motivo"],
                    "possivel_contador": confianca["possivel_contador"],
                    "origem": "receita_federal" if (e == email_receita or e in emails_receita_extras) else ("site" if e in emails_site else "busca"),
                }
                # e-mail com padrão de contador NÃO entra na lista principal:
                # cold email pra contador é buraco negro e queima sender reputation
                if confianca["possivel_contador"]:
                    emails_nao_verificados.append(registro)
                else:
                    emails_classificados.append(registro)

            telefones_classificados = []
            for t in telefones_encontrados[:5]:
                if t == telefone_receita or t in telefones_receita_extras:
                    origem = "receita"
                elif t in telefones_site:
                    origem = "site"
                else:
                    origem = "busca"
                conf_tel = avaliar_confianca_telefone(t, origem, telefones_site)
                telefones_classificados.append({
                    "telefone": t,
                    "origem": origem,
                    "confianca": conf_tel["nivel"],
                    "motivo": conf_tel["motivo"],
                })

            # telefone suspeito de contador (só na Receita + e-mail da Receita era de contador):
            # rebaixa a confiança e deixa o alerta explícito pro BDR
            receita_email_de_contador = email_receita and parece_contador(email_receita)
            if receita_email_de_contador:
                for tc in telefones_classificados:
                    if tc["origem"] == "receita" and tc["confianca"] != "alta":
                        tc["confianca"] = "baixa"
                        tc["motivo"] = "cadastro da Receita aparenta ser do contador (e-mail contábil no mesmo registro)"

            sugestoes = sugerir_emails_departamentais(site, [e["email"] for e in emails_classificados])

            nao_encontrado = "Não encontrado em fonte pública"
            resultado = {
                "empresa": empresa_nome,
                "site": site or nao_encontrado,
                # formato antigo preservado (index.html continua funcionando):
                "telefones": [tc["telefone"] for tc in telefones_classificados],
                "emails": [{"email": ec["email"], "departamento": ec["departamento"]} for ec in emails_classificados],
                # NOVOS campos com a camada de confiança:
                "telefones_detalhe": telefones_classificados,
                "emails_detalhe": emails_classificados,
                "emails_nao_verificados": emails_nao_verificados,
                "alerta_contador": bool(receita_email_de_contador or emails_nao_verificados),
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
