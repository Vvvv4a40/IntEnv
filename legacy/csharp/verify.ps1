[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$enviSolution = Join-Path $PSScriptRoot 'Envi.slnx'
$enviTestProject = Join-Path $PSScriptRoot 'tests\Envi.Tests\Envi.Tests.csproj'
$enviDesktopAssembly = Join-Path $PSScriptRoot 'src\Envi.Desktop\bin\Debug\net10.0-windows\Envi.Desktop.dll'

& dotnet restore $enviSolution --configfile (Join-Path $PSScriptRoot 'NuGet.Config')
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& dotnet build $enviSolution --no-restore '-p:TargetPlatformDisplayName=Windows'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& dotnet run --no-restore --no-build --project $enviTestProject
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& dotnet $enviDesktopAssembly --settings (Join-Path $PSScriptRoot 'envi.settings.json') --smoke-test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'Envi: сборка, автономные тесты и инициализация окна прошли.'
