# =========================================
# Loan Pre-Screening Portal — Deploy to Azure Container Apps
# =========================================
# Shared infra (already exists):
#   ACR:         acrsysintcommoneus
#   ACA Env:     aca-env-sysint-eus  (rg-sysint-common-eus)
#   Key Vault:   kv-sysint-common-eus
#
# Key Vault secrets required (already created):
#   foundry-sysint-01-endpoint   → AZURE_MS_FOUNDRY_ENDPOINT
#   di-free-sysint-endpoint      → DOCUMENT_INTELLIGENCE_ENDPOINT
#   di-free-sysint-key           → DOCUMENT_INTELLIGENCE_KEY
#
# Usage (first time):
#   .\infra\deploy.ps1
#
# Usage (redeploy after code change):
#   .\infra\deploy.ps1 -Redeploy -RevisionSuffix "v2"
# =========================================

param(
  [string]$ResourceGroup   = "rg-sysint-loan-portal-eus",
  [string]$AppName         = "loan-prescreening-portal",
  [string]$AcrName         = "acrsysintcommoneus",
  [string]$AcaEnv          = "aca-env-sysint-eus",
  [string]$AcaEnvRg        = "rg-sysint-common-eus",
  [string]$KeyVault        = "kv-sysint-common-eus",
  [string]$ImageName       = "loan-prescreening-portal",
  [string]$RevisionSuffix  = "v1",
  [switch]$Redeploy
)

$ErrorActionPreference = "Stop"
$Image    = "$AcrName.azurecr.io/${ImageName}:latest"
$AppDir   = "$PSScriptRoot\.."

Write-Host "`nLoan Pre-Screening Portal — Azure Container Apps Deploy" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

# ---- Step 1: Build image in ACR (no Docker needed locally) ----
Write-Host "`n[1/3] Building image in ACR: $Image ..." -ForegroundColor Yellow

az acr build `
  --registry $AcrName `
  --image "${ImageName}:latest" `
  --file "$AppDir\Dockerfile" `
  $AppDir

Write-Host "Image built and pushed to ACR." -ForegroundColor Green

# ---- Step 2: Create or Update Container App ----
if ($Redeploy) {
  Write-Host "`n[2/3] Redeploying Container App: $AppName ..." -ForegroundColor Yellow

  az containerapp update `
    --name $AppName `
    --resource-group $ResourceGroup `
    --image $Image `
    --revision-suffix $RevisionSuffix

} else {
  Write-Host "`n[2/3] Creating Container App: $AppName ..." -ForegroundColor Yellow

  # Create resource group if it doesn't exist
  az group create --name $ResourceGroup --location eastus

  # Get full resource ID of the shared ACA environment
  $EnvId = az containerapp env show `
    --name $AcaEnv `
    --resource-group $AcaEnvRg `
    --query id --output tsv

  az containerapp create `
    --name $AppName `
    --resource-group $ResourceGroup `
    --environment $EnvId `
    --image $Image `
    --registry-server "$AcrName.azurecr.io" `
    --min-replicas 0 `
    --max-replicas 2 `
    --cpu 0.5 `
    --memory 1.0Gi `
    --ingress external `
    --target-port 5001 `
    --system-assigned `
    --secrets `
      "foundry-sysint-01-endpoint=keyvaultref:https://$KeyVault.vault.azure.net/secrets/foundry-sysint-01-endpoint,identityref:system" `
      "di-free-sysint-endpoint=keyvaultref:https://$KeyVault.vault.azure.net/secrets/di-free-sysint-endpoint,identityref:system" `
      "di-free-sysint-key=keyvaultref:https://$KeyVault.vault.azure.net/secrets/di-free-sysint-key,identityref:system" `
    --env-vars `
      "AZURE_MS_FOUNDRY_ENDPOINT=secretref:foundry-sysint-01-endpoint" `
      "DOCUMENT_INTELLIGENCE_ENDPOINT=secretref:di-free-sysint-endpoint" `
      "DOCUMENT_INTELLIGENCE_KEY=secretref:di-free-sysint-key" `
      "DOCUMENT_INTELLIGENCE_MODEL_ID=loan-form-extractor-2" `
      "PORT=5001"
}

# ---- Step 3: Get live URL ----
Write-Host "`n[3/3] Fetching live URL..." -ForegroundColor Yellow

$fqdn = az containerapp show `
  --name $AppName `
  --resource-group $ResourceGroup `
  --query "properties.configuration.ingress.fqdn" `
  --output tsv

Write-Host "`nDone! Portal live at:" -ForegroundColor Green
Write-Host "https://$fqdn" -ForegroundColor White

# ---- Redeploy reminder ----
Write-Host "`nTo redeploy after code changes:" -ForegroundColor DarkGray
Write-Host "  .\infra\deploy.ps1 -Redeploy -RevisionSuffix `"v2`"" -ForegroundColor DarkGray

# ---- Key Vault reminder ----
Write-Host "`nEnsure these secrets exist in Key Vault before deploying:" -ForegroundColor DarkGray
Write-Host "  foundry-sysint-01-endpoint" -ForegroundColor DarkGray
Write-Host "  di-free-sysint-endpoint" -ForegroundColor DarkGray
Write-Host "  di-free-sysint-key" -ForegroundColor DarkGray
