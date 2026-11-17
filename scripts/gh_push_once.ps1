# 一次性推送：用 PAT 把本仓库推到 GitHub（不需要装 SSH key）
#
# 为什么给你这个脚本：
#   - 本机没有 GitHub 凭据，你的 SSH 公钥两次都没生效（GitHub 侧提示 publickey denied）；
#   - 这个脚本把 token 读成"不回显"的输入，只在内存里活一次，不落盘、不进 git config、不进命令行历史。
#
# 用法（在任意终端执行一次即可）：
#   powershell -ExecutionPolicy Bypass -File scripts\gh_push_once.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\gh_push_once.ps1 -DryRun    # 只看要做什么
#
# PAT 所需权限（fine-grained，仅限该仓库）：
#   Contents: Read and write    —— 推送代码
#   Workflows: Read and write   —— 仓库里有 .github/workflows/，缺这条会被拒
#
# 推送成功后，CI 会自动在 https://github.com/Shiacc/testing-craft/actions 上跑起来。

[CmdletBinding()]
param(
    [string]$Repo = 'Shiacc/testing-craft',
    [string]$Branch = 'main',
    [string]$LocalBranch = 'master',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $root
try {
    Write-Host "仓库根目录：$root"
    Write-Host "推送映射：$LocalBranch -> $Branch（远端 $Repo）"

    if ($DryRun) {
        Write-Host "[dry-run] 将执行：git push https://github.com/$Repo.git ${LocalBranch}:${Branch}"
        Write-Host "[dry-run] 鉴权：http.extraheader（token 只存在于本次内存，不写入 remote / config）"
        return
    }

    $secure = Read-Host -AsSecureString '把 GitHub PAT 粘进来（输入不回显，回车确认）'
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }

    if ([string]::IsNullOrWhiteSpace($token)) {
        throw 'token 为空，已中止'
    }
    $token = $token.Trim()

    $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("x-access-token:$token"))
    Write-Host '推送中…'
    git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $auth" push "https://github.com/$Repo.git" "${LocalBranch}:${Branch}"

    if ($LASTEXITCODE -ne 0) {
        throw '推送失败。检查：① token 是否对 Shiacc/testing-craft 有 Contents+Workflows 读写；② 是否已过期；③ 仓库是否为空仓（应为空）'
    }

    Write-Host ''
    Write-Host '推送成功 ✅' -ForegroundColor Green
    Write-Host "  仓库：https://github.com/$Repo"
    Write-Host "  Actions：https://github.com/$Repo/actions"
    Write-Host ''
    Write-Host 'CI 会在 10-30 秒内出现运行记录；第一个 run 会跑 static → e2e（两个项目）→ 主干 repeat 3。'
}
finally {
    $token = $null
    $auth = $null
    Pop-Location
}