param(
  [string]$ProjectId = "",
  [string]$Region = "australia-southeast1",
  [string]$ServiceName = "planet-low-tide-browser",
  [string]$Memory = "2Gi",
  [string]$Cpu = "1",
  [string]$MaxInstances = "1",
  [bool]$AllowUnauthenticated = $true
)

$ErrorActionPreference = "Stop"

if (-not $ProjectId) {
  Write-Error "Pass -ProjectId your-google-cloud-project-id"
}

gcloud config set project $ProjectId

$deployArgs = @(
  "run", "deploy", $ServiceName,
  "--source", ".",
  "--region", $Region,
  "--memory", $Memory,
  "--cpu", $Cpu,
  "--timeout", "3600",
  "--min-instances", "0",
  "--max-instances", $MaxInstances,
  "--set-env-vars", "PLANET_BROWSER_NO_OPEN=1"
)

if ($AllowUnauthenticated) {
  $deployArgs += "--allow-unauthenticated"
} else {
  $deployArgs += "--no-allow-unauthenticated"
}

gcloud @deployArgs
