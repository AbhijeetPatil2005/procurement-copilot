"""Capture README screenshots of the running UI (requires `pip install playwright && playwright install chromium`).

    python run_local.py            # in another terminal
    python scripts/capture_screenshots.py --architecture "A · Single agent"
"""
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def pick_request(page, request_id: str) -> None:
    page.locator("[data-testid='stSidebar'] [data-baseweb='select']").first.click()
    page.keyboard.type(request_id)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)


def analyze(page, architecture: str) -> None:
    page.locator("[data-testid='stSidebar']").get_by_text(architecture, exact=True).click()
    page.get_by_role("button", name="Analyze request").click()
    page.wait_for_selector(".pc-banner", timeout=180_000)
    page.wait_for_timeout(1200)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8501")
    ap.add_argument("--architecture", default="A · Single agent")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Streamlit scrolls an inner container, so full_page alone misses content - use a tall viewport.
        page = browser.new_page(viewport={"width": 1500, "height": 2300}, device_scale_factor=1)
        page.goto(args.url)
        page.wait_for_selector("text=AI Procurement Request Copilot", timeout=60_000)

        shots = [("REQ-1002", "copilot.png"), ("REQ-1006", "injection.png"), ("REQ-1007", "conflict.png")]
        for request_id, name in shots:
            pick_request(page, request_id)
            analyze(page, args.architecture)
            page.screenshot(path=str(OUT / name), full_page=True)
            print("saved", name)

        page.get_by_role("tab", name="📊 Evaluation").click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "evaluation.png"), full_page=True)
        print("saved evaluation.png")
        browser.close()


if __name__ == "__main__":
    main()
