# Buscador de Leads — Flash Benefícios

Ferramenta de enriquecimento de leads: dado o nome, domínio ou CNPJ de uma empresa, encontra telefone, e-mail e prováveis contatos de RH e Financeiro, com camada de confiança (domínio bate com o site? confirmado por 2+ fontes?) e filtro anti-duplicidade contra o que você já tem no HubSpot.

Funciona em cascata — só avança pra próxima fonte paga se a anterior não trouxe RH e Financeiro completos (nome + cargo + contato), pra economizar cota.

---

## Como funciona (cascata)

1. **Receita Federal** (grátis, sem chave) — BrasilAPI → CNPJá → ReceitaWS, nessa ordem de fallback. Só entra se a entrada for um CNPJ.
2. **Site oficial** (grátis) — descobre o domínio e faz scraping das páginas de contato.
3. **SerpAPI** — busca geral de contato + LinkedIn da empresa e das pessoas de RH/Financeiro.
4. **Apify** — scraping de funcionários do LinkedIn da empresa.
5. **Hunter.io** — encontra o e-mail de uma pessoa já identificada (nome + domínio).
6. **Clay** — mesmo papel do Hunter, como fallback.
7. **PhantomBuster** — scraping de LinkedIn mais profundo, assíncrono (última opção, aceita demorar mais). *Ainda não totalmente configurado — falta `PHANTOMBUSTER_AGENT_ID`.*

**Gemini** entra como camada auxiliar de classificação, não como fonte de busca: ajuda a escolher, entre os resultados que o SerpAPI já trouxe, qual é o perfil de LinkedIn certo de RH/Financeiro — nunca inventa ou completa dado por conta própria (só pode escolher um link que já esteja na lista de resultados).

Cada busca é cacheada por empresa por 30 dias, e o cache invalida sozinho se a lista de contatos do HubSpot mudar entre uma busca e outra da mesma empresa.

---

## Pré-requisitos

- Python instalado (python.org/downloads)
- Chave da SerpAPI (serpapi.com) — obrigatória
- As demais são opcionais — se a chave não estiver no `.env`, a fonte correspondente é simplesmente pulada:
  - Apify (apify.com)
  - Hunter.io (hunter.io)
  - Clay (clay.com)
  - PhantomBuster (phantombuster.com) — também precisa do ID de um agente já configurado na sua conta
  - Gemini API (aistudio.google.com) — só usada como classificador de RH/Financeiro

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

3. Abre o browser em: http://localhost:10000
4. Digita o nome, domínio ou CNPJ da empresa
5. (Opcional) cola a lista de contatos que você já tem no HubSpot — a ferramenta não repete contato que já esteja nela, e avisa quando tudo que achou já é conhecido

---

## Limites de cota

O app controla cota mensal por fonte (arquivo `quota_uso.json`, reseta todo mês). Quando uma fonte bate no limite, a cascata pula pra próxima e continua funcionando — só para de usar aquela fonte específica até o mês virar.

| Fonte | Limite/mês | Observação |
|---|---|---|
| SerpAPI | 90 | plano free é 100/mês, deixamos 10 de colchão |
| Apify | 40 | dentro dos $5 grátis em créditos |
| Hunter.io | 20 | free tier real é 25/mês, deixamos margem |
| Clay | 20 | provisório até confirmar o plano contratado |
| PhantomBuster | 20 | provisório — tem limite de slots simultâneos também, ainda não tratado |
| Gemini | 40 | dentro do tier gratuito real da API |

Uma empresa pesquisada pode usar de 1 a ~6 chamadas de SerpAPI dependendo de quanto dado já foi achado nas fontes gratuitas (Receita Federal, site oficial).

---

## Para encerrar

Volta no terminal e aperta Ctrl+C
