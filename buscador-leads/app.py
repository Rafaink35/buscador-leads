import os
import json
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
GEMINI_KEY  = os.getenv("GEMINI_API_KEY")
GEMINI_URL  = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={GEMINI_KEY}"


def buscar(query):
    r = requests.get("https://serpapi.com/search", params={
        "q": query, "api_key": SERPAPI_KEY, "engine": "google", "num": 5, "hl": "pt", "gl": "br"
    })
    return r.json().get("organic_results", [])


def resumir(resultados):
    return "\n".join([f"Título: {r.get('title','')}\nLink: {r.get('link','')}\nResumo: {r.get('snippet','')}\n---" for r in resultados])


def chamar_gemini(prompt):
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1000}}
    r = requests.post(GEMINI_URL, json=payload)
    data = r.json()
    if "error" in data:
        raise Exception(data["error"].get("message", "Erro no Gemini"))
    return data["candidates"][0]["content"]["parts"][0]["text"]


PROMPT = """Analise os resultados de busca sobre "{empresa}" e extraia APENAS dados explícitos.
NUNCA invente. Se não encontrar: "Não encontrado em fonte pública".

Resultados:
{resultados}

Responda APENAS JSON puro sem markdown:
{{
  "empresa": "nome oficial",
  "site": "url ou Não encontrado em fonte pública",
  "responsavel_rh": "Nome - Cargo ou Não encontrado em fonte pública",
  "linkedin_rh": "url linkedin RH ou Não encontrado em fonte pública",
  "responsavel_financeiro": "Nome - Cargo ou Não encontrado em fonte pública",
  "linkedin_financeiro": "url linkedin Financeiro ou Não encontrado em fonte pública",
  "email": "email ou Não encontrado em fonte pública",
  "telefone": "telefone ou Não encontrado em fonte pública",
  "linkedin_empresa": "url linkedin empresa ou Não encontrado em fonte pública",
  "fontes": ["url1", "url2"]
}}"""


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/buscar", methods=["POST"])
def buscar_lead():
    empresa = request.json.get("empresa", "").strip()
    if not empresa:
        return jsonify({"erro": "Nome ou CNPJ é obrigatório"}), 400
    try:
        todos = []
        fontes = []
        for query in [
            f"{empresa} site oficial contato email",
            f"{empresa} LinkedIn empresa",
            f"{empresa} gerente diretor RH LinkedIn",
            f"{empresa} diretor financeiro CFO LinkedIn",
            f"{empresa} telefone WhatsApp contato"
        ]:
            r = buscar(query)
            todos.extend(r)
            fontes += [x.get("link") for x in r if x.get("link")]

        fontes = list(dict.fromkeys(fontes))[:6]
        resposta = chamar_gemini(PROMPT.format(empresa=empresa, resultados=resumir(todos[:15])))
        resposta = resposta.strip().replace("```json","").replace("```","").strip()
        resultado = json.loads(resposta)
        resultado["fontes"] = fontes
        return jsonify(resultado)
    except json.JSONDecodeError as e:
        return jsonify({"erro": f"Erro JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
