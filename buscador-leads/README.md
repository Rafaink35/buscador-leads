# Buscador de Leads — Flash Benefícios

Ferramenta de enriquecimento de leads usando Gemini + Google Search.
100% gratuita dentro dos limites das APIs.

---

## Pré-requisitos

- Python instalado (python.org/downloads)
- Chave da Gemini API (aistudio.google.com)
- Chave da Google Custom Search API + Search Engine ID

---

## Instalação (só uma vez)

1. Abre o terminal na pasta do projeto
2. Instala as dependências:

```
pip install -r requirements.txt
```

3. Renomeia o arquivo `.env.exemplo` para `.env`
4. Abre o `.env` e cola suas chaves reais

---

## Como usar todo dia

1. Abre o terminal na pasta do projeto
2. Roda:

```
python app.py
```

3. Abre o browser em: http://localhost:5000
4. Digita o nome ou CNPJ da empresa e pressiona Enter

---

## Limites gratuitos

- Google Custom Search: 100 buscas/dia
- Gemini API: 1.500 requisições/dia
- Cada empresa pesquisada usa ~4-5 buscas

---

## Para encerrar

Volta no terminal e aperta Ctrl+C
