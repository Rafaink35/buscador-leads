import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY           = os.getenv("SERPAPI_KEY")
APIFY_TOKEN           = os.getenv("APIFY_TOKEN")
HUNTER_API_KEY        = os.getenv("HUNTER_API_KEY")
LUSHA_API_KEY         = os.getenv("LUSHA_API_KEY")
PHANTOMBUSTER_API_KEY = os.getenv("PHANTOMBUSTER_API_KEY")
PHANTOMBUSTER_AGENT_ID= os.getenv("PHANTOMBUSTER_AGENT_ID")
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"

CACHE_ARQUIVO  = "cache_empresas.json"
QUOTA_ARQUIVO  = "quota_uso.json"
CACHE_VALIDADE_DIAS = 30

LIMITES_MES = {
    "serpapi":      90,
    "apify":        40,
    "hunter":       20,
    "lusha":        60,   # free tier: 70 créditos — deixamos 10 de colchão
    "phantombuster":20,
    "gemini":       40,
}


# ─── Cache ──────────────────────────────────────────────────────────────────
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

def buscar_no_cache(chave: str, hubspot_fingerprint: str) -> dict:
    cache = ler_cache()
    entrada = cache.get(chave)
    if not entrada:
        return None
    if (time.time() - entrada.get("timestamp", 0)) / 86400 > CACHE_VALIDADE_DIAS:
        return None
    if entrada.get("hubspot_fingerprint", "") != hubspot_fingerprint:
        return None
    return entrada.get("dados")

def salvar_no_cache(chave: str, dados: dict, hubspot_fingerprint: str):
    cache = ler_cache()
    cache[chave] = {"timestamp": time.time(), "dados": dados, "hubspot_fingerprint": hubspot_fingerprint}
    gravar_cache(cache)


# ─── Controle de cota ────────────────────────────────────────────────────────
def ler_quotas() -> dict:
    hoje = __import__("datetime").date.today()
    mes_atual = f"{hoje.year}-{hoje.month:02d}"
    dados = {}
    if os.path.exists(QUOTA_ARQUIVO):
        with open(QUOTA_ARQUIVO, "r") as f:
            dados = json.load(f)
    for fonte in LIMITES_MES:
        if fonte not in dados or dados[fonte].get("mes") != mes_atual:
            dados[fonte] = {"mes": mes_atual, "usos": 0}
    return dados

def gravar_quotas(dados: dict):
    with open(QUOTA_ARQUIVO, "w") as f:
        json.dump(dados, f)

def pode_usar(fonte: str) -> bool:
    return ler_quotas()[fonte]["usos"] < LIMITES_MES[fonte]

def registrar_uso(fonte: str):
    quotas = ler_quotas()
    quotas[fonte]["usos"] += 1
    gravar_quotas(quotas)


# ─── Utilitários ─────────────────────────────────────────────────────────────
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

def normalizar_contato(valor: str) -> str:
    valor = (valor or "").strip()
    return valor.lower() if "@" in valor else normalizar_telefone(valor)

def parse_lista_contatos(bruto) -> list:
    if not bruto:
        return []
    if isinstance(bruto, list):
        return [c for c in bruto if c and str(c).strip()]
    return [c for c in re.split(r'[,;\n]', str(bruto)) if c.strip()]

def fingerprint_contatos(contatos: list) -> str:
    return "|".join(sorted(set(normalizar_contato(c) for c in contatos)))

def separar_nome(nome_completo: str) -> tuple:
    nome_limpo = (nome_completo or "").split(" - ")[0].strip()
    partes = nome_limpo.split()
    if not partes:
        return "", ""
    return partes[0], (partes[-1] if len(partes) > 1 else "")

def pessoa_completa(pessoa: dict) -> bool:
    if not pessoa:
        return False
    return bool(pessoa.get("nome_cargo")) and bool(pessoa.get("email") or pessoa.get("telefone"))

def dados_completos(pessoa_rh: dict, pessoa_fin: dict) -> bool:
    return pessoa_completa(pessoa_rh) and pessoa_completa(pessoa_fin)

def extrair_dominio_de_url(url: str) -> str:
    if not url:
        return ""
    m = re.match(r'https?://(?:www\.)?([^/]+)', url.strip())
    return m.group(1).lower() if m else url.strip().lower().replace("www.", "")

def extrair_dominio_de_email(email: str) -> str:
    return email.split("@")[-1].lower().strip() if "@" in email else ""

PREFIXOS_DEPARTAMENTO = {
    "financeiro": ["financeiro", "contas", "cobranca", "billing", "faturamento"],
    "rh":         ["rh", "recursoshumanos", "recrutamento", "vagas", "talentos", "people"],
    "compras":    ["compras", "suprimentos", "procurement", "fornecedores"],
    "comercial":  ["comercial", "vendas", "sales", "atendimento", "contato"],
}

def classificar_email_por_departamento(email: str) -> str:
    usuario = email.split("@")[0].lower()
    for depto, prefixos in PREFIXOS_DEPARTAMENTO.items():
        if any(p == usuario or usuario.startswith(p + ".") or usuario.startswith(p + "-") for p in prefixos):
            return depto
    return "geral"


# ─── Confiança de contatos ───────────────────────────────────────────────────
PADROES_CONTADOR = ["contab","contabil","contadores","contador","assessoria",
                    "escritorio","fiscal","tributar","tributos","bpo",
                    "consultoriacontabil","accounting","despachante","certificadora"]
DOMINIOS_GENERICOS = {"gmail.com","hotmail.com","outlook.com","yahoo.com","yahoo.com.br",
                       "uol.com.br","bol.com.br","terra.com.br","ig.com.br","globo.com",
                       "live.com","msn.com","icloud.com","r7.com","oi.com.br","zipmail.com.br"}

def parece_contador(texto: str) -> bool:
    t = normalizar_texto(texto)
    return any(p in t for p in PADROES_CONTADOR)

def avaliar_confianca_email(email: str, dominio_site: str, razao_social: str = "") -> dict:
    dom_email = extrair_dominio_de_email(email)
    if parece_contador(email):
        return {"nivel": "baixa", "motivo": "padrão de escritório contábil", "possivel_contador": True}
    if dominio_site and dom_email == dominio_site:
        return {"nivel": "alta", "motivo": "domínio bate com o site da empresa", "possivel_contador": False}
    if dom_email in DOMINIOS_GENERICOS:
        return {"nivel": "media", "motivo": "domínio genérico", "possivel_contador": False}
    if dominio_site and dom_email != dominio_site:
        return {"nivel": "baixa", "motivo": f"domínio ({dom_email}) não bate com o site ({dominio_site})", "possivel_contador": False}
    if razao_social:
        raiz = normalizar_texto(razao_social)[:6]
        if raiz and raiz in normalizar_texto(dom_email):
            return {"nivel": "media", "motivo": "domínio parecido com a razão social", "possivel_contador": False}
    return {"nivel": "baixa", "motivo": "sem site para confirmar", "possivel_contador": False}

def avaliar_confianca_telefone(telefone: str, origem: str, telefones_site: list) -> dict:
    tel_norm = normalizar_telefone(telefone)
    no_site = any(normalizar_telefone(t) == tel_norm for t in telefones_site)
    if origem == "receita" and no_site:
        return {"nivel": "alta", "motivo": "consta na Receita E no site da empresa"}
    if origem == "site":
        return {"nivel": "alta", "motivo": "publicado no site da própria empresa"}
    if origem == "receita":
        return {"nivel": "media", "motivo": "somente na Receita — pode ser do contador"}
    return {"nivel": "media", "motivo": f"origem: {origem}"}


# ─── NÍVEL 0 — Receita Federal (grátis, 3 fontes em cascata) ────────────────
def buscar_brasilapi(cnpj: str) -> dict:
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_limpo}",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code != 200:
            return {}
        d = r.json()
        telefone = None
        if d.get("ddd_telefone_1"):
            tel = d["ddd_telefone_1"]
            telefone = tel if "(" in tel else f"({tel[:2]}) {tel[2:]}"
        return {"razao_social": d.get("razao_social"),
                "nome_fantasia": d.get("nome_fantasia") or d.get("razao_social"),
                "telefone": telefone, "email": d.get("email"),
                "socios": [s.get("nome_socio") for s in d.get("qsa", []) if s.get("nome_socio")]}
    except Exception:
        return {}

def buscar_cnpja(cnpj: str) -> dict:
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(f"https://open.cnpja.com/office/{cnpj_limpo}",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code != 200:
            return {}
        d = r.json()
        company = d.get("company", {}) or {}
        telefones = []
        for ph in (d.get("phones") or []):
            area, numero = ph.get("area", ""), ph.get("number", "")
            if area and numero:
                tel = f"({area}) {numero[:5]}-{numero[5:]}" if len(numero) == 9 else f"({area}) {numero[:4]}-{numero[4:]}"
                if telefone_plausivel(tel):
                    telefones.append(tel)
        emails = [e.get("address", "").lower() for e in (d.get("emails") or []) if e.get("address")]
        return {"razao_social": company.get("name"),
                "nome_fantasia": d.get("alias") or company.get("name"),
                "telefone": telefones[0] if telefones else None,
                "telefones_extras": telefones[1:],
                "email": emails[0] if emails else None,
                "emails_extras": emails[1:],
                "socios": [m.get("person", {}).get("name") for m in (company.get("members") or []) if m.get("person", {}).get("name")]}
    except Exception:
        return {}

def buscar_receitaws(cnpj: str) -> dict:
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(f"https://receitaws.com.br/v1/cnpj/{cnpj_limpo}", timeout=8)
        if r.status_code != 200:
            return {}
        d = r.json()
        if d.get("status") == "ERROR":
            return {}
        return {"razao_social": d.get("nome"),
                "nome_fantasia": d.get("fantasia") or d.get("nome"),
                "telefone": d.get("telefone") or None,
                "email": (d.get("email") or "").lower() or None,
                "socios": [s.get("nome") for s in d.get("qsa", []) if s.get("nome")]}
    except Exception:
        return {}

def buscar_receita(cnpj: str) -> dict:
    for fn, nome in [(buscar_brasilapi, "brasilapi"), (buscar_cnpja, "cnpja"), (buscar_receitaws, "receitaws")]:
        dados = fn(cnpj)
        if dados:
            dados["fonte_cadastro"] = nome
            return dados
    return {}


# ─── NÍVEL 0 — Site oficial (grátis) ─────────────────────────────────────────
TLDS_TENTATIVA_DIRETA = ['.com.br', '.com', '.ai', '.io', '.co', '.net',
                          '.app', '.tech', '.digital', '.online', '.store', '.cloud']
TLDS_CONHECIDOS = ['.com.br', '.com', '.com.ar', '.com.mx', '.org.br', '.org',
                   '.net.br', '.net', '.ai', '.io', '.co', '.app', '.tech',
                   '.digital', '.online', '.store', '.site', '.cloud']
SINAIS_PAGINA_INVALIDA = ["just a moment", "enable javascript and cookies",
                           "domain is for sale", "buy this domain", "parked domain"]

def parece_dominio(texto: str) -> bool:
    texto = texto.strip()
    return " " not in texto and bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*(\.[a-zA-Z0-9-]+)+$', texto))

def remover_tld(empresa: str) -> str:
    """Remove o TLD antes de normalizar — 'blip.ai' -> 'blip', nao 'blipai'.
    Evita gerar slugs errados como 'blipai.com.br' quando a entrada e um dominio."""
    empresa_lower = empresa.lower().strip()
    for tld in sorted(TLDS_CONHECIDOS, key=len, reverse=True):
        if empresa_lower.endswith(tld):
            return empresa_lower[:-len(tld)]
    return empresa_lower

def validar_candidato_site(url: str, termo_esperado: str, exigir_termo: bool = True) -> str:
    try:
        r = requests.get(url, timeout=8, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if r.status_code >= 400:
            return None
        if extrair_dominio_de_url(r.url) != extrair_dominio_de_url(url):
            return None
        texto_pagina = r.text[:20000]
        if any(s in texto_pagina.lower() for s in SINAIS_PAGINA_INVALIDA):
            return None
        if not exigir_termo:
            return r.url
        if termo_esperado and termo_esperado in normalizar_texto(texto_pagina):
            return r.url
    except Exception:
        pass
    return None

def gerar_variacoes_slug(empresa: str) -> list:
    # Se parece domínio (blip.ai, totvs.com.br), usa só o nome sem o TLD
    if parece_dominio(empresa):
        slug_base = normalizar_texto(remover_tld(empresa))
        return [slug_base] if slug_base else []

    palavras = re.sub(r'[^a-zA-Z0-9\s]', '', empresa).split()
    ignorar = {'ltda','sa','eireli','me','epp','equipamentos','comercio','industria',
               'servicos','solucoes','grupo','brasil','lojas','cia','companhia','rede'}
    uteis = [p for p in palavras if p.lower() not in ignorar]
    slugs = []
    if uteis:
        slugs.append(normalizar_texto(uteis[0]))
    if len(uteis) >= 2:
        slugs.append(normalizar_texto(uteis[0] + uteis[1]))
    slugs.append(normalizar_texto(empresa))
    return list(dict.fromkeys(slugs))

def descobrir_site_tentativa_direta(empresa: str) -> str:
    if parece_dominio(empresa):
        dominio = empresa.strip().lower()
        for url in [f"https://www.{dominio}", f"https://{dominio}"]:
            achado = validar_candidato_site(url, None, exigir_termo=False)
            if achado:
                return achado
    for slug in gerar_variacoes_slug(empresa):
        if len(slug) < 3:
            continue
        for tld in TLDS_TENTATIVA_DIRETA:
            for url in [f"https://www.{slug}{tld}", f"https://{slug}{tld}"]:
                achado = validar_candidato_site(url, slug)
                if achado:
                    return achado
    return None

def descobrir_site_via_duckduckgo(empresa: str) -> str:
    try:
        r = requests.get("https://html.duckduckgo.com/html/",
                          params={"q": f"{empresa} site oficial"},
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                          timeout=6)
        if r.status_code != 200:
            return None
        links = re.findall(r'href="(https?://[^"]+)"', r.text)
        bloqueados = ["duckduckgo.com","linkedin.com","facebook.com","instagram.com",
                      "youtube.com","indeed.com","glassdoor","wikipedia.org","google.com",
                      "econodata","cnpj","consultas.plus","datanyze"]
        primeira = normalizar_texto(empresa.split()[0]) if empresa.split() else ""
        for link in links:
            if any(b in link.lower() for b in bloqueados):
                continue
            if primeira and len(primeira) >= 4 and primeira in normalizar_texto(link):
                m = re.match(r'https?://(?:www\.)?([^/]+)', link)
                if m:
                    return f"https://{m.group(1)}"
        return None
    except Exception:
        return None

def descobrir_site(empresa: str) -> str:
    return descobrir_site_tentativa_direta(empresa) or descobrir_site_via_duckduckgo(empresa)

DDDS_VALIDOS = {
    '11','12','13','14','15','16','17','18','19','21','22','24','27','28',
    '31','32','33','34','35','37','38','41','42','43','44','45','46','47','48','49',
    '51','53','54','55','61','62','63','64','65','66','67','68','69',
    '71','73','74','75','77','79','81','82','83','84','85','86','87','88','89',
    '91','92','93','94','95','96','97','98','99',
}

def telefone_plausivel(telefone: str) -> bool:
    digitos = re.sub(r'\D', '', telefone)
    if digitos.startswith('55') and len(digitos) > 11:
        digitos = digitos[2:]
    if len(digitos) not in (10, 11):
        return False
    return digitos[:2] in DDDS_VALIDOS

def extrair_emails_telefones_do_site(url_base: str) -> dict:
    paginas = ["","/contato","/fale-conosco","/sobre","/atendimento","/contact",
               "/trabalhe-conosco","/carreiras","/financeiro","/fornecedores",
               "/contatos","/quem-somos","/institucional"]
    emails, telefones = [], []
    for pagina in paginas:
        try:
            r = requests.get(url_base.rstrip("/") + pagina,
                              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                              timeout=8)
            if r.status_code != 200:
                continue
            texto = r.text
            achados = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', texto)
            ignorar = ['png','jpg','jpeg','gif','svg','webp','sentry','wixpress','.css','.js',
                       'example','schema.org','w3.org','gravatar']
            emails += [e.lower() for e in achados if not any(i in e.lower() for i in ignorar)]
            candidatos_tel = []
            for p in [r'\(\d{2}\)\s?\d{4,5}-?\d{4}', r'\+55\s?\d{2}\s?\d{4,5}[-\s]?\d{4}',
                      r'\b\d{2}\s\d{4,5}-?\d{4}\b', r'0800\s?\d{3}\s?\d{4}']:
                candidatos_tel += re.findall(p, texto)
            telefones += [t for t in candidatos_tel if t.startswith('0800') or telefone_plausivel(t)]
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
        if prefixo not in confirmados_norm:
            sugestoes.append({"departamento": depto, "email_sugerido": f"{prefixo}@{dominio_limpo}"})
    return sugestoes


# ─── NÍVEL 1 — SerpAPI ───────────────────────────────────────────────────────
def buscar_serpapi(query: str, hl: str = "pt", gl: str = "br") -> list:
    try:
        params = {"q": query, "api_key": SERPAPI_KEY, "engine": "google",
                  "num": 5, "hl": hl or "pt", "gl": gl or "br", "safe": "off"}
        r = requests.get("https://serpapi.com/search", params=params, timeout=8)
        results = r.json().get("organic_results", [])
        validos = [res for res in results if not (res.get("link","") or "").startswith("/goto")]
        return validos if validos else []
    except Exception:
        return []


def buscar_duckduckgo(query: str) -> list:
    """Busca via DuckDuckGo HTML — grátis, sem API key, sem problema de /goto.
    Retorna lista de resultados no mesmo formato do SerpAPI."""
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=8
        )
        if r.status_code != 200:
            return []
        # Extrai links e snippets do HTML do DuckDuckGo
        links = re.findall(r'class="result__url"[^>]*>([^<]+)<', r.text)
        titles = re.findall(r'class="result__a"[^>]*>([^<]+)<', r.text)
        snippets = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', r.text)
        # Normaliza URLs
        results = []
        for i, link in enumerate(links[:5]):
            url = link.strip()
            if not url.startswith("http"):
                url = "https://" + url
            results.append({
                "link": url,
                "title": titles[i].strip() if i < len(titles) else "",
                "snippet": snippets[i].strip() if i < len(snippets) else ""
            })
        return results
    except Exception:
        return []


def buscar_linkedin_pessoa(query: str) -> list:
    """Busca perfis de LinkedIn de pessoas — tenta SerpAPI, cai no DuckDuckGo."""
    if SERPAPI_KEY and pode_usar("serpapi"):
        resultados = buscar_serpapi(query, hl=None, gl=None)
        if any("linkedin.com/in/" in r.get("link","") for r in resultados):
            return resultados, True  # True = usou SerpAPI
    # DuckDuckGo como fallback
    return buscar_duckduckgo(query), False

def texto_resultados(resultados: list) -> str:
    return " ".join([(r.get("title","") + " " + r.get("snippet","") + " " + r.get("link","")) for r in resultados])

def termo_relacao_empresa(empresa: str) -> str:
    for tld in TLDS_TENTATIVA_DIRETA:
        if empresa.lower().endswith(tld):
            return normalizar_texto(empresa[:-len(tld)])
    return normalizar_texto(empresa)

def escolher_linkedin_via_gemini(resultados: list, empresa: str, papel: str) -> dict:
    """Gemini só ESCOLHE entre candidatos já trazidos pelo SerpAPI — nunca inventa."""
    candidatos = [r for r in resultados if "linkedin.com/in/" in r.get("link", "")]
    if not candidatos:
        return None
    lista = "\n".join(
        f'{i+1}. link: {c["link"]}\n   titulo: {c.get("title","")}\n   snippet: {c.get("snippet","")}'
        for i, c in enumerate(candidatos)
    )
    prompt = (
        f'Destes resultados de busca, qual é o perfil do LinkedIn de uma pessoa que '
        f'trabalha em "{empresa}" em cargo de {papel}?\n\n{lista}\n\n'
        f'Responda APENAS com o link exato de um dos resultados acima, ou a palavra '
        f'"nenhum" se não houver candidato claro. Nunca invente um link.'
    )
    try:
        r = requests.post(GEMINI_URL, json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 200,
                                  "thinkingConfig": {"thinkingBudget": 0}}
        }, timeout=20)
        if r.status_code != 200:
            return None
        texto = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None
    escolhido = next((c for c in candidatos if c["link"].strip() in texto), None)
    if not escolhido:
        return None
    titulo = escolhido.get("title", "")
    partes = titulo.split(" | ")[0].split(" - ", 1)
    nome = partes[0].strip()
    cargo = partes[1].strip() if len(partes) > 1 else None
    if not nome:
        return None
    return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome,
            "linkedin": escolhido["link"], "email": None, "telefone": None}


# ── CORRIGIDO: busca de LinkedIn melhorada com múltiplas estratégias ──────────
def extrair_pessoa_linkedin_de_resultados(resultados: list, empresa: str, termos_cargo: list, aceitar_analista: bool = False) -> dict:
    """
    Estratégia 1 (alta confiança): título contém 'na {empresa}' OU 'at {empresa}' E cargo bate.
    Ex: 'Diego Matuella - Gerente de Tesouraria na TOTVS' -> aceita imediatamente.
    Estratégia 2 (média confiança): empresa aparece no snippet E cargo no título.
    Estratégia 3 (baixa, só se aceitar_analista=True): analista/coordenador com empresa confirmada.
    """
    empresa_norm = normalizar_texto(empresa)
    empresa_lower = empresa.lower()
    padroes_empresa_titulo = [
        f" na {empresa_lower}",
        f" at {empresa_lower}",
        f"| {empresa_lower}",
        f"- {empresa_lower}",
    ]

    # Estratégia 1 — "na EMPRESA" no título + cargo confirmado
    for r in resultados[:5]:
        link = r.get("link", "")
        if "linkedin.com/in/" not in link:
            continue
        titulo_orig = r.get("title", "")
        titulo_lower = titulo_orig.lower()
        tem_empresa = any(p in titulo_lower for p in padroes_empresa_titulo)
        if not tem_empresa:
            tem_empresa = empresa_norm[:6] in normalizar_texto(titulo_orig)
        if not tem_empresa:
            continue
        tem_cargo = any(normalizar_texto(t) in normalizar_texto(titulo_orig) for t in termos_cargo)
        if not tem_cargo:
            continue
        partes = titulo_orig.split(" | ")[0].split(" - ", 1)
        nome = partes[0].strip()
        cargo = partes[1].strip() if len(partes) > 1 else None
        if not nome:
            continue
        return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome,
                "linkedin": link, "email": None, "telefone": None, "confianca_li": "alta"}

    # Estratégia 2 — empresa no snippet + cargo no título
    for r in resultados[:5]:
        link = r.get("link", "")
        if "linkedin.com/in/" not in link:
            continue
        titulo_orig = r.get("title", "")
        snippet = r.get("snippet", "")
        tem_empresa_snippet = empresa_norm[:6] in normalizar_texto(snippet)
        tem_cargo = any(normalizar_texto(t) in normalizar_texto(titulo_orig) for t in termos_cargo)
        if not tem_empresa_snippet or not tem_cargo:
            continue
        partes = titulo_orig.split(" | ")[0].split(" - ", 1)
        nome = partes[0].strip()
        cargo = partes[1].strip() if len(partes) > 1 else None
        if not nome:
            continue
        return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome,
                "linkedin": link, "email": None, "telefone": None, "confianca_li": "media"}

    # Estratégia 3 — analista/coordenador quando aceitar_analista=True
    if aceitar_analista:
        for r in resultados[:5]:
            link = r.get("link", "")
            if "linkedin.com/in/" not in link:
                continue
            titulo_orig = r.get("title", "")
            snippet = r.get("snippet", "")
            tem_empresa = (empresa_norm[:6] in normalizar_texto(titulo_orig) or
                           empresa_norm[:6] in normalizar_texto(snippet))
            tem_cargo = any(normalizar_texto(t) in normalizar_texto(titulo_orig) for t in termos_cargo)
            if not tem_empresa or not tem_cargo:
                continue
            partes = titulo_orig.split(" | ")[0].split(" - ", 1)
            nome = partes[0].strip()
            cargo = partes[1].strip() if len(partes) > 1 else None
            if not nome:
                continue
            return {"nome_cargo": f"{nome} - {cargo} ⚠️ analista" if cargo else nome,
                    "linkedin": link, "email": None, "telefone": None, "confianca_li": "baixa"}

    return None


def extrair_linkedin_empresa(resultados: list) -> str:
    for r in resultados:
        if "linkedin.com/company/" in r.get("link", ""):
            return r.get("link")
    return None


# ─── NÍVEL 2 — Apify ──────────────────────────────────────────────────────────
def buscar_apify_funcionarios(linkedin_empresa_url: str) -> list:
    if not APIFY_TOKEN or not linkedin_empresa_url:
        return []
    try:
        url = "https://api.apify.com/v2/acts/apt_marble~linkedin-company-employees-scraper/run-sync-get-dataset-items"
        r = requests.post(url, params={"token": APIFY_TOKEN},
                           json={"companyUrls": [linkedin_empresa_url]}, timeout=90)
        return r.json() if r.status_code in (200, 201) else []
    except Exception:
        return []

def filtrar_cargo_na_lista(funcionarios: list, termos_cargo: list) -> dict:
    for f in funcionarios:
        titulo = f.get("title","") or f.get("headline","") or f.get("position","")
        nome = f.get("name","") or f.get("fullName","")
        link = f.get("profileUrl","") or f.get("url","") or f.get("link","")
        email = (f.get("email") or "").lower() or None
        if not titulo or not nome:
            continue
        if any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos_cargo):
            cargo = next((t for t in termos_cargo if normalizar_texto(t) in normalizar_texto(titulo)), titulo)
            return {"nome_cargo": f"{nome} - {cargo}", "linkedin": link,
                    "email": email, "telefone": None, "confianca_li": "alta"}
    return None


# ─── NÍVEL 3 — Lusha ──────────────────────────────────────────────────────────
def buscar_lusha_decisores(dominio: str) -> dict:
    """
    Decision Makers API: POST com body {"companies": [{"domain": "..."}]}
    Retorna previews gratuitos — nome, cargo, LinkedIn, departamento.
    Emails/telefones precisam de enrich separado via buscar_lusha_enrich.
    """
    if not LUSHA_API_KEY or not dominio:
        return {}
    try:
        r = requests.post(
            "https://api.lusha.com/v3/contacts/decision-makers",
            headers={"api_key": LUSHA_API_KEY, "Content-Type": "application/json"},
            json={"companies": [{"domain": dominio}]},
            timeout=15
        )
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}

def buscar_lusha_enrich(linkedin_url: str) -> dict:
    """
    Search & Enrich: dado o LinkedIn de uma pessoa já identificada, retorna
    email, telefone e empresa atual — usado como VALIDADOR oficial.
    Gasta 1 crédito por email revelado + 5 por telefone.
    """
    if not LUSHA_API_KEY or not linkedin_url:
        return {}
    try:
        r = requests.get(
            "https://api.lusha.com/v3/contacts/search-and-enrich",
            headers={"api_key": LUSHA_API_KEY, "Content-Type": "application/json"},
            params={"linkedInUrl": linkedin_url},
            timeout=20
        )
        if r.status_code != 200:
            return {}
        dados = (r.json() or {}).get("data") or {}
        email = (dados.get("email") or "").lower() or None
        telefone = None
        phones = dados.get("phones") or []
        if phones:
            p = phones[0]
            raw = p.get("internationalNumber") or p.get("localNumber") or ""
            if raw and telefone_plausivel(raw):
                telefone = raw
        # Empresa atual retornada pela Lusha (usada pra validar)
        empresa_atual = (dados.get("currentJobTitle") or
                         (dados.get("positions") or [{}])[0].get("companyName") or
                         dados.get("companyName") or "")
        return {"email": email, "telefone": telefone, "empresa_atual": empresa_atual.lower()}
    except Exception:
        return {}


def validar_decisor_com_lusha(pessoa: dict, empresa_buscada: str) -> dict:
    """
    Usa a Lusha como validador oficial:
    1. Chama search-and-enrich com o LinkedIn já encontrado
    2. Verifica se a empresa atual retornada bate com a empresa buscada
    3. Se bater: retorna pessoa enriquecida com email/telefone (alta confiança)
    4. Se não bater: retorna None (pessoa errada, descarta)
    5. Se Lusha não tiver dados: retorna pessoa original sem validação
    """
    if not pessoa or not pessoa.get("linkedin"):
        return pessoa
    if not LUSHA_API_KEY or not pode_usar("lusha"):
        return pessoa  # sem cota, retorna sem validar

    enrich = buscar_lusha_enrich(pessoa["linkedin"])
    registrar_uso("lusha")

    if not enrich:
        # Lusha não achou — mantém pessoa mas marca como não validada
        pessoa["validado_lusha"] = False
        return pessoa

    empresa_atual = enrich.get("empresa_atual", "")
    empresa_norm = normalizar_texto(empresa_buscada)[:8]

    if empresa_atual and empresa_norm and empresa_norm not in normalizar_texto(empresa_atual):
        # Empresa não bate — pessoa errada, descarta
        return None

    # Empresa confirmada — enriquece com contatos reais
    if enrich.get("email"):
        pessoa["email"] = enrich["email"]
    if enrich.get("telefone"):
        pessoa["telefone"] = enrich["telefone"]
    pessoa["confianca_li"] = "alta"
    pessoa["validado_lusha"] = True
    return pessoa


def processar_lusha_decisores(dados_lusha: dict, termos_rh: list, termos_fin: list) -> tuple:
    """Extrai o melhor RH e Financeiro da resposta da Lusha Decision Makers v3.
    Estrutura real: {results: [{companyId, decisionMakers: [{firstName, lastName, jobTitle: {title}, socialLinks: {linkedin}}]}]}
    Os contatos são previews — emails/telefones precisam de enrich separado via LinkedIn."""
    pessoa_rh, pessoa_fin = None, None

    # Navega na estrutura real da API v3
    resultados = dados_lusha.get("results") or []
    contatos = []
    for res in resultados:
        contatos.extend(res.get("decisionMakers") or res.get("contacts") or [])

    # Fallback pra outras estruturas possíveis
    if not contatos:
        contatos = (dados_lusha.get("decisionMakers") or
                    dados_lusha.get("contacts") or
                    (dados_lusha if isinstance(dados_lusha, list) else []))

    for contato in contatos:
        # jobTitle pode ser objeto ou string
        job_obj = contato.get("jobTitle") or {}
        if isinstance(job_obj, dict):
            titulo = job_obj.get("title") or ""
            departamentos = job_obj.get("departments") or []
        else:
            titulo = str(job_obj)
            departamentos = []

        nome = f"{contato.get('firstName','')} {contato.get('lastName','')}".strip()
        social = contato.get("socialLinks") or {}
        linkedin = social.get("linkedin") or contato.get("linkedInUrl") or ""

        if not nome or not titulo:
            continue

        # Verifica por cargo OU departamento
        titulo_completo = titulo + " " + " ".join(departamentos)

        if not pessoa_fin and any(normalizar_texto(t) in normalizar_texto(titulo_completo) for t in termos_fin):
            pessoa_fin = {"nome_cargo": f"{nome} - {titulo}", "linkedin": linkedin,
                          "email": None, "telefone": None, "confianca_li": "alta",
                          "validado_lusha": True, "lusha_id": contato.get("id")}
        if not pessoa_rh and any(normalizar_texto(t) in normalizar_texto(titulo_completo) for t in termos_rh):
            pessoa_rh = {"nome_cargo": f"{nome} - {titulo}", "linkedin": linkedin,
                         "email": None, "telefone": None, "confianca_li": "alta",
                         "validado_lusha": True, "lusha_id": contato.get("id")}
        if pessoa_rh and pessoa_fin:
            break

    return pessoa_rh, pessoa_fin


# ─── NÍVEL 4 — Hunter.io ─────────────────────────────────────────────────────
def buscar_hunter_email(nome_completo: str, dominio: str) -> dict:
    if not HUNTER_API_KEY or not dominio or not nome_completo:
        return {}
    primeiro, ultimo = separar_nome(nome_completo)
    if not primeiro:
        return {}
    try:
        r = requests.get("https://api.hunter.io/v2/email-finder", params={
            "domain": dominio, "first_name": primeiro, "last_name": ultimo or primeiro,
            "api_key": HUNTER_API_KEY
        }, timeout=8)
        if r.status_code != 200:
            return {}
        dados = (r.json() or {}).get("data") or {}
        email = dados.get("email")
        if not email or (dados.get("score") or 0) < 50:
            return {}
        return {"email": email.lower(), "score": dados.get("score")}
    except Exception:
        return {}


# ─── NÍVEL 5 — PhantomBuster ──────────────────────────────────────────────────
def buscar_phantombuster_funcionarios(linkedin_empresa_url: str, tempo_maximo_s: int = 120) -> list:
    if not PHANTOMBUSTER_API_KEY or not PHANTOMBUSTER_AGENT_ID or not linkedin_empresa_url:
        return []
    headers = {"X-Phantombuster-Key": PHANTOMBUSTER_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post("https://api.phantombuster.com/api/v2/agents/launch",
                           headers=headers,
                           json={"id": PHANTOMBUSTER_AGENT_ID, "argument": {"companyUrl": linkedin_empresa_url}},
                           timeout=20)
        if r.status_code != 200:
            return []
        container_id = (r.json() or {}).get("containerId")
        if not container_id:
            return []
        decorridos, intervalo = 0, 5
        while decorridos < tempo_maximo_s:
            time.sleep(intervalo)
            decorridos += intervalo
            status_r = requests.get("https://api.phantombuster.com/api/v2/containers/fetch-output",
                                     headers=headers, params={"id": container_id}, timeout=20)
            if status_r.status_code != 200:
                continue
            status_dados = status_r.json() or {}
            if status_dados.get("status") == "finished":
                resultado = status_dados.get("resultObject")
                if isinstance(resultado, str):
                    try:
                        resultado = json.loads(resultado)
                    except Exception:
                        return []
                return resultado if isinstance(resultado, list) else []
        return []
    except Exception:
        return []


@app.route("/debug")
def debug():
    """Endpoint de diagnóstico — remove do código em produção."""
    import os
    serpapi_key = os.getenv("SERPAPI_KEY", "")
    lusha_key = os.getenv("LUSHA_API_KEY", "")

    resultado = {
        "serpapi_configurado": bool(serpapi_key),
        "lusha_configurado": bool(lusha_key),
        "serpapi_key_prefixo": serpapi_key[:8] + "..." if serpapi_key else "VAZIO",
        "lusha_key_prefixo": lusha_key[:8] + "..." if lusha_key else "VAZIO",
    }

    # Testa SerpAPI com TOTVS — versão corrigida com filtro de /goto
    try:
        r = requests.get("https://serpapi.com/search", params={
            "q": "TOTVS gerente financeiro linkedin",
            "api_key": serpapi_key,
            "engine": "google",
            "num": 5,
            "hl": "pt",
            "gl": "br",
            "safe": "off"
        }, timeout=8)
        todos_links = [item.get("link","") for item in r.json().get("organic_results", [])]
        links_validos = [l for l in todos_links if not l.startswith("/goto")]
        resultado["serpapi_teste_totvs"] = todos_links[:5]
        resultado["serpapi_links_validos"] = links_validos
        resultado["serpapi_status"] = r.status_code
    except Exception as e:
        resultado["serpapi_erro"] = str(e)

    # Testa Lusha com POST correto
    try:
        r = requests.post(
            "https://api.lusha.com/v3/contacts/decision-makers",
            headers={"api_key": lusha_key, "Content-Type": "application/json"},
            json={"companies": [{"domain": "totvs.com"}]},
            timeout=8)
        resultado["lusha_status"] = r.status_code
        resultado["lusha_resposta"] = str(r.json())[:800]
    except Exception as e:
        resultado["lusha_erro"] = str(e)

    return jsonify(resultado)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/buscar", methods=["POST"])
def buscar_lead():
    data = request.json
    entrada = data.get("empresa", "").strip()
    contatos_hubspot_bruto = parse_lista_contatos(data.get("contatos_hubspot"))

    if not entrada:
        return jsonify({"erro": "Nome, site ou CNPJ é obrigatório"}), 400

    contatos_hubspot_norm = {normalizar_contato(c) for c in contatos_hubspot_bruto}
    hubspot_fingerprint = fingerprint_contatos(contatos_hubspot_bruto)

    chave = chave_cache(entrada)
    cache_hit = buscar_no_cache(chave, hubspot_fingerprint)

    if cache_hit:
        resultado = dict(cache_hit)
        resultado["veio_do_cache"] = True
    else:
        try:
            emails_fontes, emails_score = {}, {}
            telefones_fontes = {}
            emails_brutos, telefones_brutos = set(), set()

            def registrar_email(email, fonte, score=None) -> bool:
                if not email:
                    return False
                email = email.lower().strip()
                emails_brutos.add(email)
                if email in contatos_hubspot_norm:
                    return False
                emails_fontes.setdefault(email, set()).add(fonte)
                if score is not None and email not in emails_score:
                    emails_score[email] = {"valor": score, "fonte": fonte}
                return True

            def registrar_telefone(telefone, fonte) -> bool:
                if not telefone:
                    return False
                chave_tel = normalizar_telefone(telefone)
                telefones_brutos.add(chave_tel)
                if chave_tel in contatos_hubspot_norm:
                    return False
                if chave_tel not in telefones_fontes:
                    telefones_fontes[chave_tel] = {"display": telefone, "fontes": set()}
                telefones_fontes[chave_tel]["fontes"].add(fonte)
                return True

            site, empresa_nome, fonte_receita = None, entrada, False
            linkedin_empresa, pessoa_rh, pessoa_fin = None, None, None
            niveis_usados, socios = [], []
            razao_social = ""
            email_receita, telefone_receita = None, None
            telefones_site, emails_site = [], []

            termos_rh = ["RH","Recursos Humanos","Gerente de RH","Diretor de RH","Head de RH",
                         "HR","Human Resources","People","People Ops","Talent","Head of People",
                         "People and Culture","Head de Pessoas","Gerente de Pessoas"]
            termos_rh_analista = ["Analista de RH","Analista de Recursos Humanos","Analista de People",
                                   "Analista de Gente","Especialista de RH","Coordenador de RH",
                                   "Coordenador de Pessoas","BP de RH","Business Partner"]
            termos_fin = ["Financeiro","CFO","Diretor Financeiro","Gerente Financeiro","Controller",
                          "Chief Financial Officer","Finance","VP Finance","Head of Finance",
                          "Tesouraria","Tesoureiro","Controladoria","Head Financeiro",
                          "Gerente de Tesouraria","Diretor de Finanças"]
            termos_fin_analista = ["Analista Financeiro","Analista de Controladoria","Analista de Tesouraria",
                                    "Especialista Financeiro","Coordenador Financeiro","Analista de Finanças",
                                    "Analista Contábil","Coordenador de Controladoria"]

            # ═══ NÍVEL 0 — Receita + site (grátis) ═══
            if eh_cnpj(entrada):
                dados = buscar_receita(entrada)
                if dados:
                    niveis_usados.append(f"receita:{dados.get('fonte_cadastro','?')}")
                    empresa_nome = dados.get("nome_fantasia") or entrada
                    razao_social = dados.get("razao_social") or ""
                    fonte_receita = True
                    socios = dados.get("socios", [])
                    if dados.get("telefone"):
                        telefone_receita = dados["telefone"]
                        registrar_telefone(telefone_receita, "receita")
                    for tel_extra in dados.get("telefones_extras", []):
                        registrar_telefone(tel_extra, "receita")
                    if dados.get("email"):
                        email_receita = dados["email"].lower()
                        registrar_email(email_receita, "receita")
                    for em_extra in dados.get("emails_extras", []):
                        registrar_email(em_extra.lower(), "receita")

            termo_busca = empresa_nome if empresa_nome != entrada else entrada

            # domínio de referência para filtrar emails — prioriza o site descoberto,
            # mas se a entrada já era um domínio (ex: blip.ai), usa ela diretamente
            dominio_referencia_email = ""
            if parece_dominio(entrada):
                dominio_referencia_email = entrada.strip().lower().replace("www.", "")

            site = descobrir_site(termo_busca)
            if site:
                extra = extrair_emails_telefones_do_site(site)
                emails_site = extra["emails"]
                telefones_site = extra["telefones"]
                for e in emails_site:
                    registrar_email(e, "site")
                for t in telefones_site:
                    registrar_telefone(t, "site")

            dominio_site = extrair_dominio_de_url(site) if site else ""
            # domínio final para comparação de emails (site tem prioridade sobre entrada)
            dominio_email_ref = dominio_site or dominio_referencia_email

            # ═══ NÍVEL 1 — SerpAPI ═══
            if not dados_completos(pessoa_rh, pessoa_fin) and pode_usar("serpapi"):
                r1 = buscar_serpapi(f'"{termo_busca}" telefone contato email')
                registrar_uso("serpapi")
                niveis_usados.append("serpapi")
                texto1 = texto_resultados(r1)
                padrao_email = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
                for e in re.findall(padrao_email, texto1):
                    if normalizar_texto(termo_busca)[:6] in normalizar_texto(e):
                        registrar_email(e, "serpapi")
                for t in re.findall(r'\(\d{2}\)\s?\d{4,5}-?\d{4}', texto1):
                    if telefone_plausivel(t):
                        registrar_telefone(t, "serpapi")

                # Busca LinkedIn da empresa — DuckDuckGo (grátis, sem problema de /goto)
                if not linkedin_empresa:
                    try:
                        r_ddg = requests.get(
                            "https://html.duckduckgo.com/html/",
                            params={"q": f"{termo_busca} site:linkedin.com/company"},
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                            timeout=8
                        )
                        links_li = re.findall(
                            r'href="(https?://[^"]*linkedin\.com/company/[^"?]+)"',
                            r_ddg.text
                        )
                        if links_li:
                            linkedin_empresa = links_li[0].split("?")[0]
                    except Exception:
                        pass

                # Fallback SerpAPI se DuckDuckGo não achou
                if not linkedin_empresa and pode_usar("serpapi"):
                    r_li = buscar_serpapi(f'{termo_busca} linkedin company page')
                    registrar_uso("serpapi")
                    linkedin_empresa = extrair_linkedin_empresa(r_li)

                # Busca de decisores — usa DuckDuckGo se SerpAPI não retornar LinkedIn
                if not pessoa_completa(pessoa_rh) and pode_usar("serpapi"):
                    r_rh, usou_serpapi = buscar_linkedin_pessoa(f'{termo_busca} gerente de RH')
                    if usou_serpapi:
                        registrar_uso("serpapi")
                    if not r_rh or not any("linkedin.com/in/" in r.get("link","") for r in r_rh):
                        r_rh, usou_serpapi2 = buscar_linkedin_pessoa(f'{termo_busca} head de pessoas recursos humanos linkedin')
                        if usou_serpapi2:
                            registrar_uso("serpapi")
                    pessoa_rh = None
                    if GEMINI_API_KEY and pode_usar("gemini"):
                        pessoa_rh = escolher_linkedin_via_gemini(r_rh, termo_busca, "RH")
                        registrar_uso("gemini")
                    if not pessoa_rh:
                        pessoa_rh = extrair_pessoa_linkedin_de_resultados(r_rh, termo_busca, termos_rh)
                    if pessoa_rh and LUSHA_API_KEY:
                        pessoa_rh = validar_decisor_com_lusha(pessoa_rh, termo_busca)
                        if pessoa_rh:
                            niveis_usados.append("lusha:validacao")
                    if not pessoa_rh:
                        r_rh_an, usou_sa = buscar_linkedin_pessoa(f'{termo_busca} analista coordenador RH linkedin')
                        if usou_sa:
                            registrar_uso("serpapi")
                        candidato_an = extrair_pessoa_linkedin_de_resultados(
                            r_rh_an, termo_busca, termos_rh + termos_rh_analista, aceitar_analista=True)
                        if candidato_an and LUSHA_API_KEY:
                            candidato_an = validar_decisor_com_lusha(candidato_an, termo_busca)
                        pessoa_rh = candidato_an

                if not pessoa_completa(pessoa_fin) and pode_usar("serpapi"):
                    r_fin, usou_serpapi = buscar_linkedin_pessoa(f'{termo_busca} gerente financeiro')
                    if usou_serpapi:
                        registrar_uso("serpapi")
                    if not r_fin or not any("linkedin.com/in/" in r.get("link","") for r in r_fin):
                        r_fin, usou_serpapi2 = buscar_linkedin_pessoa(f'{termo_busca} CFO diretor financeiro controller linkedin')
                        if usou_serpapi2:
                            registrar_uso("serpapi")
                    pessoa_fin = None
                    if GEMINI_API_KEY and pode_usar("gemini"):
                        pessoa_fin = escolher_linkedin_via_gemini(r_fin, termo_busca, "Financeiro")
                        registrar_uso("gemini")
                    if not pessoa_fin:
                        pessoa_fin = extrair_pessoa_linkedin_de_resultados(r_fin, termo_busca, termos_fin)
                    if pessoa_fin and LUSHA_API_KEY:
                        pessoa_fin = validar_decisor_com_lusha(pessoa_fin, termo_busca)
                        if pessoa_fin:
                            niveis_usados.append("lusha:validacao")
                    if not pessoa_fin:
                        r_fin_an, usou_sa = buscar_linkedin_pessoa(f'{termo_busca} analista coordenador financeiro linkedin')
                        if usou_sa:
                            registrar_uso("serpapi")
                        candidato_an = extrair_pessoa_linkedin_de_resultados(
                            r_fin_an, termo_busca, termos_fin + termos_fin_analista, aceitar_analista=True)
                        if candidato_an and LUSHA_API_KEY:
                            candidato_an = validar_decisor_com_lusha(candidato_an, termo_busca)
                        pessoa_fin = candidato_an

            # ═══ NÍVEL 2 — Apify ═══
            if not dados_completos(pessoa_rh, pessoa_fin) and linkedin_empresa and pode_usar("apify"):
                funcionarios = buscar_apify_funcionarios(linkedin_empresa)
                if funcionarios:
                    registrar_uso("apify")
                    niveis_usados.append("apify")
                    if not pessoa_completa(pessoa_fin):
                        candidato = filtrar_cargo_na_lista(funcionarios, termos_fin)
                        if candidato:
                            pessoa_fin = candidato
                    if not pessoa_completa(pessoa_rh):
                        candidato = filtrar_cargo_na_lista(funcionarios, termos_rh)
                        if candidato:
                            pessoa_rh = candidato
                    for pessoa in (pessoa_rh, pessoa_fin):
                        if pessoa and pessoa.get("email") and not registrar_email(pessoa["email"], "apify"):
                            pessoa["email"] = None

            # ═══ NÍVEL 3 — Lusha Decision Makers ═══
            # Só entra aqui se ainda não temos decisores após Google + Apify
            # A validação individual já foi feita inline no nível 1
            if LUSHA_API_KEY and not dados_completos(pessoa_rh, pessoa_fin) and pode_usar("lusha"):
                niveis_usados.append("lusha:decision-makers")

                # Enriquece quem foi achado pelo Apify mas sem contato
                for pessoa in [pessoa_rh, pessoa_fin]:
                    if pessoa and not pessoa_completa(pessoa) and pessoa.get("linkedin") and pode_usar("lusha"):
                        achou = buscar_lusha_enrich(pessoa["linkedin"])
                        registrar_uso("lusha")
                        if achou.get("email") and registrar_email(achou["email"], "lusha"):
                            pessoa["email"] = achou["email"]
                        if achou.get("telefone") and registrar_telefone(achou["telefone"], "lusha"):
                            pessoa["telefone"] = achou["telefone"]

                # Decision Makers pelo domínio quando não achamos ninguém ainda
                if dominio_site and (not pessoa_rh or not pessoa_fin) and pode_usar("lusha"):
                    dados_lusha = buscar_lusha_decisores(dominio_site)
                    registrar_uso("lusha")
                    if dados_lusha:
                        rh_lusha, fin_lusha = processar_lusha_decisores(dados_lusha, termos_rh, termos_fin)
                        if not pessoa_rh and rh_lusha:
                            pessoa_rh = rh_lusha
                            if pessoa_rh.get("email"):
                                registrar_email(pessoa_rh["email"], "lusha")
                            if pessoa_rh.get("telefone"):
                                registrar_telefone(pessoa_rh["telefone"], "lusha")
                        if not pessoa_fin and fin_lusha:
                            pessoa_fin = fin_lusha
                            if pessoa_fin.get("email"):
                                registrar_email(pessoa_fin["email"], "lusha")
                            if pessoa_fin.get("telefone"):
                                registrar_telefone(pessoa_fin["telefone"], "lusha")

                        # Extrai LinkedIn da empresa da resposta da Lusha se ainda não temos
                        if not linkedin_empresa:
                            try:
                                resultados_lusha = dados_lusha.get("results") or []
                                for res in resultados_lusha:
                                    comp = res.get("company") or {}
                                    comp_domain = comp.get("domain") or dominio_site
                                    # Monta URL do LinkedIn da empresa a partir do domínio
                                    slug = comp_domain.replace("www.", "").split(".")[0]
                                    if slug:
                                        linkedin_empresa = f"https://www.linkedin.com/company/{slug}"
                                        break
                            except Exception:
                                pass

            # ═══ NÍVEL 4 — Hunter.io ═══
            if HUNTER_API_KEY and not dados_completos(pessoa_rh, pessoa_fin) and dominio_site and pode_usar("hunter"):
                for pessoa in [pessoa_rh, pessoa_fin]:
                    if pessoa and not pessoa_completa(pessoa) and pode_usar("hunter"):
                        achou = buscar_hunter_email(pessoa["nome_cargo"], dominio_site)
                        registrar_uso("hunter")
                        niveis_usados.append("hunter")
                        if achou.get("email") and registrar_email(achou["email"], "hunter", achou.get("score")):
                            pessoa["email"] = achou["email"]

            # ═══ NÍVEL 5 — PhantomBuster ═══
            if not dados_completos(pessoa_rh, pessoa_fin) and linkedin_empresa and pode_usar("phantombuster"):
                funcionarios = buscar_phantombuster_funcionarios(linkedin_empresa)
                if funcionarios:
                    registrar_uso("phantombuster")
                    niveis_usados.append("phantombuster")
                    if not pessoa_completa(pessoa_fin):
                        candidato = filtrar_cargo_na_lista(funcionarios, termos_fin)
                        if candidato:
                            pessoa_fin = candidato
                    if not pessoa_completa(pessoa_rh):
                        candidato = filtrar_cargo_na_lista(funcionarios, termos_rh)
                        if candidato:
                            pessoa_rh = candidato
                    for pessoa in (pessoa_rh, pessoa_fin):
                        if pessoa and pessoa.get("email") and not registrar_email(pessoa["email"], "phantombuster"):
                            pessoa["email"] = None

            # ── Classificação de confiança ────────────────────────────────
            emails_encontrados = list(emails_fontes.keys())
            emails_classificados, emails_nao_verificados = [], []
            for e in emails_encontrados[:8]:
                confianca = avaliar_confianca_email(e, dominio_email_ref, razao_social)
                fontes = emails_fontes[e]
                registro = {
                    "email": e,
                    "departamento": classificar_email_por_departamento(e),
                    "confianca": confianca["nivel"],
                    "motivo": confianca["motivo"],
                    "possivel_contador": confianca["possivel_contador"],
                    "fontes": sorted(fontes),
                    "status_confirmacao": "confirmado" if len(fontes) >= 2 else "não confirmado",
                    "score_verificacao": emails_score.get(e),
                    "origem": "receita_federal" if e == email_receita else ("site" if e in emails_site else "busca"),
                }
                if confianca["possivel_contador"]:
                    emails_nao_verificados.append(registro)
                else:
                    emails_classificados.append(registro)

            telefones_classificados = []
            for chave_tel in list(telefones_fontes.keys())[:5]:
                info = telefones_fontes[chave_tel]
                display, fontes = info["display"], info["fontes"]
                if chave_tel == normalizar_telefone(telefone_receita or ""):
                    origem = "receita"
                elif display in telefones_site:
                    origem = "site"
                else:
                    origem = "busca"
                conf_tel = avaliar_confianca_telefone(display, origem, telefones_site)
                telefones_classificados.append({
                    "telefone": display, "origem": origem,
                    "confianca": conf_tel["nivel"], "motivo": conf_tel["motivo"],
                    "fontes": sorted(fontes),
                    "status_confirmacao": "confirmado" if len(fontes) >= 2 else "não confirmado",
                })

            receita_email_de_contador = email_receita and parece_contador(email_receita)
            if receita_email_de_contador:
                for tc in telefones_classificados:
                    if tc["origem"] == "receita" and tc["confianca"] != "alta":
                        tc["confianca"] = "baixa"
                        tc["motivo"] = "cadastro da Receita aparenta ser do contador"

            sugestoes = sugerir_emails_departamentais(dominio_email_ref or site, [e["email"] for e in emails_classificados])
            todos_contatos_ja_existem = bool(
                (emails_brutos or telefones_brutos) and not emails_encontrados and not telefones_fontes
            )

            nao_encontrado = "Não encontrado em fonte pública"
            resultado = {
                "empresa": empresa_nome,
                "site": site or nao_encontrado,
                "telefones": [tc["telefone"] for tc in telefones_classificados],
                "emails": [{"email": ec["email"], "departamento": ec["departamento"]} for ec in emails_classificados],
                "telefones_detalhe": telefones_classificados,
                "emails_detalhe": emails_classificados,
                "emails_nao_verificados": emails_nao_verificados,
                "alerta_contador": bool(receita_email_de_contador or emails_nao_verificados),
                "emails_sugeridos": sugestoes,
                "socios": socios[:5],
                "fonte_receita_federal": fonte_receita,
                "linkedin_empresa": linkedin_empresa or nao_encontrado,
                "linkedin_rh": (pessoa_rh["nome_cargo"] + " (a confirmar)") if pessoa_rh else nao_encontrado,
                "linkedin_rh_url": pessoa_rh.get("linkedin") if pessoa_rh else None,
                "rh_email": pessoa_rh.get("email") if pessoa_rh else None,
                "rh_telefone": pessoa_rh.get("telefone") if pessoa_rh else None,
                "linkedin_financeiro": (pessoa_fin["nome_cargo"] + " (a confirmar)") if pessoa_fin else nao_encontrado,
                "linkedin_financeiro_url": pessoa_fin.get("linkedin") if pessoa_fin else None,
                "financeiro_email": pessoa_fin.get("email") if pessoa_fin else None,
                "financeiro_telefone": pessoa_fin.get("telefone") if pessoa_fin else None,
                "todos_contatos_ja_existem": todos_contatos_ja_existem,
                "niveis_usados": list(dict.fromkeys(niveis_usados)),
                "veio_do_cache": False
            }
            salvar_no_cache(chave, resultado, hubspot_fingerprint)

        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    quotas = ler_quotas()
    resultado["quotas"] = {f: {"usos": quotas[f]["usos"], "limite": LIMITES_MES[f]} for f in LIMITES_MES}
    return jsonify(resultado)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
