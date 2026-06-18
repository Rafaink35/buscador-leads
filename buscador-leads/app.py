import os
import re
import json
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY = os.getenv("SERPAPI_KEY")


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


def extrair_emails(texto: str) -> list:
    padrao = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
    emails = re.findall(padrao, texto)
    ignorar = ['png', 'jpg', 'gif', 'example', 'seusite', 'sentry', 'wixpress', '.css', '.js']
    return list(set([e.lower() for e in emails if not any(i in e.lower() for i in ignorar)]))


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

        # Busca 1: contato oficial
        r1 = buscar_google(f'"{termo_busca}" telefone contato')
        texto1 = texto_resultados(r1)
        telefones_encontrados += extrair_telefones(texto1)
        emails_encontrados += extrair_emails(texto1)
        site = extrair_dominio_oficial(r1, termo_busca)
        fontes += [r.get("link") for r in r1 if r.get("link")]

        # Busca 2: fale conosco
        r2 = buscar_google(f'"{termo_busca}" "fale conosco" OR "atendimento" email')
        texto2 = texto_resultados(r2)
        telefones_encontrados += extrair_telefones(texto2)
        emails_encontrados += extrair_emails(texto2)
        if not site:
            site = extrair_dominio_oficial(r2, termo_busca)
        fontes += [r.get("link") for r in r2 if r.get("link")]

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

        telefones_encontrados = list(dict.fromkeys(telefones_encontrados))
        emails_encontrados = list(dict.fromkeys(emails_encontrados))
        fontes = list(dict.fromkeys([f for f in fontes if f]))[:6]

        numeros_conhecidos_norm = [normalizar_telefone(n) for n in re.split(r'[,;\n]', numeros_conhecidos) if n.strip()]
        telefones_novos = [t for t in telefones_encontrados if normalizar_telefone(t) not in numeros_conhecidos_norm]
        telefones_repetidos = [t for t in telefones_encontrados if normalizar_telefone(t) in numeros_conhecidos_norm]

        nao_encontrado = "Não encontrado em fonte pública"

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
            "fontes": fontes
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
