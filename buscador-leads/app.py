import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY  = os.getenv("SERPAPI_KEY")
LUSHA_API_KEY = os.getenv("LUSHA_API_KEY")

CACHE_ARQUIVO  = "cache_empresas.json"
QUOTA_ARQUIVO  = "quota_uso.json"
CACHE_VALIDADE_DIAS = 30

LIMITES_MES = {
    "serpapi": 90,
    "lusha":   60,
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

def buscar_no_cache(chave: str, hubspot_fp: str) -> dict:
    cache = ler_cache()
    entrada = cache.get(chave)
    if not entrada:
        return None
    if (time.time() - entrada.get("timestamp", 0)) / 86400 > CACHE_VALIDADE_DIAS:
        return None
    if entrada.get("hubspot_fingerprint", "") != hubspot_fp:
        return None
    return entrada.get("dados")

def salvar_no_cache(chave: str, dados: dict, hubspot_fp: str):
    cache = ler_cache()
    cache[chave] = {"timestamp": time.time(), "dados": dados, "hubspot_fingerprint": hubspot_fp}
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
    return ler_quotas().get(fonte, {}).get("usos", 0) < LIMITES_MES.get(fonte, 0)

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

def normalizar_contato(valor: str) -> str:
    valor = (valor or "").strip()
    return valor.lower() if "@" in valor else normalizar_telefone(valor)

def chave_cache(texto: str) -> str:
    return limpar_cnpj(texto) if eh_cnpj(texto) else normalizar_texto(texto)

def parse_lista_contatos(bruto) -> list:
    if not bruto:
        return []
    if isinstance(bruto, list):
        return [c for c in bruto if c and str(c).strip()]
    return [c for c in re.split(r'[,;\n]', str(bruto)) if c.strip()]

def fingerprint_contatos(contatos: list) -> str:
    return "|".join(sorted(set(normalizar_contato(c) for c in contatos)))

def extrair_dominio_de_url(url: str) -> str:
    if not url:
        return ""
    m = re.match(r'https?://(?:www\.)?([^/]+)', url.strip())
    return m.group(1).lower() if m else ""

def extrair_dominio_de_email(email: str) -> str:
    return email.split("@")[-1].lower().strip() if "@" in email else ""

def pessoa_completa(pessoa: dict) -> bool:
    if not pessoa:
        return False
    return bool(pessoa.get("nome_cargo")) and bool(pessoa.get("email") or pessoa.get("telefone"))

def dados_completos(rh: dict, fin: dict) -> bool:
    return pessoa_completa(rh) and pessoa_completa(fin)

TLDS_CONHECIDOS = ['.com.br','.com','.com.ar','.net.br','.net','.org.br','.org',
                   '.ai','.io','.co','.app','.tech','.digital','.online','.store','.cloud']

def parece_dominio(texto: str) -> bool:
    texto = texto.strip()
    return " " not in texto and bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*(\.[a-zA-Z0-9-]+)+$', texto))

def remover_tld(empresa: str) -> str:
    empresa_lower = empresa.lower().strip()
    for tld in sorted(TLDS_CONHECIDOS, key=len, reverse=True):
        if empresa_lower.endswith(tld):
            return empresa_lower[:-len(tld)]
    return empresa_lower

PREFIXOS_DEPARTAMENTO = {
    "financeiro": ["financeiro","contas","cobranca","billing","faturamento"],
    "rh":         ["rh","recursoshumanos","recrutamento","vagas","talentos","people"],
    "compras":    ["compras","suprimentos","procurement","fornecedores"],
    "comercial":  ["comercial","vendas","sales","atendimento","contato"],
}

def classificar_email_por_departamento(email: str) -> str:
    usuario = email.split("@")[0].lower()
    for depto, prefixos in PREFIXOS_DEPARTAMENTO.items():
        if any(p == usuario or usuario.startswith(p+".") or usuario.startswith(p+"-") for p in prefixos):
            return depto
    return "geral"

DOMINIOS_GENERICOS = {"gmail.com","hotmail.com","outlook.com","yahoo.com","yahoo.com.br",
                       "uol.com.br","bol.com.br","terra.com.br","ig.com.br","live.com","icloud.com"}
PADROES_CONTADOR   = ["contab","contabil","contadores","contador","assessoria","escritorio",
                       "fiscal","tributar","bpo","consultoria","accounting","despachante"]

def parece_contador(texto: str) -> bool:
    t = normalizar_texto(texto)
    return any(p in t for p in PADROES_CONTADOR)

def avaliar_confianca_email(email: str, dominio_site: str) -> str:
    dom = extrair_dominio_de_email(email)
    if parece_contador(email):
        return "baixa"
    if dominio_site and dom == dominio_site:
        return "alta"
    if dom in DOMINIOS_GENERICOS:
        return "media"
    return "baixa"

DDDS_VALIDOS = {
    '11','12','13','14','15','16','17','18','19','21','22','24','27','28',
    '31','32','33','34','35','37','38','41','42','43','44','45','46','47','48','49',
    '51','53','54','55','61','62','63','64','65','66','67','68','69',
    '71','73','74','75','77','79','81','82','83','84','85','86','87','88','89',
    '91','92','93','94','95','96','97','98','99',
}

def telefone_plausivel(tel: str) -> bool:
    d = re.sub(r'\D', '', tel)
    if d.startswith('55') and len(d) > 11:
        d = d[2:]
    if len(d) not in (10, 11):
        return False
    return d[:2] in DDDS_VALIDOS


# ─── NÍVEL 0 — Receita Federal (grátis, 3 fontes) ───────────────────────────
def buscar_brasilapi(cnpj: str) -> dict:
    try:
        r = requests.get(f"https://brasilapi.com.br/api/cnpj/v1/{limpar_cnpj(cnpj)}",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return {}
        d = r.json()
        tel = None
        if d.get("ddd_telefone_1"):
            t = d["ddd_telefone_1"]
            tel = t if "(" in t else f"({t[:2]}) {t[2:]}"
        return {"razao_social": d.get("razao_social"),
                "nome_fantasia": d.get("nome_fantasia") or d.get("razao_social"),
                "telefone": tel, "email": d.get("email"),
                "socios": [s.get("nome_socio") for s in d.get("qsa",[]) if s.get("nome_socio")]}
    except Exception:
        return {}

def buscar_cnpja(cnpj: str) -> dict:
    try:
        r = requests.get(f"https://open.cnpja.com/office/{limpar_cnpj(cnpj)}",
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return {}
        d = r.json()
        company = d.get("company") or {}
        tels = []
        for ph in (d.get("phones") or []):
            area, num = ph.get("area",""), ph.get("number","")
            if area and num:
                fmt = f"({area}) {num[:5]}-{num[5:]}" if len(num)==9 else f"({area}) {num[:4]}-{num[4:]}"
                if telefone_plausivel(fmt):
                    tels.append(fmt)
        emails = [e.get("address","").lower() for e in (d.get("emails") or []) if e.get("address")]
        return {"razao_social": company.get("name"),
                "nome_fantasia": d.get("alias") or company.get("name"),
                "telefone": tels[0] if tels else None,
                "telefones_extras": tels[1:],
                "email": emails[0] if emails else None,
                "socios": [m.get("person",{}).get("name") for m in (company.get("members") or []) if m.get("person",{}).get("name")]}
    except Exception:
        return {}

def buscar_receitaws(cnpj: str) -> dict:
    try:
        r = requests.get(f"https://receitaws.com.br/v1/cnpj/{limpar_cnpj(cnpj)}", timeout=10)
        if r.status_code != 200 or r.json().get("status") == "ERROR":
            return {}
        d = r.json()
        return {"razao_social": d.get("nome"),
                "nome_fantasia": d.get("fantasia") or d.get("nome"),
                "telefone": d.get("telefone") or None,
                "email": (d.get("email") or "").lower() or None,
                "socios": [s.get("nome") for s in d.get("qsa",[]) if s.get("nome")]}
    except Exception:
        return {}

def buscar_receita(cnpj: str) -> dict:
    for fn in [buscar_brasilapi, buscar_cnpja, buscar_receitaws]:
        dados = fn(cnpj)
        if dados:
            return dados
    return {}


# ─── NÍVEL 0 — Site oficial (grátis) ─────────────────────────────────────────
def gerar_variacoes_slug(empresa: str) -> list:
    if parece_dominio(empresa):
        slug = normalizar_texto(remover_tld(empresa))
        return [slug] if slug else []
    palavras = re.sub(r'[^a-zA-Z0-9\s]', '', empresa).split()
    ignorar = {'ltda','sa','eireli','me','epp','equipamentos','comercio','industria',
               'servicos','solucoes','grupo','brasil','lojas','cia','companhia','rede'}
    uteis = [p for p in palavras if p.lower() not in ignorar]
    slugs = []
    if uteis:
        slugs.append(normalizar_texto(uteis[0]))
    if len(uteis) >= 2:
        slugs.append(normalizar_texto(uteis[0]+uteis[1]))
    slugs.append(normalizar_texto(empresa))
    return list(dict.fromkeys(slugs))

TLDS_TENTATIVA = ['.com.br','.com','.ai','.io','.co','.net','.app','.tech','.digital']
SINAIS_INVALIDA = ["just a moment","enable javascript","domain is for sale","buy this domain","parked domain"]

def validar_site(url: str, termo: str) -> str:
    try:
        r = requests.get(url, timeout=6, allow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        if r.status_code >= 400:
            return None
        if any(s in r.text[:5000].lower() for s in SINAIS_INVALIDA):
            return None
        if not termo or termo in normalizar_texto(r.text[:10000]):
            return r.url
    except Exception:
        pass
    return None

def descobrir_site(empresa: str) -> str:
    if parece_dominio(empresa):
        for url in [f"https://www.{empresa}", f"https://{empresa}"]:
            achado = validar_site(url, None)
            if achado:
                return achado

    for slug in gerar_variacoes_slug(empresa):
        if len(slug) < 3:
            continue
        for tld in TLDS_TENTATIVA:
            for url in [f"https://www.{slug}{tld}", f"https://{slug}{tld}"]:
                achado = validar_site(url, slug[:4])
                if achado:
                    return achado

    # Fallback DuckDuckGo
    try:
        r = requests.get("https://html.duckduckgo.com/html/",
                          params={"q": f"{empresa} site oficial"},
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                          timeout=8)
        links = re.findall(r'href="(https?://[^"]+)"', r.text)
        bloqueados = ["duckduckgo","linkedin","facebook","instagram","youtube","indeed",
                      "glassdoor","wikipedia","google","cnpj","consultas","datanyze"]
        primeira = normalizar_texto(empresa.split()[0]) if empresa.split() else ""
        for link in links:
            if any(b in link.lower() for b in bloqueados):
                continue
            if primeira and len(primeira) >= 4 and primeira in normalizar_texto(link):
                m = re.match(r'https?://(?:www\.)?([^/]+)', link)
                if m:
                    return f"https://{m.group(1)}"
    except Exception:
        pass
    return None

def extrair_contatos_do_site(url_base: str) -> dict:
    paginas = ["","/contato","/fale-conosco","/sobre","/atendimento","/contact",
               "/contatos","/quem-somos","/institucional","/financeiro","/fornecedores"]
    emails, telefones, whatsapp = [], [], []
    for pagina in paginas:
        try:
            r = requests.get(url_base.rstrip("/")+pagina,
                              headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                              timeout=6)
            if r.status_code != 200:
                continue
            texto = r.text
            achados = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', texto)
            ignorar = ['png','jpg','jpeg','gif','svg','webp','sentry','wixpress',
                       '.css','.js','example','schema.org','w3.org','gravatar']
            emails += [e.lower() for e in achados if not any(i in e.lower() for i in ignorar)]
            for p in [r'\(\d{2}\)\s?\d{4,5}-?\d{4}', r'\+55\s?\d{2}\s?\d{4,5}[-\s]?\d{4}',
                      r'0800\s?\d{3}\s?\d{4}']:
                candidatos = re.findall(p, texto)
                telefones += [t for t in candidatos if t.startswith('0800') or telefone_plausivel(t)]
            # WhatsApp — fica separado dos telefones, não misturado
            wa = re.findall(r'(?:wa\.me/|api\.whatsapp\.com/send\?phone=)(\+?55\d{10,11})', texto)
            for num in wa:
                num = re.sub(r'\D','',num)
                if num.startswith('55'):
                    num = num[2:]
                if telefone_plausivel(num):
                    ddd, resto = num[:2], num[2:]
                    whatsapp.append(f"({ddd}) {resto[:5]}-{resto[5:]}" if len(resto)==9
                                     else f"({ddd}) {resto[:4]}-{resto[4:]}")
        except Exception:
            continue
    return {"emails": list(dict.fromkeys(emails))[:5],
            "telefones": list(dict.fromkeys(telefones))[:5],
            "whatsapp": list(dict.fromkeys(whatsapp))[:5]}

def sugerir_emails_departamentais(dominio: str, emails_confirmados: list) -> list:
    if not dominio:
        return []
    dominio_limpo = re.sub(r'https?://(www\.)?','',dominio).rstrip('/')
    confirmados = [e.split("@")[0].lower() for e in emails_confirmados]
    return [{"departamento": d, "email_sugerido": f"{p}@{dominio_limpo}"}
            for d, p in {"financeiro":"financeiro","rh":"rh","compras":"compras"}.items()
            if p not in confirmados]


# ─── NÍVEL 1 — SerpAPI + DuckDuckGo ──────────────────────────────────────────
# Quando o Google retorna um knowledge panel em vez de resultados orgânicos "puros",
# o SerpAPI às vezes devolve links internos de navegação (ex.: "/goto?url=...", "/url?q=...",
# páginas de cache) em vez do destino real. Esses links nunca são úteis, então descartamos
# qualquer coisa que não seja uma URL absoluta apontando para fora do próprio Google.
HOSTS_INTERNOS_GOOGLE = ("google.com", "google.com.br", "webcache.googleusercontent.com")

def link_valido_serp(link: str) -> bool:
    link = (link or "").strip()
    if not link.startswith(("http://", "https://")):
        return False
    dominio = extrair_dominio_de_url(link)
    return not any(dominio == h or dominio.endswith("." + h) for h in HOSTS_INTERNOS_GOOGLE)

def buscar_serpapi(query: str) -> list:
    if not SERPAPI_KEY:
        return []
    try:
        r = requests.get("https://serpapi.com/search", params={
            "q": query, "api_key": SERPAPI_KEY, "engine": "google",
            "num": 5, "hl": "pt", "gl": "br", "safe": "off"
        }, timeout=8)
        results = r.json().get("organic_results", [])
        return [res for res in results if link_valido_serp(res.get("link",""))]
    except Exception:
        return []

def buscar_duckduckgo(query: str) -> list:
    try:
        r = requests.get("https://html.duckduckgo.com/html/",
                          params={"q": query},
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                          timeout=8)
        if r.status_code != 200:
            return []
        links  = re.findall(r'class="result__url"[^>]*>([^<]+)<', r.text)
        titles = re.findall(r'class="result__a"[^>]*>([^<]+)<', r.text)
        snips  = re.findall(r'class="result__snippet"[^>]*>([^<]+)<', r.text)
        results = []
        for i, link in enumerate(links[:5]):
            url = link.strip()
            if not url.startswith("http"):
                url = "https://" + url
            results.append({"link": url,
                             "title": titles[i].strip() if i < len(titles) else "",
                             "snippet": snips[i].strip() if i < len(snips) else ""})
        return results
    except Exception:
        return []

def buscar_linkedin_pessoa(query: str) -> tuple:
    """Retorna (resultados, usou_serpapi)"""
    if SERPAPI_KEY and pode_usar("serpapi"):
        r = buscar_serpapi(query)
        if any("linkedin.com/in/" in x.get("link","") for x in r):
            return r, True
    return buscar_duckduckgo(query), False

def extrair_linkedin_empresa(resultados: list) -> str:
    for r in resultados:
        if "linkedin.com/company/" in r.get("link",""):
            return r["link"].split("?")[0]
    return None

def texto_resultados(resultados: list) -> str:
    return " ".join([(r.get("title","")+" "+r.get("snippet","")+" "+r.get("link","")) for r in resultados])

def extrair_pessoa_linkedin(resultados: list, empresa: str, termos: list, aceitar_analista: bool=False) -> dict:
    empresa_norm  = normalizar_texto(empresa)
    empresa_lower = empresa.lower()
    padroes = [f" na {empresa_lower}", f" at {empresa_lower}", f"| {empresa_lower}", f"- {empresa_lower}"]

    # Estratégia 1: "na Empresa" no título + cargo
    for r in resultados[:5]:
        link = r.get("link","")
        if "linkedin.com/in/" not in link:
            continue
        titulo = r.get("title","")
        titulo_lower = titulo.lower()
        tem_empresa = any(p in titulo_lower for p in padroes) or empresa_norm[:6] in normalizar_texto(titulo)
        if not tem_empresa:
            continue
        if not any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos):
            continue
        partes = titulo.split(" | ")[0].split(" - ",1)
        nome = partes[0].strip()
        cargo = partes[1].strip() if len(partes)>1 else None
        if nome:
            return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome,
                    "linkedin": link, "email": None, "telefone": None, "confianca_li": "alta"}

    # Estratégia 2: empresa no snippet + cargo no título
    for r in resultados[:5]:
        link = r.get("link","")
        if "linkedin.com/in/" not in link:
            continue
        titulo = r.get("title","")
        snippet = r.get("snippet","")
        if not (empresa_norm[:6] in normalizar_texto(snippet)):
            continue
        if not any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos):
            continue
        partes = titulo.split(" | ")[0].split(" - ",1)
        nome = partes[0].strip()
        cargo = partes[1].strip() if len(partes)>1 else None
        if nome:
            return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome,
                    "linkedin": link, "email": None, "telefone": None, "confianca_li": "media"}

    # Estratégia 3: analista (só se aceitar_analista)
    if aceitar_analista:
        for r in resultados[:5]:
            link = r.get("link","")
            if "linkedin.com/in/" not in link:
                continue
            titulo = r.get("title","")
            snippet = r.get("snippet","")
            tem_empresa = (empresa_norm[:6] in normalizar_texto(titulo) or
                           empresa_norm[:6] in normalizar_texto(snippet))
            if not tem_empresa:
                continue
            if not any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos):
                continue
            partes = titulo.split(" | ")[0].split(" - ",1)
            nome = partes[0].strip()
            cargo = partes[1].strip() if len(partes)>1 else None
            if nome:
                return {"nome_cargo": f"{nome} - {cargo} ⚠️ analista" if cargo else nome,
                        "linkedin": link, "email": None, "telefone": None, "confianca_li": "baixa"}
    return None


# ─── NÍVEL 2 — Lusha ──────────────────────────────────────────────────────────
def lusha_decision_makers(dominio: str) -> dict:
    if not LUSHA_API_KEY or not dominio:
        return {}
    try:
        r = requests.post(
            "https://api.lusha.com/v3/contacts/decision-makers",
            headers={"api_key": LUSHA_API_KEY, "Content-Type": "application/json"},
            json={"companies": [{"domain": dominio}]},
            timeout=15
        )
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}

def lusha_search_enrich(linkedin_url: str) -> dict:
    if not LUSHA_API_KEY or not linkedin_url:
        return {}
    try:
        r = requests.get(
            "https://api.lusha.com/v3/contacts/search-and-enrich",
            headers={"api_key": LUSHA_API_KEY, "Content-Type": "application/json"},
            params={"linkedInUrl": linkedin_url},
            timeout=15
        )
        if r.status_code != 200:
            return {}
        dados = (r.json() or {}).get("data") or {}
        email = (dados.get("email") or "").lower() or None
        telefone = None
        whatsapp = None
        phones = dados.get("phones") or []
        if phones:
            raw = phones[0].get("internationalNumber") or phones[0].get("localNumber") or ""
            if raw and telefone_plausivel(raw):
                telefone = raw
                try:
                    num = re.sub(r'\D', '', raw)
                    if num.startswith('55'):
                        num = num[2:]
                    if telefone_plausivel(num):
                        ddd, resto = num[:2], num[2:]
                        whatsapp = f"({ddd}) {resto[:5]}-{resto[5:]}" if len(resto)==9 else f"({ddd}) {resto[:4]}-{resto[4:]}"
                except Exception:
                    pass
        empresa_atual = ((dados.get("positions") or [{}])[0].get("companyName") or
                          dados.get("companyName") or "").lower()
        return {"email": email, "telefone": telefone, "whatsapp": whatsapp, "empresa_atual": empresa_atual}
    except Exception:
        return {}

def lusha_validar(pessoa: dict, empresa_buscada: str) -> dict:
    """Valida se a pessoa realmente trabalha na empresa e enriquece com contatos."""
    if not pessoa or not pessoa.get("linkedin") or not LUSHA_API_KEY:
        return pessoa
    if not pode_usar("lusha"):
        return pessoa
    enrich = lusha_search_enrich(pessoa["linkedin"])
    registrar_uso("lusha")
    if not enrich:
        pessoa["validado_lusha"] = False
        return pessoa
    empresa_atual = enrich.get("empresa_atual","")
    empresa_norm  = normalizar_texto(empresa_buscada)[:8]
    if empresa_atual and empresa_norm and empresa_norm not in normalizar_texto(empresa_atual):
        return None  # pessoa errada — descarta
    if enrich.get("email"):
        pessoa["email"] = enrich["email"]
    if enrich.get("telefone"):
        pessoa["telefone"] = enrich["telefone"]
    pessoa["confianca_li"] = "alta"
    pessoa["validado_lusha"] = True
    return pessoa

def lusha_processar_decisores(dados: dict, termos_rh: list, termos_fin: list) -> tuple:
    pessoa_rh, pessoa_fin = None, None
    resultados = dados.get("results") or []
    contatos = []
    for res in resultados:
        contatos.extend(res.get("decisionMakers") or res.get("contacts") or [])
    if not contatos:
        contatos = dados.get("decisionMakers") or dados.get("contacts") or []

    for c in contatos:
        job_obj = c.get("jobTitle") or {}
        titulo = job_obj.get("title","") if isinstance(job_obj, dict) else str(job_obj)
        deptos = job_obj.get("departments",[]) if isinstance(job_obj, dict) else []
        nome   = f"{c.get('firstName','')} {c.get('lastName','')}".strip()
        social = c.get("socialLinks") or {}
        linkedin = social.get("linkedin") or c.get("linkedInUrl") or ""
        if not nome or not titulo:
            continue
        titulo_completo = titulo + " " + " ".join(deptos)
        if not pessoa_fin and any(normalizar_texto(t) in normalizar_texto(titulo_completo) for t in termos_fin):
            pessoa_fin = {"nome_cargo": f"{nome} - {titulo}", "linkedin": linkedin,
                          "email": None, "telefone": None, "confianca_li": "alta", "validado_lusha": True}
        if not pessoa_rh and any(normalizar_texto(t) in normalizar_texto(titulo_completo) for t in termos_rh):
            pessoa_rh = {"nome_cargo": f"{nome} - {titulo}", "linkedin": linkedin,
                         "email": None, "telefone": None, "confianca_li": "alta", "validado_lusha": True}
        if pessoa_rh and pessoa_fin:
            break
    return pessoa_rh, pessoa_fin


# ─── Debug ────────────────────────────────────────────────────────────────────
@app.route("/debug")
def debug():
    resultado = {
        "serpapi_configurado": bool(SERPAPI_KEY),
        "lusha_configurado": bool(LUSHA_API_KEY),
    }
    try:
        r = requests.get("https://serpapi.com/search", params={
            "q": "TOTVS gerente financeiro linkedin", "api_key": SERPAPI_KEY,
            "engine": "google", "num": 5, "hl": "pt", "gl": "br"
        }, timeout=8)
        links = [x.get("link","") for x in r.json().get("organic_results",[])]
        resultado["serpapi_links"] = links
        resultado["serpapi_links_validos"] = [l for l in links if link_valido_serp(l)]
    except Exception as e:
        resultado["serpapi_erro"] = str(e)
    try:
        r = requests.post("https://api.lusha.com/v3/contacts/decision-makers",
                           headers={"api_key": LUSHA_API_KEY, "Content-Type": "application/json"},
                           json={"companies": [{"domain": "totvs.com"}]}, timeout=8)
        resultado["lusha_status"] = r.status_code
        resultado["lusha_resposta"] = str(r.json())[:500]
    except Exception as e:
        resultado["lusha_erro"] = str(e)
    return jsonify(resultado)


@app.route("/limpar-cache", methods=["POST"])
def limpar_cache():
    """Permite ao BDR forçar uma nova busca sem precisar mexer no Render.
    POST {"empresa": "Totvs"} limpa só aquela empresa do cache.
    POST {"todos": true} limpa o cache inteiro."""
    data = request.json or {}
    entrada = (data.get("empresa") or "").strip()

    if entrada:
        cache = ler_cache()
        chave = chave_cache(entrada)
        existia = chave in cache
        if existia:
            del cache[chave]
            gravar_cache(cache)
        return jsonify({"removido": existia, "empresa": entrada})

    if data.get("todos") is True:
        total = len(ler_cache())
        gravar_cache({})
        return jsonify({"removido": True, "total_removido": total})

    return jsonify({"erro": "Informe 'empresa' para limpar uma empresa específica, ou 'todos': true para limpar tudo."}), 400


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/buscar", methods=["POST"])
def buscar_lead():
    data = request.json
    entrada = data.get("empresa","").strip()
    contatos_hubspot = parse_lista_contatos(data.get("contatos_hubspot"))

    if not entrada:
        return jsonify({"erro": "Nome, site ou CNPJ é obrigatório"}), 400

    contatos_norm = {normalizar_contato(c) for c in contatos_hubspot}
    hubspot_fp    = fingerprint_contatos(contatos_hubspot)
    chave         = chave_cache(entrada)
    cache_hit     = buscar_no_cache(chave, hubspot_fp)

    if cache_hit:
        resultado = dict(cache_hit)
        resultado["veio_do_cache"] = True
    else:
        try:
            emails_fontes   = {}
            telefones_fontes = {}
            whatsapp_fontes = {}

            def reg_email(email, fonte):
                if not email:
                    return
                email = email.lower().strip()
                if normalizar_contato(email) not in contatos_norm:
                    emails_fontes.setdefault(email, set()).add(fonte)

            def reg_tel(tel, fonte):
                if not tel:
                    return
                chave_tel = normalizar_telefone(tel)
                if chave_tel not in contatos_norm:
                    if chave_tel not in telefones_fontes:
                        telefones_fontes[chave_tel] = {"display": tel, "fontes": set()}
                    telefones_fontes[chave_tel]["fontes"].add(fonte)

            def reg_whatsapp(tel, fonte):
                if not tel:
                    return
                chave_tel = normalizar_telefone(tel)
                if chave_tel not in contatos_norm:
                    if chave_tel not in whatsapp_fontes:
                        whatsapp_fontes[chave_tel] = {"display": tel, "fontes": set()}
                    whatsapp_fontes[chave_tel]["fontes"].add(fonte)

            site, empresa_nome, fonte_receita = None, entrada, False
            linkedin_empresa, pessoa_rh, pessoa_fin = None, None, None
            socios, niveis_usados = [], []

            termos_rh  = ["RH","Recursos Humanos","Gerente de RH","Diretor de RH","Head de RH",
                           "HR","Human Resources","People","People Ops","Talent","Head of People",
                           "People and Culture","Head de Pessoas","Gerente de Pessoas"]
            termos_fin = ["Financeiro","CFO","Diretor Financeiro","Gerente Financeiro","Controller",
                           "Chief Financial Officer","Finance","Head of Finance",
                           "Tesouraria","Tesoureiro","Controladoria","Gerente de Tesouraria"]
            termos_rh_an  = ["Analista de RH","Analista de Recursos Humanos","Especialista de RH",
                              "Coordenador de RH","Coordenador de Pessoas","Business Partner"]
            termos_fin_an = ["Analista Financeiro","Analista de Controladoria","Especialista Financeiro",
                              "Coordenador Financeiro","Analista Contábil"]

            # ═══ NÍVEL 0 — Receita Federal + site ═══
            if eh_cnpj(entrada):
                dados = buscar_receita(entrada)
                if dados:
                    niveis_usados.append("receita")
                    empresa_nome = dados.get("nome_fantasia") or entrada
                    fonte_receita = True
                    socios = dados.get("socios", [])
                    reg_tel(dados.get("telefone"), "receita")
                    reg_email(dados.get("email"), "receita")

            termo_busca = empresa_nome if empresa_nome != entrada else entrada
            site = descobrir_site(termo_busca)
            dominio_ref = ""
            if parece_dominio(entrada):
                dominio_ref = entrada.strip().lower().replace("www.","")
            dominio_site = extrair_dominio_de_url(site) if site else ""
            dominio_email_ref = dominio_site or dominio_ref

            if site:
                extra = extrair_contatos_do_site(site)
                for e in extra["emails"]:
                    reg_email(e, "site")
                for t in extra["telefones"]:
                    reg_tel(t, "site")
                for w in extra["whatsapp"]:
                    reg_whatsapp(w, "site")
                emails_site    = extra["emails"]
                telefones_site = extra["telefones"]
            else:
                emails_site = telefones_site = []

            # ═══ NÍVEL 1 — SerpAPI + DuckDuckGo ═══
            if pode_usar("serpapi"):
                r1 = buscar_serpapi(f'"{termo_busca}" telefone contato email')
                registrar_uso("serpapi")
                niveis_usados.append("serpapi")
                for e in re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}',
                                    texto_resultados(r1)):
                    if dominio_email_ref and dominio_email_ref in e:
                        reg_email(e, "serpapi")

            # LinkedIn da empresa — DuckDuckGo primeiro (evita /goto)
            try:
                r_ddg = requests.get("https://html.duckduckgo.com/html/",
                                      params={"q": f"{termo_busca} site:linkedin.com/company"},
                                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                                      timeout=8)
                links_li = re.findall(r'href="(https?://[^"]*linkedin\.com/company/[^"?]+)"', r_ddg.text)
                if links_li:
                    linkedin_empresa = links_li[0].split("?")[0]
            except Exception:
                pass

            # Decisores — DuckDuckGo + validação Lusha
            r_rh, usou = buscar_linkedin_pessoa(f'{termo_busca} gerente de RH')
            if usou:
                registrar_uso("serpapi")
            if not any("linkedin.com/in/" in x.get("link","") for x in r_rh):
                r_rh2, usou2 = buscar_linkedin_pessoa(f'{termo_busca} head de pessoas RH linkedin')
                if usou2:
                    registrar_uso("serpapi")
                r_rh = r_rh2 if r_rh2 else r_rh
            pessoa_rh = extrair_pessoa_linkedin(r_rh, termo_busca, termos_rh)
            if pessoa_rh and LUSHA_API_KEY:
                pessoa_rh = lusha_validar(pessoa_rh, termo_busca)
                if pessoa_rh:
                    niveis_usados.append("lusha:validacao")
            if not pessoa_rh:
                r_rh_an, usou = buscar_linkedin_pessoa(f'{termo_busca} analista coordenador RH linkedin')
                if usou:
                    registrar_uso("serpapi")
                candidato = extrair_pessoa_linkedin(r_rh_an, termo_busca,
                                                     termos_rh+termos_rh_an, aceitar_analista=True)
                if candidato and LUSHA_API_KEY:
                    candidato = lusha_validar(candidato, termo_busca)
                pessoa_rh = candidato

            r_fin, usou = buscar_linkedin_pessoa(f'{termo_busca} gerente financeiro')
            if usou:
                registrar_uso("serpapi")
            if not any("linkedin.com/in/" in x.get("link","") for x in r_fin):
                r_fin2, usou2 = buscar_linkedin_pessoa(f'{termo_busca} CFO diretor financeiro linkedin')
                if usou2:
                    registrar_uso("serpapi")
                r_fin = r_fin2 if r_fin2 else r_fin
            pessoa_fin = extrair_pessoa_linkedin(r_fin, termo_busca, termos_fin)
            if pessoa_fin and LUSHA_API_KEY:
                pessoa_fin = lusha_validar(pessoa_fin, termo_busca)
                if pessoa_fin:
                    niveis_usados.append("lusha:validacao")
            if not pessoa_fin:
                r_fin_an, usou = buscar_linkedin_pessoa(f'{termo_busca} analista coordenador financeiro linkedin')
                if usou:
                    registrar_uso("serpapi")
                candidato = extrair_pessoa_linkedin(r_fin_an, termo_busca,
                                                     termos_fin+termos_fin_an, aceitar_analista=True)
                if candidato and LUSHA_API_KEY:
                    candidato = lusha_validar(candidato, termo_busca)
                pessoa_fin = candidato

            # ═══ NÍVEL 2 — Lusha Decision Makers ═══
            # Entra se ainda falta decisor OU se falta contato (email/tel) de quem foi achado
            if LUSHA_API_KEY and dominio_site and not dados_completos(pessoa_rh, pessoa_fin):
                dados_lusha = lusha_decision_makers(dominio_site)
                registrar_uso("lusha")
                niveis_usados.append("lusha:decision-makers")
                if dados_lusha:
                    rh_l, fin_l = lusha_processar_decisores(dados_lusha, termos_rh, termos_fin)
                    if not pessoa_rh and rh_l:
                        pessoa_rh = rh_l
                    if not pessoa_fin and fin_l:
                        pessoa_fin = fin_l
                    # Extrai LinkedIn da empresa da resposta da Lusha
                    if not linkedin_empresa:
                        try:
                            slug = dominio_site.replace("www.","").split(".")[0]
                            if slug:
                                linkedin_empresa = f"https://www.linkedin.com/company/{slug}"
                        except Exception:
                            pass

            # Enriquece contatos dos decisores encontrados + tenta nível analista se sem email
            rh_whatsapp_str = None
            fin_whatsapp_str = None
            if LUSHA_API_KEY:
                for pessoa_ref, pessoa_type in [(pessoa_rh, "rh"), (pessoa_fin, "fin")]:
                    if pessoa_ref and pessoa_ref.get("linkedin") and pode_usar("lusha"):
                        enrich = lusha_search_enrich(pessoa_ref["linkedin"])
                        registrar_uso("lusha")
                        if enrich.get("email"):
                            pessoa_ref["email"] = enrich["email"]
                            reg_email(enrich["email"], "lusha")
                        if enrich.get("telefone") and not pessoa_ref.get("telefone"):
                            pessoa_ref["telefone"] = enrich["telefone"]
                            reg_tel(enrich["telefone"], "lusha")
                        if enrich.get("whatsapp"):
                            if pessoa_type == "rh":
                                rh_whatsapp_str = enrich["whatsapp"]
                                reg_whatsapp(enrich["whatsapp"], "lusha")
                            elif pessoa_type == "fin":
                                fin_whatsapp_str = enrich["whatsapp"]
                                reg_whatsapp(enrich["whatsapp"], "lusha")

                # Tenta nível analista para email se gerente sem email
                if pessoa_rh and not pessoa_rh.get("email"):
                    r_rh_an2, usou = buscar_linkedin_pessoa(f'{termo_busca} analista rh linkedin')
                    if usou:
                        registrar_uso("serpapi")
                    analista_rh = extrair_pessoa_linkedin(r_rh_an2, termo_busca, termos_rh_an, aceitar_analista=False)
                    if analista_rh and analista_rh.get("linkedin") and pode_usar("lusha"):
                        enrich_an = lusha_search_enrich(analista_rh["linkedin"])
                        registrar_uso("lusha")
                        if enrich_an.get("email"):
                            pessoa_rh["email"] = enrich_an["email"]
                            reg_email(enrich_an["email"], "lusha")
                        if enrich_an.get("whatsapp") and not rh_whatsapp_str:
                            rh_whatsapp_str = enrich_an["whatsapp"]
                            reg_whatsapp(enrich_an["whatsapp"], "lusha")

                if pessoa_fin and not pessoa_fin.get("email"):
                    r_fin_an2, usou = buscar_linkedin_pessoa(f'{termo_busca} analista financeiro linkedin')
                    if usou:
                        registrar_uso("serpapi")
                    analista_fin = extrair_pessoa_linkedin(r_fin_an2, termo_busca, termos_fin_an, aceitar_analista=False)
                    if analista_fin and analista_fin.get("linkedin") and pode_usar("lusha"):
                        enrich_an = lusha_search_enrich(analista_fin["linkedin"])
                        registrar_uso("lusha")
                        if enrich_an.get("email"):
                            pessoa_fin["email"] = enrich_an["email"]
                            reg_email(enrich_an["email"], "lusha")
                        if enrich_an.get("whatsapp") and not fin_whatsapp_str:
                            fin_whatsapp_str = enrich_an["whatsapp"]
                            reg_whatsapp(enrich_an["whatsapp"], "lusha")

            # Registra emails/telefones dos decisores
            for pessoa in [pessoa_rh, pessoa_fin]:
                if pessoa:
                    if pessoa.get("email"):
                        reg_email(pessoa["email"], "lusha")
                    if pessoa.get("telefone"):
                        reg_tel(pessoa["telefone"], "lusha")

            # ── Classifica emails ──────────────────────────────────────────
            emails_classificados = []
            for e, fontes in list(emails_fontes.items())[:6]:
                confianca = avaliar_confianca_email(e, dominio_email_ref)
                emails_classificados.append({
                    "email": e,
                    "departamento": classificar_email_por_departamento(e),
                    "confianca": confianca,
                    "possivel_contador": parece_contador(e),
                    "fontes": sorted(fontes),
                })

            telefones_classificados = []
            for chave_tel, info in list(telefones_fontes.items())[:5]:
                display, fontes = info["display"], info["fontes"]
                origem = "site" if display in telefones_site else "busca"
                telefones_classificados.append({
                    "telefone": display, "fontes": sorted(fontes), "origem": origem
                })

            whatsapp_classificados = []
            for chave_w, info in list(whatsapp_fontes.items())[:5]:
                whatsapp_classificados.append({
                    "whatsapp": info["display"], "fontes": sorted(info["fontes"])
                })

            sugestoes = sugerir_emails_departamentais(
                dominio_email_ref or site,
                [e["email"] for e in emails_classificados]
            )

            nao_enc = "Não encontrado em fonte pública"
            resultado = {
                "empresa": empresa_nome,
                "site": site or nao_enc,
                "telefones": [t["telefone"] for t in telefones_classificados],
                "whatsapp": [w["whatsapp"] for w in whatsapp_classificados],
                "emails": [{"email": e["email"], "departamento": e["departamento"]} for e in emails_classificados],
                "telefones_detalhe": telefones_classificados,
                "whatsapp_detalhe": whatsapp_classificados,
                "emails_detalhe": emails_classificados,
                "emails_sugeridos": sugestoes,
                "socios": socios[:5],
                "fonte_receita_federal": fonte_receita,
                "linkedin_empresa": linkedin_empresa or nao_enc,
                "linkedin_rh": (pessoa_rh["nome_cargo"]+" (a confirmar)") if pessoa_rh else nao_enc,
                "linkedin_rh_url": pessoa_rh.get("linkedin") if pessoa_rh else None,
                "rh_email": pessoa_rh.get("email") if pessoa_rh else None,
                "rh_telefone": pessoa_rh.get("telefone") if pessoa_rh else None,
                "rh_whatsapp": rh_whatsapp_str or None,
                "linkedin_financeiro": (pessoa_fin["nome_cargo"]+" (a confirmar)") if pessoa_fin else nao_enc,
                "linkedin_financeiro_url": pessoa_fin.get("linkedin") if pessoa_fin else None,
                "financeiro_email": pessoa_fin.get("email") if pessoa_fin else None,
                "financeiro_telefone": pessoa_fin.get("telefone") if pessoa_fin else None,
                "financeiro_whatsapp": fin_whatsapp_str or None,
                "niveis_usados": list(dict.fromkeys(niveis_usados)),
                "veio_do_cache": False,
            }
            salvar_no_cache(chave, resultado, hubspot_fp)

        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    quotas = ler_quotas()
    resultado["quotas"] = {f: {"usos": quotas[f]["usos"], "limite": LIMITES_MES[f]} for f in LIMITES_MES}
    return jsonify(resultado)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
