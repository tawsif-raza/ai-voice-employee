param (
    [string]$Port = $env:PORT,
    [string]$HostName = "localhost"
)

if (-not $Port) { $Port = "8000" }
$BaseUrl = "http://${HostName}:${Port}"

Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "         AI Voice Agent — Canary Deployment Verification" -ForegroundColor Cyan
Write-Host "========================================================================" -ForegroundColor Cyan
Write-Host "Target Base URL: $BaseUrl"
Write-Host "Current Persistence Mode: $($env:PERSISTENCE_MODE)"
Write-Host "Current Auth Mode: $($env:AUTH_MODE)"
Write-Host "Current LLM Provider: $($env:LLM_PROVIDER)"
Write-Host "------------------------------------------------------------------------"

# 1. Environment Configuration Check
Write-Host -NoNewline "[1/4] Checking required environment configuration... "
if ($env:AUTH_MODE -in @("production", "oidc")) {
    if (-not $env:OIDC_ISSUER_URL -or -not $env:OIDC_AUDIENCE -or -not $env:OIDC_JWKS_URL) {
        Write-Host "FAILED!" -ForegroundColor Red
        Write-Error "AUTH_MODE=production requires OIDC_ISSUER_URL, OIDC_AUDIENCE, and OIDC_JWKS_URL"
        exit 1
    }
}

if ($env:PERSISTENCE_MODE -eq "production") {
    if (-not $env:DATABASE_URL) {
        Write-Host "FAILED!" -ForegroundColor Red
        Write-Error "PERSISTENCE_MODE=production requires DATABASE_URL"
        exit 1
    }
}
Write-Host "OK" -ForegroundColor Green

# 2. Liveness Check (/health)
Write-Host -NoNewline "[2/4] Verifying liveness endpoint (/health)... "
try {
    $resp = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec 5
    if ($resp.status -eq "ok") {
        Write-Host "OK (status: ok)" -ForegroundColor Green
    } else {
        Write-Host "FAILED" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "FAILED ($($_.Exception.Message))" -ForegroundColor Red
    exit 1
}

# 3. Voice Provider Health Check (/health/voice)
Write-Host -NoNewline "[3/4] Verifying voice provider status (/health/voice)... "
try {
    $vResp = Invoke-RestMethod -Uri "$BaseUrl/health/voice" -Method Get -TimeoutSec 5
    if ($vResp.status -eq "ok") {
        Write-Host "OK (voice active: $($vResp.voice_manager_active))" -ForegroundColor Green
    } else {
        Write-Host "FAILED" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "FAILED ($($_.Exception.Message))" -ForegroundColor Red
    exit 1
}

# 4. Readiness Check (/ready)
Write-Host -NoNewline "[4/4] Verifying readiness endpoint (/ready)... "
try {
    $rResp = Invoke-RestMethod -Uri "$BaseUrl/ready" -Method Get -TimeoutSec 5
    if ($rResp.ready -eq $true) {
        Write-Host "OK (ready: true)" -ForegroundColor Green
    } else {
        Write-Host "FAILED (ready: false)" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "FAILED ($($_.Exception.Message))" -ForegroundColor Red
    exit 1
}

Write-Host "------------------------------------------------------------------------"
Write-Host "Canary verification SUCCEEDED: Service is healthy, ready, and operational." -ForegroundColor Green
Write-Host "========================================================================" -ForegroundColor Cyan
exit 0