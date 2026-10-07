"""Capture README screenshots of the running web app (requires `pip install playwright && playwright install chromium`).

    python run_local.py            # in another terminal
    python scripts/capture_screenshots.py --architecture single
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
SEG_LABEL = {"single": "A · Single agent", "staged": "B · Staged", "rules": "Rules only"}


def analyze(page, request_id: str, architecture: str) -> None:
    page.locator(f".req[data-id='{request_id}']").click()
    page.locator("#seg button", has_text=SEG_LABEL[architecture]).click()
    page.locator("#run").click()
    page.wait_for_selector(".decision", timeout=240_000)
    page.wait_for_timeout(800)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8501")
    ap.add_argument("--architecture", default="single", choices=list(SEG_LABEL))
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1400})
        page.goto(args.url)
        page.wait_for_selector(".req", timeout=60_000)

        for request_id, name in [("REQ-1002", "copilot.png"), ("REQ-1006", "injection.png"), ("REQ-1007", "conflict.png")]:
            analyze(page, request_id, args.architecture)
            page.screenshot(path=str(OUT / name))
            print("saved", name)

        page.locator(".side-tab[data-tab='trace']").click()
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "trace.png"))
        print("saved trace.png")

        page.locator(".nav-item[data-view='eval']").click()
        page.wait_for_selector(".tbl", timeout=30_000)
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "evaluation.png"))
        print("saved evaluation.png")
        browser.close()


if __name__ == "__main__":
    main()
