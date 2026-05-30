import os
import re
import json
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY = os.getenv("SERPAPI_KEY")


def buscar(query: str) -> list:
    r = requests.get("https://serpapi.com/search", params={
        "q": query,
        "api_key": SERPAPI_KEY,
        "engine": "google",
        "num": 5,
        "hl": "pt",
        "gl": "br"
    })
    return r.json().get("organic_results", [])


def extrair_emails(texto: str) -> list:
    padrao = r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}'
    emails = re.findall(padrao, texto)
    # Filtra emails genéricos/inválidos
    ignorar = ['png', 'jpg', 'gif', 'example', 'seusite', 'email']
    return list(set([e for e in emails if not any(i in e.lower() for i in ignorar)]))


def extrair_telefones(texto: str) -> list:
    padrao = r'(?:\+55\s?)?(?:\(?\d{2}\)?\s?)(?:9\s?\d{4}|\d{4})[-\s]?\d{4}'
    return list(set(re.findall(padrao, texto)))


def extrair_linkedin(resultados: list, tipo: str = "company") -> str:
    for r in resultados:
        link = r.get("link", "")
        if "linkedin.com" in link:
            if tipo == "company" and "/company/" in link:
                return link
            if tipo == "person" and "/in/" in link:
                return link
    return None


def extrair_site(resultados: list, empresa: str) -> str:
    empresa_slug = empresa.lower().replace(" ", "").replace("-", "")
    for r in resultados:
        link = r.get("link", "")
        dominio = re.sub(r'https?://(www\.)?', '', link).split('/')[0]
        dominio_slug = dominio.lower().replace("-", "").replace(".", "")
        if empresa_slug[:6] in dominio_slug and "linkedin" not in dominio and "facebook" not in dominio:
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
    empresa = data.get("empresa", "").strip()
    if not empresa:
        return jsonify({"erro": "Nome ou CNPJ é obrigatório"}), 400

    try:
        emails_encontrados = []
        telefones_encontrados = []
        linkedin_empresa = None
        linkedin_responsavel = None
        site = None
        responsavel_rh = None
        responsavel_financeiro = None
        fontes = []

        # Busca 1: site oficial + contato
        r1 = buscar(f"{empresa} site oficial contato email")
        texto1 = texto_resultados(r1)
        emails_encontrados += extrair_emails(texto1)
        telefones_encontrados += extrair_telefones(texto1)
        site = extrair_site(r1, empresa)
        fontes += [r.get("link") for r in r1 if r.get("link")]

        # Busca 2: LinkedIn empresa
        r2 = buscar(f"{empresa} LinkedIn empresa")
        linkedin_empresa = extrair_linkedin(r2, "company")
        texto2 = texto_resultados(r2)
        emails_encontrados += extrair_emails(texto2)
        fontes += [r.get("link") for r in r2 if r.get("link")]

        # Busca 3: responsável RH LinkedIn
        r3 = buscar(f"{empresa} gerente diretor RH recursos humanos LinkedIn")
        texto3 = texto_resultados(r3)
        linkedin_responsavel_rh = extrair_linkedin(r3, "person")
        # Tenta extrair nome do responsável RH do snippet
        for res in r3:
            snippet = res.get("snippet", "")
            title = res.get("title", "")
            if "linkedin.com/in/" in res.get("link", ""):
                # Nome geralmente é a primeira parte do título do LinkedIn
                nome = title.split(" - ")[0].strip() if " - " in title else None
                cargo = title.split(" - ")[1].strip() if title.count(" - ") >= 1 else None
                if nome:
                    responsavel_rh = f"{nome}{' - ' + cargo if cargo else ''}"
                    linkedin_responsavel = linkedin_responsavel or res.get("link")
                    break
        fontes += [r.get("link") for r in r3 if r.get("link")]

        # Busca 4: responsável Financeiro LinkedIn
        r4 = buscar(f"{empresa} diretor gerente financeiro CFO LinkedIn")
        for res in r4:
            if "linkedin.com/in/" in res.get("link", ""):
                title = res.get("title", "")
                nome = title.split(" - ")[0].strip() if " - " in title else None
                cargo = title.split(" - ")[1].strip() if title.count(" - ") >= 1 else None
                if nome:
                    responsavel_financeiro = f"{nome}{' - ' + cargo if cargo else ''}"
                    linkedin_responsavel = linkedin_responsavel or res.get("link")
                    break
        texto4 = texto_resultados(r4)
        emails_encontrados += extrair_emails(texto4)
        fontes += [r.get("link") for r in r4 if r.get("link")]

        # Busca 5: telefone WhatsApp
        r5 = buscar(f"{empresa} telefone WhatsApp contato")
        texto5 = texto_resultados(r5)
        telefones_encontrados += extrair_telefones(texto5)
        emails_encontrados += extrair_emails(texto5)
        fontes += [r.get("link") for r in r5 if r.get("link")]

        # Limpa duplicatas
        emails_encontrados = list(set(emails_encontrados))[:3]
        telefones_encontrados = list(set(telefones_encontrados))[:2]
        fontes = list(dict.fromkeys([f for f in fontes if f]))[:6]

        nao_encontrado = "Não encontrado em fonte pública"

        return jsonify({
            "empresa": empresa,
            "site": site or nao_encontrado,
            "responsavel_rh": responsavel_rh or nao_encontrado,
            "responsavel_financeiro": responsavel_financeiro or nao_encontrado,
            "email": ", ".join(emails_encontrados) if emails_encontrados else nao_encontrado,
            "telefone": ", ".join(telefones_encontrados) if telefones_encontrados else nao_encontrado,
            "linkedin_empresa": linkedin_empresa or nao_encontrado,
            "linkedin_responsavel": linkedin_responsavel or nao_encontrado,
            "fontes": fontes
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
