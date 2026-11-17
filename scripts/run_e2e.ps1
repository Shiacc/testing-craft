# 一键跑完整流程（Windows 薄包装）
#
# 这里只做三件事：定位仓库根目录、透传参数、给出中文提示。
# 真正的编排逻辑在 python -m atf e2e（跨平台同一份实现，CI 直接可用）。
#
# 用法：
#   .\scripts\run_e2e.ps1                                  # 默认：mock 缺陷流，禅道+Jira 都走一遍
#   .\scripts\run_e2e.ps1 -Filing dry-run                  # 只打印将提交什么，不联网
#   .\scripts\run_e2e.ps1 -Filing submit -Tracker zentao   # 提交真实禅道（需环境变量凭据）
#   .\scripts\run_e2e.ps1 -Repeat 3                        # 连跑 3 次并对比一致性（验收用）
#   .\scripts\run_e2e.ps1 -KeepServer                      # 保留被测服务，便于手工排查

[CmdletBinding()]
param(
    [ValidateSet('dry-run', 'mock', 'submit')]
    [string]$Filing = 'mock',

    [ValidateSet('zentao', 'jira', 'both')]
    [string]$Tracker = 'both',

    [int]$Port = 0,

    [int]$Repeat = 1,

    [switch]$KeepServer,

    [switch]$NoReset
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $repoRoot
try {
    Write-Host ""
    Write-Host "Haoee QA 全流程 · 一条命令跑完" -ForegroundColor Cyan
    Write-Host "  仓库：$repoRoot"
    Write-Host "  缺陷联动：$Filing / $Tracker"
    if ($Repeat -gt 1) { Write-Host "  一致性：连跑 $Repeat 次" }
    Write-Host ""

    $args = @('-m', 'atf', 'e2e', '--filing', $Filing, '--tracker', $Tracker)
    if ($Port -gt 0) { $args += @('--port', $Port) }
    if ($Repeat -gt 1) { $args += @('--repeat', $Repeat) }
    if ($KeepServer) { $args += '--keep-server' }
    if ($NoReset) { $args += '--no-reset' }

    python @args
    $code = $LASTEXITCODE

    Write-Host ""
    switch ($code) {
        0 { Write-Host "结论：通过（退出码 0）" -ForegroundColor Green }
        1 { Write-Host "结论：未通过（退出码 1）—— 看上面的失败明细与 RUN.md" -ForegroundColor Yellow }
        2 { Write-Host "结论：配置错误（退出码 2）—— 检查 atf/config/e2e.yaml" -ForegroundColor Red }
        default { Write-Host "结论：执行异常（退出码 $code）" -ForegroundColor Red }
    }
    Write-Host "产物目录：atf/e2e-runs\<run_id>\（RUN.md / results.json / junit-*.xml / console-*.log）"
    exit $code
}
finally {
    Pop-Location
}