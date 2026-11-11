param(
    [string]$ProjectPath = (Join-Path $HOME 'Documents\SOQUELENO SSD')
)

$ErrorActionPreference = 'Stop'
$SourcePath = Join-Path $PSScriptRoot 'soqueleno-sdi'

if (-not (Test-Path (Join-Path $SourcePath 'main.py'))) {
    throw "Pasta de origem inválida. Esperava encontrar: $SourcePath\main.py"
}
if (-not (Test-Path (Join-Path $ProjectPath 'main.py'))) {
    throw "Pasta SDI instalada não encontrada em: $ProjectPath. Indica o caminho correto com -ProjectPath."
}

# Only replace application code and documentation. Never touch .env, virtualenv,
# SQLite databases, logs, or storage owned by the running installation.
$Directories = @('sdi', 'tests', 'docs', 'config')
foreach ($Name in $Directories) {
    $From = Join-Path $SourcePath $Name
    if (Test-Path $From) {
        Copy-Item -Path $From -Destination $ProjectPath -Recurse -Force
    }
}

$Files = @('main.py', 'requirements.txt', 'README_PT.md', 'CHANGELOG.md', '.gitignore', '.env.example')
foreach ($Name in $Files) {
    $From = Join-Path $SourcePath $Name
    if (Test-Path $From) {
        Copy-Item -Path $From -Destination $ProjectPath -Force
    }
}

Write-Host ''
Write-Host 'SDI 0.7.0: código atualizado.' -ForegroundColor Green
Write-Host 'Preservados: .env, .venv, data, logs e storage.'
Write-Host "Instalação: $ProjectPath"
Write-Host 'Próximo: ativa o ambiente virtual e executa python main.py init e python main.py load-sources.'
