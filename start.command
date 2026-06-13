#!/usr/bin/env bash
# 资金对账工作台 · 一键启动（macOS / Linux 双击运行）
# macOS：双击本文件即可（若提示无法打开，右键 → 打开 一次即可）
cd "$(dirname "$0")"
clear
echo "============================================"
echo "   资金对账工作台  启动中…"
echo "============================================"

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo
  echo "✗ 没检测到 Python。请先安装 Python 3.11+："
  echo "  - macOS：终端运行  xcode-select --install   或到 https://www.python.org/downloads/ 下载"
  echo "  - Linux：用系统包管理器安装 python3"
  echo
  read -p "按回车键退出…" _
  exit 1
fi
echo "使用 Python：$PY"

# 读 Excel 需要 openpyxl（装不上也不影响 CSV 与演示数据）
"$PY" -m pip install --quiet openpyxl >/dev/null 2>&1 || true

export PYTHONPATH=src
PORT=8000
URL="http://127.0.0.1:$PORT"

# 稍等服务起来后自动打开浏览器
( sleep 2
  if command -v open >/dev/null 2>&1; then open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"
  fi
) &

echo
echo "浏览器即将自动打开：$URL"
echo "（没自动打开就手动把上面网址复制到浏览器）"
echo "首页点右上「⚡ 加载演示数据」即可开始演示。"
echo "关闭本窗口即停止服务。"
echo
"$PY" -m fince_recon --db demo.db serve --port "$PORT"
