import os
import re
import json
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY")
CLAY_API_KEY = os.getenv("CLAY_API_KEY")
PHANTOMBUSTER_API_KEY = os.getenv("PHANTOMBUSTER_API_KEY")
PHANTOMBUSTER_AGENT_ID = os.getenv("PHANTOMBUSTER_AGENT_ID")  # falta no .env — ver comentário em buscar_phantombuster_funcionarios
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_API_KEY}"

CACHE_ARQUIVO = "cache_empresas.json"
QUOTA_ARQUIVO = "quota_uso.json"
CACHE_VALIDADE_DIAS = 30

# Tetos com margem de segurança (abaixo do limite real de cada plano)
# hunter/clay/phantombuster: valores placeholder — ajuste conforme seu plano real
LIMITES_MES = {
    "serpapi": 90,          # plano free é 100/mês, deixamos 10 de colchão
    "apify": 40,            # dentro dos $5 grátis em créditos
    "hunter": 20,           # free tier real é 25/mês, deixamos margem
    "clay": 20,             # provisório até confirmar o plano contratado
    "phantombuster": 20,    # provisório — tem limite de slots simultâneos também, não tratado aqui ainda
    "gemini": 40,            # dentro do tier gratuito real da API
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


def buscar_no_cache(chave: str, hubspot_fingerprint: str) -> dict:
    cache = ler_cache()
    entrada = cache.get(chave)
    if not entrada:
        return None
    idade_dias = (time.time() - entrada.get("timestamp", 0)) / 86400
    if idade_dias > CACHE_VALIDADE_DIAS:
        return None
    # lista de contatos já existentes no HubSpot mudou desde a última busca:
    # invalida o cache e roda a cascata de novo (pode haver contato novo a achar)
    if entrada.get("hubspot_fingerprint", "") != hubspot_fingerprint:
        return None
    return entrada.get("dados")


def salvar_no_cache(chave: str, dados: dict, hubspot_fingerprint: str):
    cache = ler_cache()
    cache[chave] = {"timestamp": time.time(), "dados": dados, "hubspot_fingerprint": hubspot_fingerprint}
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


def normalizar_contato(valor: str) -> str:
    """E-mail -> lowercase. Telefone -> só dígitos. Usado tanto pra comparar
    com a lista do HubSpot quanto pra gerar o fingerprint do cache."""
    valor = (valor or "").strip()
    return valor.lower() if "@" in valor else normalizar_telefone(valor)


def parse_lista_contatos(bruto) -> list:
    """Aceita tanto lista (JSON array) quanto string separada por vírgula/quebra de linha."""
    if not bruto:
        return []
    if isinstance(bruto, list):
        return [c for c in bruto if c and str(c).strip()]
    return [c for c in re.split(r'[,;\n]', str(bruto)) if c.strip()]


def fingerprint_contatos(contatos: list) -> str:
    return "|".join(sorted(set(normalizar_contato(c) for c in contatos)))


def separar_nome(nome_completo: str) -> tuple:
    """'João Silva - Diretor Financeiro' -> ('João', 'Silva')"""
    nome_limpo = (nome_completo or "").split(" - ")[0].strip()
    partes = nome_limpo.split()
    if not partes:
        return "", ""
    return partes[0], (partes[-1] if len(partes) > 1 else "")


def pessoa_completa(pessoa: dict) -> bool:
    """nome + cargo (embutidos em nome_cargo) + contato direto (email ou telefone).
    LinkedIn sozinho NÃO conta como contato — é justamente o que Hunter/Clay/
    PhantomBuster existem pra resolver quando só se tem o perfil."""
    if not pessoa:
        return False
    return bool(pessoa.get("nome_cargo")) and bool(pessoa.get("email") or pessoa.get("telefone"))


def dados_completos(pessoa_rh: dict, pessoa_fin: dict) -> bool:
    return pessoa_completa(pessoa_rh) and pessoa_completa(pessoa_fin)


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
        # domínio não bate com o site oficial: baixa confiança, mesmo sendo domínio próprio
        # (pode ser holding, terceiro, ou cadastro desatualizado)
        return {"nivel": "baixa", "motivo": f"domínio ({dom_email}) não bate com o site oficial ({dominio_site})",
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


def buscar_receitaws(cnpj: str) -> dict:
    """Terceiro fallback do cadastro Receita. Endpoint aberto sem chave, mas com
    rate limit apertado (~3 consultas/min) — por isso só entra se as duas
    fontes anteriores (mais tolerantes a volume) já tiverem falhado."""
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(f"https://receitaws.com.br/v1/cnpj/{cnpj_limpo}", timeout=15)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("status") == "ERROR":
            return {}
        return {
            "razao_social": data.get("nome"),
            "nome_fantasia": data.get("fantasia") or data.get("nome"),
            "telefone": data.get("telefone") or None,
            "email": (data.get("email") or "").lower() or None,
            "endereco": f"{data.get('logradouro', '')}, {data.get('municipio', '')} - {data.get('uf', '')}",
            "socios": [s.get("nome") for s in data.get("qsa", []) if s.get("nome")]
        }
    except Exception:
        return {}


def buscar_receita(cnpj: str) -> dict:
    """Nível 0 com redundância: BrasilAPI -> CNPJá -> ReceitaWS."""
    dados = buscar_brasilapi(cnpj)
    if dados:
        dados["fonte_cadastro"] = "brasilapi"
        return dados
    dados = buscar_cnpja(cnpj)
    if dados:
        dados["fonte_cadastro"] = "cnpja"
        return dados
    dados = buscar_receitaws(cnpj)
    if dados:
        dados["fonte_cadastro"] = "receitaws"
    return dados


# ─── NÍVEL 0 — gratuito: tentativa direta de domínio + DuckDuckGo ───────────
def gerar_variacoes_slug(empresa: str) -> list:
    palavras = re.sub(r'[^a-zA-Z0-9\s]', '', empresa).split()
    palavras_uteis = [p for p in palavras if normalizar_texto(p) not in
                      ['ltda', 'sa', 'eireli', 'me', 'epp', 'equipamentos', 'comercio',
                       'industria', 'servicos', 'solucoes', 'grupo', 'brasil',
                       'lojas', 'cia', 'companhia', 'rede']]
    slugs = []
    if palavras_uteis:
        slugs.append(normalizar_texto(palavras_uteis[0]))
    if len(palavras_uteis) >= 2:
        slugs.append(normalizar_texto(palavras_uteis[0] + palavras_uteis[1]))
    slugs.append(normalizar_texto(empresa))
    return list(dict.fromkeys(slugs))


TLDS_TENTATIVA_DIRETA = ['.com.br', '.com', '.ai', '.io', '.co', '.net']

# páginas de desafio anti-bot ou de domínio parado/à venda não são conteúdo
# real da empresa — mas costumam ecoar o próprio hostname no HTML/JS da
# página (ex.: Cloudflare grava o domínio testado no config do desafio),
# o que engana a checagem de "o slug aparece no conteúdo"
SINAIS_PAGINA_INVALIDA = [
    "just a moment", "enable javascript and cookies", "checking your browser",
    "attention required! | cloudflare", "domain is for sale", "domain for sale",
    "buy this domain", "parked domain", "this domain is parked",
]


def descobrir_site_tentativa_direta(empresa: str) -> str:
    """Tenta adivinhar o domínio pelo nome da empresa. Um domínio curto/comum
    (ex.: "blip", "lojas", "stone") pode coincidir com o registro de uma
    empresa totalmente diferente — por isso não basta o domínio responder
    (status < 400): baixamos a página e confirmamos que o slug tentado
    realmente aparece no conteúdo antes de aceitar como site oficial.
    Também rejeitamos redirecionamento pra um domínio diferente do que foi
    tentado — domínio "estacionado"/vendido que redireciona pra outra
    empresa pode conter o mesmo texto por coincidência (ex.: blip.co
    redireciona pra blipbillboards.com, outra empresa, que também tem
    "blip" no conteúdo)."""
    for slug in gerar_variacoes_slug(empresa):
        if len(slug) < 3:
            continue
        for tld in TLDS_TENTATIVA_DIRETA:
            for url in [f"https://www.{slug}{tld}", f"https://{slug}{tld}"]:
                try:
                    r = requests.get(url, timeout=8, allow_redirects=True,
                                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                    if r.status_code >= 400:
                        continue
                    if extrair_dominio_de_url(r.url) != extrair_dominio_de_url(url):
                        continue
                    texto_pagina = r.text[:20000]
                    texto_pagina_lower = texto_pagina.lower()
                    if any(s in texto_pagina_lower for s in SINAIS_PAGINA_INVALIDA):
                        continue
                    if slug in normalizar_texto(texto_pagina):
                        return r.url
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
def buscar_serpapi(query: str, hl: str = "pt", gl: str = "br") -> list:
    try:
        params = {"q": query, "api_key": SERPAPI_KEY, "engine": "google", "num": 5}
        if hl:
            params["hl"] = hl
        if gl:
            params["gl"] = gl
        r = requests.get("https://serpapi.com/search", params=params, timeout=15)
        return r.json().get("organic_results", [])
    except Exception:
        return []


def texto_resultados(resultados: list) -> str:
    return " ".join([(r.get("title", "") + " " + r.get("snippet", "") + " " + r.get("link", "")) for r in resultados])


def escolher_linkedin_via_gemini(resultados: list, empresa: str, papel: str) -> dict:
    """Gemini só ESCOLHE entre candidatos que o SerpAPI já trouxe — nunca gera nem
    completa dado por conta própria. Existe porque a checagem rígida por regex
    (extrair_pessoa_linkedin) só olha os 2 primeiros resultados e exige o termo
    exato no título, o que descarta candidatos válidos quando o Google varia a
    ordem/redação dos resultados entre execuções."""
    candidatos = [r for r in resultados if "linkedin.com/in/" in r.get("link", "")]
    if not candidatos:
        return None

    lista = "\n".join(
        f'{i+1}. link: {c["link"]}\n   titulo: {c.get("title", "")}\n   snippet: {c.get("snippet", "")}'
        for i, c in enumerate(candidatos)
    )
    prompt = (
        f'Destes resultados de busca, qual é o perfil do LinkedIn de uma pessoa que '
        f'trabalha em "{empresa}" em cargo de {papel}?\n\n{lista}\n\n'
        f'Responda APENAS com o link exato de um dos resultados acima, ou a palavra '
        f'"nenhum" se não houver candidato claro. Nunca invente um link que não esteja na lista acima.'
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

    # trava anti-alucinação em código, não só no prompt: só aceita se o link
    # devolvido bater literalmente com um dos candidatos que enviamos
    escolhido = next((c for c in candidatos if c["link"].strip() in texto), None)
    if not escolhido:
        return None

    titulo = escolhido.get("title", "")
    partes = titulo.split(" | ")[0].split(" - ", 1)
    nome = partes[0].strip()
    cargo = partes[1].strip() if len(partes) > 1 else None
    if not nome:
        return None
    return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome, "linkedin": escolhido["link"],
            "email": None, "telefone": None}


def termo_relacao_empresa(empresa: str) -> str:
    """Fragmento mais provável de aparecer num título/perfil de alguém falando
    da empresa. Quando a entrada já vem como domínio (ex.: "blip.ai"), o sufixo
    de TLD quase nunca é repetido por pessoas ("at Blip", não "at BlipAI") —
    então removemos antes de normalizar, senão a comparação falha por completo."""
    for tld in TLDS_TENTATIVA_DIRETA:
        if empresa.lower().endswith(tld):
            return normalizar_texto(empresa[:-len(tld)])
    return normalizar_texto(empresa)


def extrair_pessoa_linkedin(resultados: list, empresa: str, termos_cargo: list) -> dict:
    # query simples e direta ("{cargo} {empresa} linkedin", sem aspas/operadores/site:)
    # já bota o resultado certo nos primeiros lugares — o Google ranqueia bem esse tipo
    # de busca. Por isso só olhamos os 2 primeiros resultados, e confiamos no TÍTULO
    # (não no snippet, que o Google trunca de forma imprevisível) pra confirmar a
    # empresa: o formato padrão do Google pra um perfil é "Nome - Cargo | Empresa",
    # então o título já traz nome, cargo e empresa juntos de forma confiável.
    # Validação leve de cargo também no título (não no snippet) — sem isso, qualquer
    # pessoa nos 2 primeiros resultados vira "RH"/"Financeiro" mesmo sem ter nada a
    # ver com a função (ex.: um CEO virando "Financeiro" só por aparecer na busca).
    termo_empresa = termo_relacao_empresa(empresa)
    for r in resultados[:2]:
        link = r.get("link", "")
        if "linkedin.com/in/" not in link:
            continue
        titulo = r.get("title", "")
        if termo_empresa not in normalizar_texto(titulo):
            continue
        if not any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos_cargo):
            continue
        partes = titulo.split(" | ")[0].split(" - ", 1)
        nome = partes[0].strip()
        cargo = partes[1].strip() if len(partes) > 1 else None
        if not nome:
            continue
        return {"nome_cargo": f"{nome} - {cargo}" if cargo else nome, "linkedin": link,
                "email": None, "telefone": None}
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
                           json={"companyUrls": [linkedin_empresa_url]}, timeout=90)
        return r.json() if r.status_code in (200, 201) else []
    except Exception:
        return []


def filtrar_cargo_na_lista(funcionarios: list, termos_cargo: list) -> dict:
    for f in funcionarios:
        titulo = f.get("title", "") or f.get("headline", "") or f.get("position", "")
        nome = f.get("name", "") or f.get("fullName", "")
        link = f.get("profileUrl", "") or f.get("url", "") or f.get("link", "")
        # alguns scrapers de LinkedIn trazem e-mail público no próprio registro
        email = (f.get("email") or "").lower() or None
        if not titulo or not nome:
            continue
        if any(normalizar_texto(t) in normalizar_texto(titulo) for t in termos_cargo):
            cargo = next((t for t in termos_cargo if normalizar_texto(t) in normalizar_texto(titulo)), titulo)
            return {"nome_cargo": f"{nome} - {cargo}", "linkedin": link, "email": email, "telefone": None}
    return None


# ─── NÍVEL 3 — pago com cota: Hunter.io (email finder por nome + domínio) ────
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
        }, timeout=15)
        if r.status_code != 200:
            return {}
        dados = (r.json() or {}).get("data") or {}
        email = dados.get("email")
        # score baixo (< 50) o Hunter já sinaliza como pouco confiável — descarta
        if not email or (dados.get("score") or 0) < 50:
            return {}
        return {"email": email.lower(), "score": dados.get("score")}
    except Exception:
        return {}


# ─── NÍVEL 4 — pago com cota: Clay (enriquecimento agregado) ────────────────
# ATENÇÃO: Clay normalmente opera via webhook de uma tabela configurada na sua
# conta, não um REST genérico de "achar e-mail de pessoa". O endpoint abaixo é
# um placeholder no mesmo formato de entrada/saída do Hunter — precisa validar
# contra a sua conta real (pode ser preciso trocar por uma URL de webhook).
def buscar_clay_email(nome_completo: str, dominio: str, empresa: str = "") -> dict:
    if not CLAY_API_KEY or not dominio or not nome_completo:
        return {}
    primeiro, ultimo = separar_nome(nome_completo)
    if not primeiro:
        return {}
    try:
        r = requests.post(
            "https://api.clay.com/v1/people/enrich",  # placeholder — confirmar endpoint real da sua conta
            headers={"Authorization": f"Bearer {CLAY_API_KEY}", "Content-Type": "application/json"},
            json={"first_name": primeiro, "last_name": ultimo or primeiro, "domain": dominio, "company": empresa},
            timeout=20
        )
        if r.status_code != 200:
            return {}
        dados = r.json() or {}
        bloco = dados.get("data") or dados
        email = (bloco.get("email") or "").lower()
        if not email:
            return {}
        # nome do campo de score/deliverability ainda não confirmado contra a
        # conta real — tenta as variações mais comuns e guarda o que vier
        score = bloco.get("score") or bloco.get("confidence") or bloco.get("deliverability_score")
        return {"email": email, "score": score}
    except Exception:
        return {}


# ─── NÍVEL 5 — pago com cota, assíncrono: PhantomBuster (última opção) ──────
# Precisa de PHANTOMBUSTER_AGENT_ID (id do Phantom já configurado na sua conta
# pra scraping de funcionários de empresa no LinkedIn) — não está no .env ainda.
# Assíncrono de verdade: dispara o agente e faz polling até terminar ou estourar
# o tempo máximo (aceitável demorar mais, por isso é sempre a última fonte).
def buscar_phantombuster_funcionarios(linkedin_empresa_url: str, tempo_maximo_s: int = 120) -> list:
    if not PHANTOMBUSTER_API_KEY or not PHANTOMBUSTER_AGENT_ID or not linkedin_empresa_url:
        return []
    headers = {"X-Phantombuster-Key": PHANTOMBUSTER_API_KEY, "Content-Type": "application/json"}
    try:
        r = requests.post(
            "https://api.phantombuster.com/api/v2/agents/launch",
            headers=headers,
            json={"id": PHANTOMBUSTER_AGENT_ID, "argument": {"companyUrl": linkedin_empresa_url}},
            timeout=20
        )
        if r.status_code != 200:
            return []
        container_id = (r.json() or {}).get("containerId")
        if not container_id:
            return []

        decorridos = 0
        intervalo = 5
        while decorridos < tempo_maximo_s:
            time.sleep(intervalo)
            decorridos += intervalo
            status_r = requests.get(
                "https://api.phantombuster.com/api/v2/containers/fetch-output",
                headers=headers, params={"id": container_id}, timeout=20
            )
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
        return []  # estourou o tempo máximo — desiste, não trava a resposta pro usuário
    except Exception:
        return []


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
            # ── Estado acumulado ao longo da cascata ───────────────────────
            emails_fontes = {}      # email (lowercase) -> set(nomes das fontes que confirmaram)
            emails_score = {}       # email (lowercase) -> {"valor": int, "fonte": str} — score de deliverability, quando a fonte fornecer
            telefones_fontes = {}   # dígitos normalizados -> {"display": str, "fontes": set(...)}
            emails_brutos = set()   # tudo que foi achado, mesmo se duplicado no HubSpot (p/ flag final)
            telefones_brutos = set()

            def registrar_email(email, fonte, score=None) -> bool:
                """Registra o e-mail vindo de uma fonte. Retorna True se é contato
                NOVO (não está na lista do HubSpot) — False descarta do resultado.
                Se a fonte trouxer um score de deliverability (Hunter/Clay), guarda
                — é uma dimensão de confiança à parte, não descartamos esse dado."""
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
            niveis_usados = []
            socios = []
            razao_social = ""
            email_receita, telefone_receita = None, None
            telefones_site, emails_site = [], []
            termos_rh = ["RH", "Recursos Humanos", "Gerente de RH", "Diretor de RH", "Head de RH",
                         "HR", "Human Resources", "People", "People Ops", "Recruiter", "Talent",
                         "Head of People", "Head of HR", "Head of Talent"]
            termos_fin = ["Financeiro", "CFO", "Diretor Financeiro", "Gerente Financeiro", "Controller",
                          "Chief Financial Officer", "Finance", "VP Finance", "Head of Finance"]

            # ═══ NÍVEL 0 — ReceitaWS/BrasilAPI/CNPJá + site oficial (grátis, sempre roda) ═══
            if eh_cnpj(entrada):
                dados = buscar_receita(entrada)
                if dados:
                    niveis_usados.append(f"receita:{dados.get('fonte_cadastro', '?')}")
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

            # ═══ NÍVEL 1 — SerpAPI (cota) ═══
            # pessoa_rh/pessoa_fin nunca vêm preenchidos do Nível 0 (ele só dá
            # dado de empresa, não de pessoa) — então este nível roda quase
            # sempre. É esperado: é a única forma de achar um nome pra começar.
            if not dados_completos(pessoa_rh, pessoa_fin) and pode_usar("serpapi"):
                r1 = buscar_serpapi(f'"{termo_busca}" telefone contato email')
                registrar_uso("serpapi")
                niveis_usados.append("serpapi")
                texto1 = texto_resultados(r1)
                # NÃO usar a página do LinkedIn como "site" aqui — linkedin_empresa
                # é descoberto corretamente logo abaixo, com sua própria query. Usar
                # o link do LinkedIn como domínio de comparação fazia todo e-mail
                # legítimo da empresa (@empresa.com.br) ser marcado como baixa
                # confiança por "não bater com o site oficial (linkedin.com)".
                padrao_email = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
                for e in re.findall(padrao_email, texto1):
                    if normalizar_texto(termo_busca)[:6] in normalizar_texto(e):
                        registrar_email(e, "serpapi")
                for t in re.findall(r'\(\d{2}\)\s?\d{4,5}-?\d{4}', texto1):
                    if telefone_plausivel(t):
                        registrar_telefone(t, "serpapi")

                if pode_usar("serpapi"):
                    r_li = buscar_serpapi(f'"{termo_busca}" site:linkedin.com/company')
                    registrar_uso("serpapi")
                    linkedin_empresa = extrair_linkedin_empresa(r_li)

                if linkedin_empresa and not pessoa_completa(pessoa_rh) and pode_usar("serpapi"):
                    # sem hl/gl: com hl=pt/gl=br o Google prioriza as páginas da própria
                    # empresa em vez de perfis pessoais pra esse tipo de busca
                    r_rh = buscar_serpapi(f'RH {termo_busca} linkedin', hl=None, gl=None)
                    registrar_uso("serpapi")
                    pessoa_rh = None
                    if GEMINI_API_KEY and pode_usar("gemini"):
                        pessoa_rh = escolher_linkedin_via_gemini(r_rh, termo_busca, "RH")
                        registrar_uso("gemini")
                    if not pessoa_rh:
                        pessoa_rh = extrair_pessoa_linkedin(r_rh, termo_busca, termos_rh)

                if linkedin_empresa and not pessoa_completa(pessoa_fin) and pode_usar("serpapi"):
                    r_fin = buscar_serpapi(f'financeiro {termo_busca} linkedin', hl=None, gl=None)
                    registrar_uso("serpapi")
                    pessoa_fin = None
                    if GEMINI_API_KEY and pode_usar("gemini"):
                        pessoa_fin = escolher_linkedin_via_gemini(r_fin, termo_busca, "Financeiro")
                        registrar_uso("gemini")
                    if not pessoa_fin:
                        pessoa_fin = extrair_pessoa_linkedin(r_fin, termo_busca, termos_fin)

            # ═══ NÍVEL 2 — Apify (scraping de funcionários do LinkedIn) ═══
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
                    # e-mail que já veio pronto no registro do scraping também passa pelo filtro anti-HubSpot
                    for pessoa in (pessoa_rh, pessoa_fin):
                        if pessoa and pessoa.get("email") and not registrar_email(pessoa["email"], "apify"):
                            pessoa["email"] = None

            # ═══ NÍVEL 3 — Hunter.io (email finder pra pessoa já identificada) ═══
            if HUNTER_API_KEY and not dados_completos(pessoa_rh, pessoa_fin) and dominio_site and pode_usar("hunter"):
                if pessoa_rh and not pessoa_completa(pessoa_rh):
                    achou = buscar_hunter_email(pessoa_rh["nome_cargo"], dominio_site)
                    registrar_uso("hunter")
                    niveis_usados.append("hunter")
                    if achou.get("email") and registrar_email(achou["email"], "hunter", achou.get("score")):
                        pessoa_rh["email"] = achou["email"]
                if pessoa_fin and not pessoa_completa(pessoa_fin) and pode_usar("hunter"):
                    achou = buscar_hunter_email(pessoa_fin["nome_cargo"], dominio_site)
                    registrar_uso("hunter")
                    niveis_usados.append("hunter")
                    if achou.get("email") and registrar_email(achou["email"], "hunter", achou.get("score")):
                        pessoa_fin["email"] = achou["email"]

            # ═══ NÍVEL 4 — Clay (enriquecimento agregado) ═══
            if CLAY_API_KEY and not dados_completos(pessoa_rh, pessoa_fin) and dominio_site and pode_usar("clay"):
                if pessoa_rh and not pessoa_completa(pessoa_rh):
                    achou = buscar_clay_email(pessoa_rh["nome_cargo"], dominio_site, empresa_nome)
                    registrar_uso("clay")
                    niveis_usados.append("clay")
                    if achou.get("email") and registrar_email(achou["email"], "clay", achou.get("score")):
                        pessoa_rh["email"] = achou["email"]
                if pessoa_fin and not pessoa_completa(pessoa_fin) and pode_usar("clay"):
                    achou = buscar_clay_email(pessoa_fin["nome_cargo"], dominio_site, empresa_nome)
                    registrar_uso("clay")
                    niveis_usados.append("clay")
                    if achou.get("email") and registrar_email(achou["email"], "clay", achou.get("score")):
                        pessoa_fin["email"] = achou["email"]

            # ═══ NÍVEL 5 — PhantomBuster (última opção, assíncrono, pode demorar) ═══
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

            # ── Classificação de confiança + status de confirmação ─────────
            emails_encontrados = list(emails_fontes.keys())

            emails_classificados = []
            emails_nao_verificados = []
            for e in emails_encontrados[:8]:
                confianca = avaliar_confianca_email(e, dominio_site, razao_social)
                fontes = emails_fontes[e]
                registro = {
                    "email": e,
                    "departamento": classificar_email_por_departamento(e),
                    "confianca": confianca["nivel"],
                    "motivo": confianca["motivo"],
                    "possivel_contador": confianca["possivel_contador"],
                    "fontes": sorted(fontes),
                    "status_confirmacao": "confirmado" if len(fontes) >= 2 else "não confirmado, usar com cautela",
                    # 3ª dimensão de confiança: score de deliverability do Hunter/Clay,
                    # quando a fonte fornecer (None se nenhuma fonte deu esse dado)
                    "score_verificacao": emails_score.get(e),
                    "origem": "receita_federal" if e == email_receita else ("site" if e in emails_site else "busca"),
                }
                # e-mail com padrão de contador NÃO entra na lista principal:
                # cold email pra contador é buraco negro e queima sender reputation
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
                    "telefone": display,
                    "origem": origem,
                    "confianca": conf_tel["nivel"],
                    "motivo": conf_tel["motivo"],
                    "fontes": sorted(fontes),
                    "status_confirmacao": "confirmado" if len(fontes) >= 2 else "não confirmado, usar com cautela",
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

            # filtro anti-duplicidade HubSpot: sinaliza quando TUDO que foi achado
            # já existia no HubSpot, em vez de simplesmente devolver vazio
            todos_contatos_ja_existem = bool(
                (emails_brutos or telefones_brutos) and not emails_encontrados and not telefones_fontes
            )

            nao_encontrado = "Não encontrado em fonte pública"
            resultado = {
                "empresa": empresa_nome,
                "site": site or nao_encontrado,
                # formato antigo preservado (index.html continua funcionando):
                "telefones": [tc["telefone"] for tc in telefones_classificados],
                "emails": [{"email": ec["email"], "departamento": ec["departamento"]} for ec in emails_classificados],
                # camada de confiança + rastreio de fontes:
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
                "rh_email": pessoa_rh.get("email") if pessoa_rh else None,
                "linkedin_financeiro": (pessoa_fin["nome_cargo"] + " (a confirmar)") if pessoa_fin else nao_encontrado,
                "linkedin_financeiro_url": pessoa_fin["linkedin"] if pessoa_fin else None,
                "financeiro_email": pessoa_fin.get("email") if pessoa_fin else None,
                # filtro anti-duplicidade HubSpot:
                "todos_contatos_ja_existem": todos_contatos_ja_existem,
                "niveis_usados": list(dict.fromkeys(niveis_usados)),
                "veio_do_cache": False
            }
            salvar_no_cache(chave, resultado, hubspot_fingerprint)

        except Exception as e:
            return jsonify({"erro": str(e)}), 500

    quotas = ler_quotas()
    resultado["quotas"] = {fonte: {"usos": quotas[fonte]["usos"], "limite": LIMITES_MES[fonte]} for fonte in LIMITES_MES}

    return jsonify(resultado)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
