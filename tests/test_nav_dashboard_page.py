from pathlib import Path

PAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "nav_dashboard_page.html"


def _read_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def test_holding_css_rule_exists():
    page = _read_page()
    assert ".holding" in page
    assert "font-variant-numeric: tabular-nums" in page


def test_product_template_contains_holding_node():
    page = _read_page()
    assert '<div class="holding">' in page
    assert "holding" in page


def test_top_metrics_unchanged():
    page = _read_page()
    assert "估算市值(元)" in page
    assert "已设份额" in page
    assert "今日披露" in page
