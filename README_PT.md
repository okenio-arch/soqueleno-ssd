# Soqueleno Discovery Infrastructure (SDI)

**Versão do serviço:** 0.9.0

A SDI é uma infraestrutura focada de descoberta, indexação e evidência para fornecedores jurídicos do Reino Unido. É uma fonte de pesquisa para o Scout, não um substituto do Scout e não um motor de pesquisa geral da Internet.

## O que a SDI faz

- Importa datasets oficiais e mantém o respetivo contexto de publicação e data de referência.
- Descobre organizações através de seeds HTML aprovadas e expansão recursiva limitada.
- Enriquece websites públicos já associados a organizações, respeitando robots.txt e limites de recolha.
- Indexa páginas e organizações em SQLite FTS5.
- Deduplica páginas e categorias repetidas.
- Guarda evidência por campo, com URL, autoridade, tipo de evidência, data de recolha, data de publicação, data de referência e confiança.
- Expõe pesquisa e perfis através de uma API interna autenticada.
- Disponibiliza um Founder Console local para executar ciclos, consultar fontes, ver métricas, pesquisar e abrir evidências.

## O que não faz

- Não pesquisa nem indexa a Internet inteira.
- Não confirma elegibilidade legal nem substitui a decisão do Scout.
- Não transforma texto de websites em autorização regulatória confirmada.
- Não contorna autenticação, paywalls, CAPTCHA, robots.txt ou restrições de reutilização.
- Não ativa automaticamente fontes cuja autorização, credenciais ou adaptador estejam em falta.

## Estado das fontes

| Fonte | Estado | Limite |
|---|---|---|
| IAA FOI register | Importador oficial ativo | Snapshot com data de referência 09-07-2026; não é estado de autorização em tempo real. |
| LAA provider directory | Ativo | Diretório de legal aid; importar apenas categorias Immigration/Asylum. |
| LAA South West remote access | Ativo | Apenas o âmbito regional declarado. |
| IAA Adviser Finder | Bloqueado | O aviso oficial de 09-11-2026 informa que Adviser Finder e Adviser Register continuam indisponíveis. |
| SRA Data Share API V1 | Bloqueado até registo e chave | A API é gratuita, mas requer conta/subscrição e atribuição pública à SRA. O adaptador ainda não está implementado. |
| Law Society Find a Solicitor | Bloqueado | Não automatizar sem autorização escrita compatível com reutilização comercial. |

Não existe atualmente uma seed HTML ativa no catálogo. A descoberta recursiva está implementada, mas permanece sem uma fonte inicial aprovada. Não inventar uma seed para fazer os contadores parecerem ocupados.

Consulta o inventário detalhado em [docs/SDI_ENGINEERING_STATUS.md](docs/SDI_ENGINEERING_STATUS.md).

## Instalação Windows / PowerShell

Na pasta do repositório:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py init
python main.py load-sources
```

Se o PowerShell bloquear a ativação do ambiente virtual, executa apenas nessa janela:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Importação IAA manual

```powershell
python main.py inspect-iaa --file data/IAA_register.xlsx
python main.py import-iaa --file data/IAA_register.xlsx --dry-run
python main.py import-iaa --file data/IAA_register.xlsx
```

O dry-run deve ser verificado antes de importar. A importação automática a partir de GOV.UK é feita pelo ciclo de descoberta quando a fonte estiver vencida e ativa.

## Chaves API: separar Scout e Founder Console

A chave do Scout deve ser read-only:

```powershell
python main.py create-key --client scout
```

A chave do Founder Console precisa de permissões de execução e gestão de fontes. Cria-a separadamente:

```powershell
python main.py create-key --client sdi-console --scopes organisations:read,discovery:run,sources:manage
```

A chave é apresentada uma única vez. Guarda-a num gestor de segredos ou local seguro. A base de dados guarda apenas o hash. Não coloques a chave em código, GitHub ou screenshots.

Scopes permitidos:
- organisations:read
- discovery:run
- sources:manage

## Executar o Founder Console

```powershell
python main.py serve
```

Abre:

- Console: http://127.0.0.1:8100/
- Health: http://127.0.0.1:8100/health
- API docs: http://127.0.0.1:8100/docs

Cola a chave sdi-console no painel. A chave fica apenas na memória da página e não é guardada em localStorage.

A execução automática e o scheduler estão desativados por defeito. O utilizador inicia os ciclos manualmente no painel. Para ativar o scheduler, altera explicitamente no .env:

```env
SDI_SCHEDULER_ENABLED=true
SDI_AUTO_RUN_ON_START=false
SDI_SCHEDULE_HOURS=24
```

## Pesquisa e API para o Scout

Pesquisa de organizações:

```powershell
$headers = @{ Authorization = "Bearer COLOCA_A_CHAVE_SCOUT_AQUI" }
Invoke-RestMethod -Uri "http://127.0.0.1:8100/api/v1/organisations/search?q=immigration%20solicitor&limit=20" -Headers $headers
```

Pesquisa combinada de organizações e páginas:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8100/api/v1/search?q=skilled%20worker&limit=30" -Headers $headers
```

Perfil com evidências:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8100/api/v1/organisations/1" -Headers $headers
```

Cada campo relevante deve ser interpretado com a respetiva evidência. official_snapshot, website_observation e discovery_observation não são equivalentes. Os valores de confiança são pesos heurísticos, não probabilidades estatísticas.

## Segurança do crawler

- Apenas HTTP/HTTPS, sem credenciais e sem portas não padrão.
- DNS público validado e socket ligado diretamente a um IP fixado; Host e SNI continuam a usar o hostname original.
- Clientes sem proxies herdados do ambiente.
- Respostas em streaming com limites de bytes: robots.txt 512 KB, HTML/sitemaps 2 MB, XLSX oficial 35 MB.
- Redirecionamentos manuais, limitados e sujeitos a allowlists.
- robots.txt consultado antes das páginas; falhas de consulta são tratadas de forma conservadora.
- Sitemap indexes limitados a cinco ficheiros por origem.
- Fragments e parâmetros de tracking removidos da identidade da página.
- Conteúdo duplicado por organização excluído do índice de pesquisa.
- Descoberta recursiva com limites de profundidade/domínios/candidatos, persistência da fila e até três tentativas por domínio.

## Testes

```powershell
python -m compileall -q sdi tests main.py
python -m unittest discover -s tests -v
```

Os testes offline não substituem testes de rede reais, migração sobre uma cópia da base SQLite existente ou validação do comportamento do Founder Console no computador local.

## Dados e configuração

- data/sdi.sqlite3: base operacional.
- config/sources.json: catálogo declarativo das fontes.
- .env: limites e configuração local, nunca partilhar.
- storage/datasets: snapshots oficiais descarregados.
- api_clients: hashes das chaves por consumidor.
- A API escuta por defeito em 127.0.0.1. Não a exponhas à Internet sem autenticação, TLS, gestão de segredos e revisão de segurança.
