"""Local-only browser geometry checks; no application API or real trades."""
from pathlib import Path

import pytest


PAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "nav_dashboard_page.html"


@pytest.mark.parametrize("width,height", [(320, 640), (430, 800), (1024, 768)])
def test_scroll_viewport_never_overlaps_either_bar(width, height):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        # Use the installed full Chromium, not a separately downloaded shell.
        browser = runtime.chromium.launch(channel="chromium", headless=True)
        try:
            page = browser.new_page(viewport={"width": width, "height": height}, java_script_enabled=False)
            page.route("**/*", lambda route: route.abort())
            page.set_content(PAGE_PATH.read_text(encoding="utf-8"))
            page.locator("#accessError").evaluate("el => el.hidden = true")
            page.locator("#dashboardContent").evaluate("el => el.hidden = false")
            for panel_id in ("overviewPanel", "positionsPanel", "transactionsPanel", "sipPanel"):
                page.locator(".management-panel").evaluate_all(
                    "(panels, active) => panels.forEach(el => el.hidden = el.id !== active)", panel_id
                )
                panel = page.locator("#" + panel_id)
                panel.evaluate("el => { const spacer = document.createElement('div'); spacer.style.height = '2500px'; el.appendChild(spacer); }")
                topbar = page.locator(".topbar").bounding_box()
                tabs = page.locator(".management-tabs").bounding_box()
                bounds = panel.bounding_box()
                assert bounds["y"] >= topbar["y"] + topbar["height"] - 0.5, panel_id
                assert bounds["y"] + bounds["height"] <= tabs["y"] + 0.5, panel_id
                assert tabs["y"] + tabs["height"] <= height + 0.5
                assert panel.evaluate("el => el.scrollHeight > el.clientHeight")
                panel.evaluate("el => el.scrollTop = el.scrollHeight")
                assert panel.evaluate("el => el.scrollTop") > 0
                assert page.locator(".topbar").bounding_box() == topbar
                assert page.locator(".management-tabs").bounding_box() == tabs
        finally:
            browser.close()
