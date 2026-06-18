import os
import json
import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=".")

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def buscar(query):
    r = requests.get("https://serpapi.com/search", params={
        "q": query, "api_key": SERPAPI_KEY, "engine": "google", "num": 5, "hl": "pt", "gl": "br"
    })
    return r.json().get("organic_results", [])


def resumir(resultados):
    return "\n".join([f"Título: {r.get('title','')}\nLink: {r.get('link','')}\nResumo: {r.get('snippet','')}\n---" for r in resultados])


def chamar_groq(prompt):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
        "temperature": 0.0
    }
    r = requests.post(GROQ_URL, headers=headers, json=payload)
    data = r.json()
    if "error" in data:
        raise Exception(data["error"].get("message", "Erro no Groq"))
    return data["choices"][0]["message"]["content"]


PROMPT = """Você é um validador RIGOROSO de dados de prospecção B2B no Brasil. Sua prioridade #1 é NUNCA atribuir uma pessoa errada a uma empresa.

Empresa-alvo: "{empresa}"

Resultados de busca (já filtrados para perfis de LinkedIn de pessoas e páginas da empresa):
{resultados}

REGRAS DE VALIDAÇÃO OBRIGATÓRIAS antes de incluir qualquer pessoa como responsável:
1. O resultado precisa ser um perfil do LinkedIn (linkedin.com/in/) cujo TÍTULO ou RESUMO mencione explicitamente o nome "{empresa}" como o cargo ATUAL da pessoa (não experiência passada, não cliente, não menção aleatória).
2. Se o snippet do LinkedIn mostra a pessoa em OUTRA empresa diferente de "{empresa}", ou a relação com "{empresa}" não está clara, NÃO inclua essa pessoa — retorne "Não encontrado em fonte pública" para esse campo.
3. Nomes de pessoas que aparecem em sites de terceiros (associações, sindicatos, blogs) sem ligação clara e ATUAL com "{empresa}" devem ser descartados.
4. Na dúvida, SEMPRE prefira retornar "Não encontrado em fonte pública" do que arriscar um nome errado. Um falso negativo é aceitável; um falso positivo não é.
5. Emails e telefones só devem vir do site oficial da empresa ou de páginas que claramente pertencem a ela (mesmo domínio do site oficial).

Responda APENAS com JSON puro, sem markdown, sem explicações, sem texto antes ou depois:
{{
  "empresa": "nome oficial da empresa",
  "site": "url do site oficial ou Não encontrado em fonte pública",
  "responsavel_rh": "Nome - Cargo (APENAS se atualmente na empresa-alvo, confirmado) ou Não encontrado em fonte pública",
  "linkedin_rh": "url linkedin do RH ou Não encontrado em fonte pública",
  "responsavel_financeiro": "Nome - Cargo (APENAS se atualmente na empresa-alvo, confirmado) ou Não encontrado em fonte pública",
  "linkedin_financeiro": "url linkedin do Financeiro ou Não encontrado em fonte pública",
  "email": "email do domínio oficial ou Não encontrado em fonte pública",
  "telefone": "telefone do site oficial ou Não encontrado em fonte pública",
  "linkedin_empresa": "url linkedin da empresa ou Não encontrado em fonte pública",
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

        queries = [
            f'"{empresa}" site oficial contato email',
            f'"{empresa}" site:linkedin.com/company',
            f'"{empresa}" ("gerente de RH" OR "diretor de RH" OR "head de RH" OR "gerente de recursos humanos") site:linkedin.com/in',
            f'"{empresa}" ("diretor financeiro" OR "CFO" OR "gerente financeiro") site:linkedin.com/in',
            f'"{empresa}" telefone WhatsApp contato'
        ]

        for query in queries:
            r = buscar(query)
            todos.extend(r)
            fontes += [x.get("link") for x in r if x.get("link")]

        fontes = list(dict.fromkeys(fontes))[:6]
        resposta = chamar_groq(PROMPT.format(empresa=empresa, resultados=resumir(todos[:20])))
        resposta = resposta.strip().replace("```json", "").replace("```", "").strip()
        resultado = json.loads(resposta)
        resultado["fontes"] = fontes
        return jsonify(resultado)
    except json.JSONDecodeError as e:
        return jsonify({"erro": f"Erro JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
