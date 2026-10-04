[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$enviProject = Join-Path $PSScriptRoot 'src\Envi.Desktop\Envi.Desktop.csproj'
$enviSettings = Join-Path $PSScriptRoot 'envi.settings.json'

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw 'Для запуска нужен .NET SDK 10.'
}

# The key is inherited from this terminal; it is not saved to a file or printed.
& dotnet restore $enviProject --configfile (Join-Path $PSScriptRoot 'NuGet.Config')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& dotnet run --no-restore --project $enviProject -- --settings $enviSettings
exit $LASTEXITCODE
