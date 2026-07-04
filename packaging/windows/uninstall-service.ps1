param(
  [string]$ServiceName = "GozikYTMusicPlugin"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Nssm = Join-Path $Root "nssm.exe"

if (!(Test-Path $Nssm)) {
  throw "Missing nssm.exe in package root: $Root"
}

& $Nssm stop $ServiceName 2>$null | Out-Null
& $Nssm remove $ServiceName confirm
Write-Host "Removed service $ServiceName."
