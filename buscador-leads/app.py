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
    """Consulta a Receita Federal via ReceitaWS (gratuito, sem chave)"""
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
            "site": None,
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
    """Remove tudo que não é número, pra comparar dois telefones de formatos diferentes"""
    return re.sub(r'\D', '', tel)


def normalizar_texto(texto: str) -> str:
    import unicodedata
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]', '', texto.lower())


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

        # Se for CNPJ, consulta a Receita Federal primeiro (fonte oficial)
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

        # Busca 1: contato oficial no Google
        r1 = buscar_google(f'"{termo_busca}" telefone contato')
        texto1 = texto_resultados(r1)
        telefones_encontrados += extrair_telefones(texto1)
        emails_encontrados += extrair_emails(texto1)
        site = extrair_dominio_oficial(r1, termo_busca)
        fontes += [r.get("link") for r in r1 if r.get("link")]

        # Busca 2: página "fale conosco" / "contato" do site
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

        # Remove duplicados
        telefones_encontrados = list(dict.fromkeys(telefones_encontrados))
        emails_encontrados = list(dict.fromkeys(emails_encontrados))
        fontes = list(dict.fromkeys([f for f in fontes if f]))[:6]

        # Compara com números já conhecidos (HubSpot) e separa os novos
        numeros_conhecidos_norm = [normalizar_telefone(n) for n in re.split(r'[,;\n]', numeros_conhecidos) if n.strip()]
        telefones_novos = [
            t for t in telefones_encontrados
            if normalizar_telefone(t) not in numeros_conhecidos_norm
        ]
        telefones_repetidos = [
            t for t in telefones_encontrados
            if normalizar_telefone(t) in numeros_conhecidos_norm
        ]

        nao_encontrado = "Não encontrado em fonte pública"

        return jsonify({
            "empresa": empresa_nome,
            "site": site or nao_encontrado,
            "telefones_novos": telefones_novos[:5] if telefones_novos else [],
            "telefones_ja_conhecidos": telefones_repetidos[:5] if telefones_repetidos else [],
            "emails": emails_encontrados[:5] if emails_encontrados else [],
            "fonte_receita_federal": fonte_receita,
            "fontes": fontes
        })

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
