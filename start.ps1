[CmdletBinding()]
param([string]$Python = '')

$ErrorActionPreference = 'Stop'
$enviSettings = Join-Path $PSScriptRoot 'envi.settings.json'
if (-not $Python) {
    $enviVenv = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $enviVenv) { $Python = $enviVenv }
    else {
        $enviCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $enviCommand) { throw 'Нужен Python 3.11+ с Tkinter. Можно указать -Python полный_путь.' }
        $Python = $enviCommand.Source
    }
}
# The key is inherited from this terminal; it is not saved to a file or printed.
Push-Location $PSScriptRoot
try {
    & $Python -X utf8 -m envi --settings $enviSettings
    $enviExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $enviExitCode
