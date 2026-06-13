@echo off
chcp 65001 >nul
title 资金对账工作台
cd /d "%~dp0"
echo ============================================
echo    资金对账工作台  启动中...
echo ============================================

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo [x] 没检测到 Python。请到 https://www.python.org/downloads/ 安装 Python 3.11+，
  echo     安装时务必勾选 "Add Python to PATH"，装完关掉本窗口重新双击。
  echo.
  pause
  exit /b 1
)

rem 读 Excel 需要 openpyxl（装不上也不影响 CSV 与演示数据）
%PY% -m pip install --quiet openpyxl >nul 2>nul

set PYTHONPATH=src
start "" http://127.0.0.1:8000

echo.
echo 浏览器即将自动打开：http://127.0.0.1:8000
echo （没自动打开就手动把上面网址复制到浏览器）
echo 首页点右上「加载演示数据」即可开始演示。
echo 关闭本窗口即停止服务。
echo.
%PY% -m fince_recon --db demo.db serve --port 8000
pause
