# OwnAI first-time setup (Windows PowerShell). Creates .env with fresh secrets and config\models.yaml.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1            (vLLM example config)
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Ollama    (Ollama example config)
param([switch]$Ollama)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function New-Secret([int]$bytes) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return [Convert]::ToBase64String($b).Replace('+', '-').Replace('/', '_')
}

if (-not (Test-Path ".env")) {
    $content = Get-Content ".env.example" -Raw
    $sandbox = (New-Secret 32).TrimEnd('=')
    $content = $content -replace '(?m)^OWNAI_SECRET_KEY=.*$', ("OWNAI_SECRET_KEY=" + (New-Secret 48).TrimEnd('='))
    # Fernet keys are url-safe base64 of 32 random bytes (padding kept)
    $content = $content -replace '(?m)^OWNAI_ENCRYPTION_KEY=.*$', ("OWNAI_ENCRYPTION_KEY=" + (New-Secret 32))
    $content = $content -replace '(?m)^OWNAI_SANDBOX_TOKEN=.*$', ("OWNAI_SANDBOX_TOKEN=" + $sandbox)
    $content = $content -replace '(?m)^SANDBOX_TOKEN=.*$', ("SANDBOX_TOKEN=" + $sandbox)
    # UTF-8 without BOM (Windows PowerShell 5.1's "-Encoding UTF8" would add a BOM)
    [System.IO.File]::WriteAllText((Join-Path (Get-Location) ".env"), $content, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "Created .env with new secrets."
} else {
    Write-Host ".env already exists - left unchanged."
}

if (-not (Test-Path "config\models.yaml")) {
    if ($Ollama) { Copy-Item "config\models.ollama.example.yaml" "config\models.yaml" }
    else { Copy-Item "config\models.example.yaml" "config\models.yaml" }
    Write-Host "Created config\models.yaml - edit it to point at your models."
}

Write-Host "Next steps (Docker Desktop):"
Write-Host "  docker compose -f deploy/docker-compose.yml --env-file .env --profile build build"
Write-Host "  docker compose -f deploy/docker-compose.yml --env-file .env up -d    ->  http://localhost:8080"
