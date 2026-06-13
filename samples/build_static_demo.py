#!/usr/bin/env python3
"""生成单文件离线演示页 docs/demo.html：内置两个月演示数据，双击即可在浏览器打开。

无需 Python/服务/命令行；所有操作仅在页面内模拟，用于演示界面与交互。
真实对账仍走 CLI/Web 引擎。运行：python3 samples/build_static_demo.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fince_recon.config import Config  # noqa: E402
from fince_recon.web import App  # noqa: E402


def collect() -> dict:
    import os
    import tempfile

    db = tempfile.mktemp(suffix=".db")
    cwd = os.getcwd()
    os.chdir(ROOT)  # seed_demo 需要 samples/ 在 cwd
    try:
        app = App(db, Config())
        app.do("seed_demo", {})
        data = {"periods": ["2026-05", "2026-06"], "by_period": {}}
        for p in data["periods"]:
            data["by_period"][p] = {
                "summary": app.summary(p),
                "matches": app.matches(p, None),
                "tickets": app.tickets(p, None),
                "board": app.board(p),
            }
        data["audit"] = app.audit_log(60)
        return data
    finally:
        os.chdir(cwd)
        if os.path.exists(db):
            os.remove(db)


TEMPLATE = (ROOT / "samples" / "static_demo_template.html").read_text(encoding="utf-8")


def main():
    data = collect()
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("/*__DATA__*/", payload)
    out = ROOT / "docs" / "demo.html"
    out.write_text(html, encoding="utf-8")
    print(f"已生成 {out}（{out.stat().st_size} bytes）")


if __name__ == "__main__":
    main()
