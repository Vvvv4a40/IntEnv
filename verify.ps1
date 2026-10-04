[CmdletBinding()]
param([string]$Python = '')

$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $enviVenv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $enviVenv) { $Python = $enviVenv }
    else {
        $enviCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $enviCommand) { throw 'Нужен Python 3.11+ с Tkinter. Можно указать -Python полный_путь.' }
        $Python = $enviCommand.Source
    }
}
Push-Location $PSScriptRoot
try {
    & $Python -X utf8 -m compileall -q envi tests
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -X utf8 -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -X utf8 -m envi --settings (Join-Path $PSScriptRoot 'envi.settings.json') --smoke-test
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host 'Envi: Python, автономные тесты и инициализация окна проверены.'
}
finally { Pop-Location }
