# 真实项目一键就绪（幂等，可重复执行）
#
# 做四件事：装依赖 -> 建库迁移 -> 灌演示数据 -> 建本地测试账号。
# 不启动服务：服务由使用者显式启动，避免"脚本跑完了但不知道服务还在不在"。

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$labDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$appDir = Join-Path $labDir "app"

Write-Host "[1/4] 安装依赖" -ForegroundColor Cyan
python -m pip install -q --disable-pip-version-check -r (Join-Path $labDir "requirements.txt")

Push-Location $appDir
try {
    Write-Host "[2/4] 数据库迁移" -ForegroundColor Cyan
    python manage.py migrate --noinput

    Write-Host "[3/4] 灌入演示内容（幂等）" -ForegroundColor Cyan
    python manage.py seed_demo

    Write-Host "[4/4] 创建/更新本地测试账号" -ForegroundColor Cyan
    python manage.py ensure_qa_user
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "就绪。启动被测站：" -ForegroundColor Green
Write-Host "  cd labs/07-real-project/app"
Write-Host "  python manage.py runserver 127.0.0.1:8099 --noreload"
Write-Host ""
Write-Host "跑测试（另开终端）："
Write-Host "  cd labs/07-real-project; python -m pytest -q"
Write-Host "  cd <repo>; python -m atf run --tag 'not canary'"

