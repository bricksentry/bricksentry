"""Render `bricksentry demo` output as docs/images/demo.png (terminal-window style).

    python scripts/render_demo_image.py            # needs: playwright + chromium

Runs the demo with colors on, converts the ANSI output to HTML and screenshots it.
"""

from __future__ import annotations

import html
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "images" / "demo.png"
COMMAND = 'uvx --from "bricksentry[duckdb]" bricksentry demo'

BASIC = {30: "#1e293b", 31: "#e5484d", 32: "#22c55e", 33: "#e5a93b", 34: "#60a5fa", 35: "#c084fc",
         36: "#22c4c4", 37: "#e2e8f0", 90: "#7d8794", 91: "#f87171", 92: "#4ade80", 93: "#facc15",
         94: "#93c5fd", 95: "#e879f9", 96: "#67e8f9", 97: "#ffffff"}


def capture() -> str:
    import click

    from bricksentry.demo import run_demo

    buf = io.StringIO()
    orig = click.echo

    def echo(message=None, **kw):  # keep ANSI codes when writing to a StringIO
        kw["color"] = True
        return orig(message, **kw)

    click.echo = echo
    try:
        with redirect_stdout(buf):
            run_demo()
    finally:
        click.echo = orig
    return buf.getvalue()


def ansi_to_html(text: str) -> str:
    out: list[str] = []
    state: dict[str, str] = {}
    pos = 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", text):
        out.append(_span(text[pos:m.start()], state))
        codes = [int(c) for c in m.group(1).split(";") if c] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                state = {}
            elif c == 1:
                state["font-weight"] = "700"
            elif c == 4:
                state["text-decoration"] = "underline"
            elif c in BASIC:
                state["color"] = BASIC[c]
            elif c in (38, 48) and codes[i + 1:i + 2] == [2]:
                r, g, b = codes[i + 2:i + 5]
                key = "color" if c == 38 else "background"
                state[key] = f"rgb({r},{g},{b})"
                if c == 48:
                    state["padding"] = "0 2px"
                i += 4
            i += 1
        pos = m.end()
    out.append(_span(text[pos:], state))
    return "".join(out)


def _span(s: str, state: dict[str, str]) -> str:
    if not s:
        return ""
    s = html.escape(s)
    if not state:
        return s
    return f'<span style="{";".join(f"{k}:{v}" for k, v in state.items())}">{s}</span>'


PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{{margin:0;background:transparent}}
.win{{width:1180px;background:#0f172a;border-radius:12px;overflow:hidden;font:15px/1.6 'DejaVu Sans Mono',monospace;color:#e2e8f0}}
.bar{{background:#1a2740;padding:10px 16px;display:flex;gap:8px;align-items:center;color:#94a3b8;font-size:13px}}
.dot{{width:12px;height:12px;border-radius:50%;display:inline-block}}
pre{{margin:0;padding:18px 24px 26px;white-space:pre-wrap;font:inherit}}
.cmd{{color:#a5b4fc}}
</style></head><body><div class="win"><div class="bar"><span class="dot" style="background:#ff5f57"></span>
<span class="dot" style="background:#e5a93b"></span><span class="dot" style="background:#28c840"></span>
<span style="margin-left:12px">bricksentry demo</span></div><pre><span class="cmd">$ {cmd}</span>

{body}</pre></div></body></html>"""


def main() -> None:
    from playwright.sync_api import sync_playwright

    body = ansi_to_html(capture().rstrip("\n"))
    page_html = PAGE.format(cmd=html.escape(COMMAND), body=body)
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(device_scale_factor=2, viewport={"width": 1180, "height": 800})
        page.set_content(page_html)
        page.locator(".win").screenshot(path=str(OUT), omit_background=True)
        b.close()
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
