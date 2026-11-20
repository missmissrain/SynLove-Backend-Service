# ModelHub 单实例启动入口。
# 参数 Python 使用服务器现有解释器；Root 为后台仓库根目录；Port 为管理接口端口。
param(
  [string]$Python = "python",
  [string]$Root = (Split-Path $PSScriptRoot -Parent),
  [int]$Port = 8810,
  [string]$Config = ""
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$entry = Join-Path $Root "modelHub\runModelHub.py"
$argsList = @($entry, "--root", $Root, "--host", "127.0.0.1", "--port", $Port)
if ($Config) { $argsList += @("--config", $Config) }
& $Python @argsList
exit $LASTEXITCODE
