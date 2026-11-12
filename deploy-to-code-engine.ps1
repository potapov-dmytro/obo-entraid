#Requires -Version 5.1
<#
.SYNOPSIS
  Build the portal image, push it to IBM Container Registry (ICR) and create or
  update the IBM Code Engine application.

.DESCRIPTION
  All settings are read from .env (see .env.example):
    - application variables (TENANT_ID, CLIENT_ID, ... WXO_*) are passed to
      Code Engine as environment variables;
    - deployment variables (IBMCLOUD_API_KEY, ICR_*, CE_*) drive this script and
      are NOT sent to the app.
  Prerequisites: docker or podman, ibmcloud CLI with the code-engine plugin
  (ibmcloud plugin install code-engine), wxo-client-private.pem and
  wxo-ibm-public.pem in this folder.

.EXAMPLE
  .\deploy-to-code-engine.ps1
.EXAMPLE
  .\deploy-to-code-engine.ps1 -Tag v1.2 -EnvFile .env.production
#>
[CmdletBinding()]
param(
  [string]$EnvFile = (Join-Path $PSScriptRoot '.env'),
  [string]$Tag = (Get-Date -Format 'yyyyMMdd-HHmmss')
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Check([string]$what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

# ── 1. Read .env ────────────────────────────────────────────────────────────
if (-not (Test-Path $EnvFile)) { throw "Env file not found: $EnvFile (copy .env.example to .env)" }
$cfg = @{}
foreach ($line in Get-Content $EnvFile -Encoding UTF8) {
  if ($line -match '^\s*(#|$)') { continue }
  if ($line -match '^\s*([^=]+?)\s*=\s*(.*?)\s*$') { $cfg[$matches[1]] = $matches[2].Trim('"').Trim("'") }
}
function Cfg([string]$k, [string]$default = '') {
  $v = $cfg[$k]; if (-not $v) { $v = [Environment]::GetEnvironmentVariable($k) }
  if (-not $v) { $v = $default }
  if (-not $v) { throw "Missing $k in $EnvFile" }
  return $v
}
$ApiKey    = Cfg 'IBMCLOUD_API_KEY'
$Registry  = Cfg 'ICR_REGISTRY' 'us.icr.io'
$Namespace = Cfg 'ICR_NAMESPACE'
$ImageName = Cfg 'IMAGE_NAME' 'wxo-auth-token-server'
$Region    = Cfg 'CE_REGION' 'eu-de'
$RG        = Cfg 'CE_RESOURCE_GROUP'
$Project   = Cfg 'CE_PROJECT'
$App       = Cfg 'CE_APP' 'wxo-auth-token-server'
$RegSecret = Cfg 'CE_REGISTRY_SECRET' 'icr-secret'
$Image     = "$Registry/$Namespace/${ImageName}:$Tag"

# Application variables forwarded to Code Engine (empty values are skipped)
$AppKeys = 'TENANT_ID','CLIENT_ID','CLIENT_SECRET','SSO_SCOPE','REDIRECT_URI','SESSION_SECRET',
           'WXO_AGENT_ID','WXO_ORCHESTRATION_ID','WXO_AGENT_ENV_ID','WXO_HOST_URL','WXO_CRN'
$EnvArgs = @()
foreach ($k in $AppKeys) { if ($cfg[$k]) { $EnvArgs += '--env'; $EnvArgs += "$k=$($cfg[$k])" } }

foreach ($f in 'Dockerfile','main.py','requirements.txt','wxo-client-private.pem','wxo-ibm-public.pem') {
  if (-not (Test-Path $f)) { throw "Missing file: $f" }
}

# ── 2. Build and push the image ─────────────────────────────────────────────
# podman is preferred when available (docker buildx is often broken on Podman/Rancher hosts)
$Ctr = if (Get-Command podman -ErrorAction SilentlyContinue) { 'podman' } elseif (Get-Command docker -ErrorAction SilentlyContinue) { 'docker' } else { throw 'Install docker or podman' }
Step "$Ctr login $Registry"
$ApiKey | & $Ctr login $Registry -u iamapikey --password-stdin; Check 'registry login'
Step "$Ctr build $Image"
& $Ctr build --platform linux/amd64 -t $Image .; Check 'build'
Step "$Ctr push $Image"
& $Ctr push $Image; Check 'push'

# ── 3. Target the Code Engine project ───────────────────────────────────────
Step "ibmcloud login ($Region / $RG / $Project)"
$env:IBMCLOUD_VERSION_CHECK = 'false'
$ErrorActionPreference = 'Continue'   # ibmcloud writes progress to stderr
ibmcloud login --apikey $ApiKey -r $Region -g $RG -q | Out-Host; Check 'ibmcloud login'
ibmcloud ce project select -n $Project | Out-Host; Check 'ce project select'

# ── 4. Registry secret (recreated each time so a rotated key is picked up) ──
Step "registry secret $RegSecret"
ibmcloud ce registry delete -n $RegSecret -f 2>$null | Out-Null
ibmcloud ce registry create -n $RegSecret -s $Registry -u iamapikey -p $ApiKey | Out-Host; Check 'ce registry create'

# ── 5. Create the app on first run, update it afterwards ────────────────────
ibmcloud ce app get -n $App 2>$null | Out-Null
$verb = if ($LASTEXITCODE -eq 0) { 'update' } else { 'create' }
Step "ce app $verb $App"
$extra = if ($verb -eq 'create') { @('--port','8080','--min-scale','1') } else { @() }
ibmcloud ce app $verb -n $App --image $Image --registry-secret $RegSecret @extra @EnvArgs --wait --wait-timeout 600 | Out-Host
Check "ce app $verb"
$ErrorActionPreference = 'Stop'

$url = (ibmcloud ce app get -n $App -o url 2>$null | Select-Object -First 1)
Write-Host "`nOK - deployed $Image" -ForegroundColor Green
Write-Host "URL: $url"
if ($cfg['REDIRECT_URI'] -ne "$url/auth/callback") {
  Write-Warning "REDIRECT_URI in .env is '$($cfg['REDIRECT_URI'])'. Set it to '$url/auth/callback' (and add it to the Entra App Registration), then run this script again."
}
