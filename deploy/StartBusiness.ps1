# 后台业务启动入口。参数Python使用现有依赖环境；ExistingNodeConfig仅首次复用中转连接配置。
param(
  [string]$Python = "python",
  [string]$ExistingNodeConfig,
  [string]$RssHubEntry,
  [switch]$DirectOnly
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$serviceRoot = Split-Path $PSScriptRoot -Parent
$setupArgs = @((Join-Path $serviceRoot "businessRuntime\BackendSetup.py"))
if ($ExistingNodeConfig) { $setupArgs += @("--existing-node-config", $ExistingNodeConfig) }
& $Python @setupArgs
if ($LASTEXITCODE -ne 0) { throw "后台配置初始化失败" }
$runArgs = @((Join-Path $serviceRoot "businessRuntime\BackendSupervisor.py"),
  "--config", (Join-Path $serviceRoot "businessRuntime\config\serviceConfig.json"))
if (-not $DirectOnly) {
  $runArgs += @("--relay-config", (Join-Path $serviceRoot "relayNode\config\node.json"))
}
if ($RssHubEntry) { $runArgs += @("--rsshub-entry", $RssHubEntry) }
& $Python @runArgs
exit $LASTEXITCODE
