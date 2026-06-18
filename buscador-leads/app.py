import os
import re
import json
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
APIFY_USOS_MAXIMOS_MES = 40  # margem de segurança dentro dos $5 grátis


def limpar_cnpj(cnpj: str) -> str:
    return re.sub(r'\D', '', cnpj)


def eh_cnpj(texto: str) -> bool:
    return len(limpar_cnpj(texto)) == 14


def buscar_receita(cnpj: str) -> dict:
    cnpj_limpo = limpar_cnpj(cnpj)
    try:
        r = requests.get(f"https://receitaws.com.br/v1/cnpj/{cnpj_limpo}", timeout=10)
        data = r.json()
        if data.get("status") == "ERROR":
            return {}
        return {
            "razao_social": data.get("nome"),
            "nome_fantasia": data.get("fantasia"),
            "telefone": data.get("telefone"),
            "email": data.get("email"),
            "endereco": f"{data.get('logradouro', '')}, {data.get('municipio', '')} - {data.get('uf', '')}"
        }
    except Exception:
        return {}


def buscar_google(query: str) -> list:
    try:
        r = requests.get("https://serpapi.com/search", params={
            "q": query, "api_key": SERPAPI_KEY, "engine": "google",
            "num": 5, "hl": "pt", "gl": "br"
        }, timeout=15)
        return r.json().get("organic_results", [])
    except Exception:
        return []


def extrair_emails(texto: str, empresa: str = None, dominio_oficial: str = None, aceitar_plataformas_vagas: bool = False) -> list:
    padrao = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
    emails = re.findall(padrao, texto)
    ignorar = ['png', 'jpg', 'gif', 'example', 'seusite', 'sentry', 'wixpress', '.css', '.js']
    emails = [e.lower() for e in emails if not any(i in e.lower() for i in ignorar)]

    if not empresa and not dominio_oficial:
        return list(set(emails))

    empresa_slug = normalizar_texto(empresa)[:8] if empresa else ""
    dominio_slug = normalizar_texto(dominio_oficial.replace("https://", "").replace("http://", "")) if dominio_oficial else ""
    plataformas_vagas = ["gupy.io", "catho.com.br", "indeedemail.com", "vagas.com.br"]

    validos = []
    for email in emails:
        usuario, dominio_email = email.split("@")[0], email.split("@")[-1]
        usuario_norm = normalizar_texto(usuario)
        dominio_email_norm = normalizar_texto(dominio_email)

        # Caso normal: domínio do email bate com a empresa ou com o site oficial
        if (empresa_slug and empresa_slug in dominio_email_norm) or (dominio_slug and dominio_slug in dominio_email_norm):
            validos.append(email)
            continue

        # Caso plataforma de vagas: nome da empresa aparece ANTES do @ (ex: vagas.empresa@gupy.io)
        if aceitar_plataformas_vagas and any(p in dominio_email_norm for p in [normalizar_texto(pv) for pv in plataformas_vagas]):
            if empresa_slug and empresa_slug in usuario_norm:
                validos.append(email)

    return list(set(validos))


def extrair_telefones(texto: str) -> list:
    padroes = [
        r'\(\d{2}\)\s?\d{4,5}-?\d{4}',
        r'\+55\s?\d{2}\s?\d{4,5}-?\d{4}',
        r'0800\s?\d{3}\s?\d{4}',
    ]
    encontrados = []
    for p in padroes:
        encontrados += re.findall(p, texto)
    return list(set(encontrados))


def normalizar_telefone(tel: str) -> str:
    return re.sub(r'\D', '', tel)


def normalizar_texto(texto: str) -> str:
    import unicodedata
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]', '', texto.lower())


def empresa_mencionada(texto: str, empresa: str) -> bool:
    texto_norm = normalizar_texto(texto)
    empresa_norm = normalizar_texto(empresa)
    if len(empresa_norm) < 4:
        return empresa_norm in texto_norm
    return empresa_norm in texto_norm


def extrair_pessoa_linkedin(resultados: list, empresa: str, termos_cargo: list) -> dict:
    """Só retorna pessoa se: perfil pessoal + empresa mencionada + cargo mencionado.
    Mesmo assim é marcado como 'a confirmar' no card."""
    for r in resultados:
        link = r.get("link", "")
        if "linkedin.com/in/" not in link:
            continue
        titulo = r.get("title", "")
        snippet = r.get("snippet", "")
        texto_completo = f"{titulo} {snippet}"
        if not empresa_mencionada(texto_completo, empresa):
            continue
        tem_cargo = any(normalizar_texto(termo) in normalizar_texto(texto_completo) for termo in termos_cargo)
        if not tem_cargo:
            continue
        nome = titulo.split(" - ")[0].split(" | ")[0].strip() if (" - " in titulo or " | " in titulo) else titulo.strip()
        cargo_match = None
        for termo in termos_cargo:
            if normalizar_texto(termo) in normalizar_texto(texto_completo):
                cargo_match = termo
                break
        return {
            "nome_cargo": f"{nome} - {cargo_match}" if cargo_match else nome,
            "linkedin": link
        }
    return None


def extrair_linkedin_empresa(resultados: list) -> str:
    for r in resultados:
        link = r.get("link", "")
        if "linkedin.com/company/" in link:
            return link
    return None


def extrair_dominio_oficial(resultados: list, empresa: str) -> str:
    empresa_slug = normalizar_texto(empresa)[:8]
    for r in resultados:
        link = r.get("link", "")
        dominio = re.sub(r'https?://(www\.)?', '', link).split('/')[0]
        dominio_slug = normalizar_texto(dominio)
        bloqueados = ["linkedin", "facebook", "instagram", "youtube", "indeed",
                      "glassdoor", "serasaexperian", "receita", "gov.br", "cnpj.biz"]
        if any(b in dominio_slug for b in bloqueados):
            continue
        if empresa_slug in dominio_slug:
            return f"https://{dominio}"
    return None


def texto_resultados(resultados: list) -> str:
    return " ".join([
        (r.get("title", "") + " " + r.get("snippet", "") + " " + r.get("link", ""))
        for r in resultados
    ])


def ler_contador_apify() -> dict:
    """Persiste o contador em arquivo, sobrevive a restarts do servidor"""
    caminho = "apify_uso.json"
    hoje = __import__("datetime").date.today()
    mes_atual = f"{hoje.year}-{hoje.month:02d}"
    if os.path.exists(caminho):
        with open(caminho, "r") as f:
            dados = json.load(f)
        if dados.get("mes") != mes_atual:
            dados = {"mes": mes_atual, "usos": 0}
    else:
        dados = {"mes": mes_atual, "usos": 0}
    return dados


def gravar_contador_apify(dados: dict):
    with open("apify_uso.json", "w") as f:
        json.dump(dados, f)


def pode_usar_apify() -> bool:
    dados = ler_contador_apify()
    return dados["usos"] < APIFY_USOS_MAXIMOS_MES


def registrar_uso_apify():
    dados = ler_contador_apify()
    dados["usos"] += 1
    gravar_contador_apify(dados)


def buscar_apify_funcionarios(linkedin_empresa_url: str) -> list:
    """Chama o Actor da Apify (tier Free, até 50 perfis) e retorna lista de funcionários.
    Só é chamado quando a busca gratuita via Google não encontrou o Financeiro."""
    if not APIFY_TOKEN or not linkedin_empresa_url:
        return []
    try:
        url = "https://api.apify.com/v2/acts/apt_marble~linkedin-company-employees-scraper/run-sync-get-dataset-items"
        params = {"token": APIFY_TOKEN}
        payload = {"companies": [linkedin_empresa_url]}
        r = requests.post(url, params=params, json=payload, timeout=90)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


def filtrar_cargo_na_lista(funcionarios: list, termos_cargo: list) -> dict:
    """Procura na lista de funcionários (vinda da Apify) alguém com o cargo desejado"""
    for f in funcionarios:
        titulo = f.get("title", "") or f.get("headline", "") or f.get("position", "")
        nome = f.get("name", "") or f.get("fullName", "")
        link = f.get("profileUrl", "") or f.get("url", "") or f.get("link", "")
        if not titulo or not nome:
            continue
        tem_cargo = any(normalizar_texto(termo) in normalizar_texto(titulo) for termo in termos_cargo)
        if tem_cargo:
            cargo_match = next((t for t in termos_cargo if normalizar_texto(t) in normalizar_texto(titulo)), titulo)
            return {"nome_cargo": f"{nome} - {cargo_match}", "linkedin": link}
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

    try:
        emails_encontrados = []
        telefones_encontrados = []
        site = None
        empresa_nome = entrada
        fontes = []
        fonte_receita = False

        if eh_cnpj(entrada):
            dados_receita = buscar_receita(entrada)
            if dados_receita:
                empresa_nome = dados_receita.get("nome_fantasia") or dados_receita.get("razao_social") or entrada
                if dados_receita.get("telefone"):
                    telefones_encontrados.append(dados_receita["telefone"])
                    fonte_receita = True
                if dados_receita.get("email"):
                    emails_encontrados.append(dados_receita["email"])
                fontes.append(f"Receita Federal (CNPJ {limpar_cnpj(entrada)})")

        termo_busca = empresa_nome if empresa_nome != entrada else entrada

        # Busca 1: contato oficial — descobre o site primeiro
        r1 = buscar_google(f'"{termo_busca}" telefone contato')
        texto1 = texto_resultados(r1)
        telefones_encontrados += extrair_telefones(texto1)
        site = extrair_dominio_oficial(r1, termo_busca)
        fontes += [r.get("link") for r in r1 if r.get("link")]

        # Busca 2: fale conosco
        r2 = buscar_google(f'"{termo_busca}" "fale conosco" OR "atendimento" email')
        texto2 = texto_resultados(r2)
        telefones_encontrados += extrair_telefones(texto2)
        if not site:
            site = extrair_dominio_oficial(r2, termo_busca)
        fontes += [r.get("link") for r in r2 if r.get("link")]

        # Agora que já temos o site oficial (se encontrado), valida os emails contra ele
        emails_encontrados += extrair_emails(texto1, empresa=termo_busca, dominio_oficial=site)
        emails_encontrados += extrair_emails(texto2, empresa=termo_busca, dominio_oficial=site)

        # Busca 3: WhatsApp comercial
        r3 = buscar_google(f'"{termo_busca}" WhatsApp comercial vendas')
        texto3 = texto_resultados(r3)
        telefones_encontrados += extrair_telefones(texto3)
        fontes += [r.get("link") for r in r3 if r.get("link")]

        # Busca 4: LinkedIn da empresa
        r4 = buscar_google(f'"{termo_busca}" site:linkedin.com/company')
        linkedin_empresa = extrair_linkedin_empresa(r4)
        fontes += [r.get("link") for r in r4 if r.get("link")]

        # Busca 5: RH no LinkedIn (extra, marcado como "a confirmar")
        termos_rh = ["RH", "Recursos Humanos", "Gerente de RH", "Diretor de RH", "Head de RH", "Talent Acquisition", "People"]
        r5 = buscar_google(f'"{termo_busca}" (gerente OR diretor OR head) RH site:linkedin.com/in')
        pessoa_rh = extrair_pessoa_linkedin(r5, termo_busca, termos_rh)
        fontes += [r.get("link") for r in r5 if r.get("link")]


        # Busca 6: Financeiro no LinkedIn (extra, marcado como "a confirmar")
        termos_fin = ["Financeiro", "CFO", "Diretor Financeiro", "Gerente Financeiro", "Controller", "Controladoria"]
        r6 = buscar_google(f'"{termo_busca}" (diretor OR gerente OR CFO) financeiro site:linkedin.com/in')
        pessoa_fin = extrair_pessoa_linkedin(r6, termo_busca, termos_fin)
        fontes += [r.get("link") for r in r6 if r.get("link")]

        # Fallback: se a busca gratuita não achou o Financeiro, tenta via Apify (tier Free, até 50 perfis)
        apify_usado = False
        if not pessoa_fin and linkedin_empresa and pode_usar_apify():
            funcionarios = buscar_apify_funcionarios(linkedin_empresa)
            if funcionarios:
                registrar_uso_apify()
                apify_usado = True
                pessoa_fin = filtrar_cargo_na_lista(funcionarios, termos_fin)
                if not pessoa_rh:
                    pessoa_rh = filtrar_cargo_na_lista(funcionarios, termos_rh)

        # Busca 7: email de RH em sites de vagas (Gupy, Indeed, Catho) — fonte de alta confiança
        r7 = buscar_google(f'"{termo_busca}" vaga emprego enviar currículo email (gupy.io OR indeed.com OR catho.com.br)')
        texto7 = texto_resultados(r7)
        emails_rh_vagas = extrair_emails(texto7, empresa=termo_busca, dominio_oficial=site, aceitar_plataformas_vagas=True)
        if emails_rh_vagas:
            emails_encontrados += emails_rh_vagas
        fontes += [r.get("link") for r in r7 if r.get("link")]

        telefones_encontrados = list(dict.fromkeys(telefones_encontrados))
        emails_encontrados = list(dict.fromkeys(emails_encontrados))
        fontes = list(dict.fromkeys([f for f in fontes if f]))[:6]

        numeros_conhecidos_norm = [normalizar_telefone(n) for n in re.split(r'[,;\n]', numeros_conhecidos) if n.strip()]
        telefones_novos = [t for t in telefones_encontrados if normalizar_telefone(t) not in numeros_conhecidos_norm]
        telefones_repetidos = [t for t in telefones_encontrados if normalizar_telefone(t) in numeros_conhecidos_norm]

        nao_encontrado = "Não encontrado em fonte pública"
        dados_quota = ler_contador_apify()

        return jsonify({
            "empresa": empresa_nome,
            "site": site or nao_encontrado,
            "telefones_novos": telefones_novos[:5] if telefones_novos else [],
            "telefones_ja_conhecidos": telefones_repetidos[:5] if telefones_repetidos else [],
            "emails": emails_encontrados[:5] if emails_encontrados else [],
            "fonte_receita_federal": fonte_receita,
            "linkedin_empresa": linkedin_empresa or nao_encontrado,
            "linkedin_rh": pessoa_rh["nome_cargo"] + " (a confirmar)" if pessoa_rh else nao_encontrado,
            "linkedin_rh_url": pessoa_rh["linkedin"] if pessoa_rh else None,
            "linkedin_financeiro": pessoa_fin["nome_cargo"] + " (a confirmar)" if pessoa_fin else nao_encontrado,
            "linkedin_financeiro_url": pessoa_fin["linkedin"] if pessoa_fin else None,
            "apify_usado_nesta_busca": apify_usado,
            "apify_usos_no_mes": dados_quota["usos"],
            "apify_limite_mes": APIFY_USOS_MAXIMOS_MES,
            "fontes": fontes
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
