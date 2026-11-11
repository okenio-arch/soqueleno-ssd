# Soqueleno Discovery Infrastructure (SDI) 0.5.0

**Entrega:** fundação, catálogo de fontes, importação IAA, crawler web controlado, perfis organizacionais e pesquisa FTS5/API interna autenticada.

## O que esta versão faz

1. Mantém catálogo das fontes e respetivos métodos/estado de acesso.
2. Importa o snapshot XLSX da IAA, preservando publicação e data de referência.
3. Guarda organizações e evidências por campo, com URL e datas.
4. Permite crawling manual de um website de organização já identificada. Segue apenas ligações do mesmo domínio, consulta `robots.txt`, limita profundidade/páginas e rejeita destinos privados/não públicos.
5. Extrai título, descrição, texto e sinais de serviços de imigração. Não transforma sinais em confirmação legal.
6. Pesquisa organizações através de SQLite FTS5.
7. Expõe endpoints internos protegidos por Bearer API key.

## O que NÃO faz

- Não faz crawling global nem corre em background.
- Não contorna logins, paywalls, CAPTCHAs, robots.txt ou limites.
- Não faz scraping automático dos diretórios Law Society/SRA/IAA. Esses conectores só devem ser implementados após confirmar formatos, permissões e condições de utilização.
- Não confirma que uma autorização IAA continua ativa: o dataset importado é um snapshot de 09-07-2026, publicado em 25-09-2026.
- Não substitui a investigação comercial do Scout.

## Instalação Windows (PowerShell)

Extrai a pasta `soqueleno-sdi` para:
`C:\Users\<teu utilizador>\Documents\soqueleno-sdi`

Abre PowerShell nessa pasta:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py init
python main.py load-sources
python main.py create-key --client scout
```

Se o PowerShell bloquear a ativação do ambiente virtual, executa apenas nessa janela:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

## Importar IAA

Descarrega manualmente o XLSX da página oficial:
https://www.gov.uk/government/publications/register-of-currently-registered-immigration-advice-organisations

Coloca o ficheiro em `data\IAA_register.xlsx`.

```powershell
python main.py inspect-iaa --file data/IAA_register.xlsx
python main.py import-iaa --file data/IAA_register.xlsx --dry-run
python main.py import-iaa --file data/IAA_register.xlsx
```

Confirma o relatório do `dry-run` antes de remover `--dry-run`. O importador exige cabeçalhos reconhecidos e deduplica pelo número de registo IAA.

## Consultar organizações

```powershell
python main.py list
python main.py search --query "immigration London"
```

## Crawling manual e limitado

Primeiro obtém o `organisation_id` com `python main.py list`. Depois:

```powershell
python main.py crawl --organisation-id 1 --url https://www.exemplo.co.uk --max-pages 10 --max-depth 1
```

Começa com limites pequenos. O crawler só visita o website indicado e ligações internas do mesmo domínio. Ligações externas são guardadas como candidatos, não visitadas automaticamente. A consulta de `robots.txt` é feita antes de cada página. Um erro de DNS, robots ou HTTP fica registado no relatório.

## API interna

A chave é criada por consumidor. A base de dados guarda apenas o hash, não a chave em texto. O comando mostra a chave uma única vez. Guarda-a no ficheiro de configuração privado do Scout, nunca no código-fonte. Para Auren, executa `python main.py create-key --client auren` e guarda a chave separadamente. Repetir o comando para um consumidor roda/substitui a respetiva chave anterior.

Inicia o serviço:

```powershell
python main.py serve
```

Verificação de saúde (não requer chave): `http://127.0.0.1:8100/health`

Pesquisa (requer Bearer token):

```powershell
$headers = @{ Authorization = "Bearer COLOCA_A_CHAVE_SCOUT_AQUI" }
Invoke-RestMethod -Uri "http://127.0.0.1:8100/api/v1/organisations/search?q=immigration&limit=10" -Headers $headers
```

Nota: o `.env` é lido pela aplicação Python. Para usar a chave na variável PowerShell, carrega-a manualmente nessa sessão ou copia temporariamente o valor, sem o guardar em scripts.

Perfil: `GET /api/v1/organisations/{organisation_id}` com o mesmo header.

## Testes offline

```powershell
python -m unittest discover -s tests -v
```

Os testes usam dados sintéticos e não fazem chamadas à Internet. Não provam que o dataset IAA real foi importado nem que websites reais autorizam crawling.

## Dados e segurança

- `data/sdi.sqlite3`: base de dados operacional.
- `config/sources.json`: catálogo declarativo das fontes.
- `.env`: limites e configuração local, nunca partilhar.
- `api_clients`: hashes das chaves por consumidor; as chaves em texto são mostradas apenas uma vez.
- `storage/`: reservado para artefactos futuros; conteúdo extraído fica atualmente na base de dados.
- A API escuta por defeito apenas em `127.0.0.1`.
- Não expor a API à Internet sem autenticação/autorização mais robusta, TLS, gestão de segredos e revisão de segurança.
