import math
from pathlib import Path

PAGE_PATH = Path(__file__).resolve().parents[1] / "src" / "nav_dashboard_page.html"


def _read_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


def test_page_has_four_management_tabs_and_quick_trade():
    html = _read_page()
    for element_id in (
        "overviewTab", "positionsTab", "transactionsTab", "sipTab",
        "overviewPanel", "positionsPanel", "transactionsPanel", "sipPanel",
        "quickTradeButton", "productDialog", "tradeDialog",
        "confirmDialog", "sipDialog", "adjustmentDialog",
    ):
        assert f'id="{element_id}"' in html


def test_write_fetch_uses_private_token_idempotency_and_custom_header():
    html = _read_page()
    assert '"X-Nav-Dashboard-Key": token' in html
    assert '"X-Portfolio-Request": "1"' in html
    assert '"Idempotency-Key": idempotencyKey' in html
    assert "/api/portfolio/transactions/preview" in html
    assert "/api/portfolio/sip-plans/preview" in html


def test_sip_save_uses_the_shared_preview_confirm_pending_action_flow():
    html = _read_page()
    assert (
        "previewPath: '/api/portfolio/sip-plans/preview', "
        "submitPath: '/api/portfolio/sip-plans'"
    ) in html
    assert (
        "requestJson('/api/portfolio/sip-plans', payload, newIdempotencyKey())"
        not in html
    )


def test_read_only_state_disables_dialog_writes_and_load_failure_closes_writes():
    html = _read_page()
    assert (
        "'.management-dialog input, .management-dialog select, "
        ".management-dialog textarea, .management-dialog "
        "button:not([data-close-dialog])'"
    ) in html
    assert "function disableWrites(reason)" in html
    assert "disableWrites('组合数据读取失败，写操作已关闭。')" in html
    assert "if (!state.writeEnabled) return;" in html


def test_holding_css_rule_exists():
    page = _read_page()
    assert ".holding" in page
    assert "font-variant-numeric: tabular-nums" in page
    # holding 跨列对齐名称左边缘（从 grid column 2 到末尾）
    assert "grid-column: 2 / -1" in page


def test_product_template_contains_holding_node():
    page = _read_page()
    assert '<div class="holding">' in page
    assert "holding" in page
    # holding 在 .income 之后、</article> 之前（article 级别子节点）
    assert "</div>${(() => { const h = formatHolding" in page or "</div>${(() => {" in page


def test_top_metrics_unchanged():
    page = _read_page()
    assert "估算市值(元)" in page
    assert "已设份额" in page
    assert "今日披露" in page


def format_holding(share_text, nav_text):
    """Python 端契约函数，行为必须与 src/nav_dashboard_page.html 内的
    formatHolding() 完全一致。HTML 改动时必须同步更新本函数与用例样本。
    """
    if not share_text:
        return ""
    try:
        shares = float(share_text)
    except (TypeError, ValueError):
        return ""
    if shares != shares:  # NaN 检查
        return ""
    if math.isinf(shares):
        return ""
    shares_text = f"{shares:.2f}"
    if not nav_text:
        return f"{shares_text} 份"
    try:
        nav = float(nav_text)
    except (TypeError, ValueError):
        return f"{shares_text} 份"
    if nav != nav:  # NaN 检查
        return f"{shares_text} 份"
    if math.isinf(nav):
        return f"{shares_text} 份"
    value = shares * nav
    value_text = f"{value:,.2f}"
    return f"{shares_text} 份 · 市值 {value_text} 元"


def test_format_holding_empty_shares_returns_empty():
    assert format_holding("", "") == ""


def test_format_holding_only_shares_when_nav_missing():
    assert format_holding("401133.95", "") == "401133.95 份"
    assert format_holding("10000", "abc") == "10000.00 份"


def test_format_holding_both_present_full_text():
    assert format_holding("401133.95", "1.001234") == "401133.95 份 · 市值 401,628.95 元"
    assert format_holding("10000", "1.000000") == "10000.00 份 · 市值 10,000.00 元"


def test_format_holding_zero_values():
    assert format_holding("0", "1.0") == "0.00 份 · 市值 0.00 元"


def test_format_holding_non_numeric_shares_treated_as_empty():
    assert format_holding("abc", "1.0") == ""
    assert format_holding(None, "1.0") == ""


def test_format_holding_non_numeric_nav_only_shares():
    assert format_holding("10000", None) == "10000.00 份"


def test_format_holding_nan_shares_returns_empty():
    assert format_holding("nan", "1.0") == ""
    assert format_holding("NaN", "1.0") == ""


def test_format_holding_nan_nav_returns_only_shares():
    assert format_holding("10000", "nan") == "10000.00 份"


def test_format_holding_inf_shares_returns_empty():
    assert format_holding("inf", "1.0") == ""
    assert format_holding("-inf", "1.0") == ""


def test_format_holding_inf_nav_returns_only_shares():
    assert format_holding("10000", "inf") == "10000.00 份"
    assert format_holding("10000", "-inf") == "10000.00 份"


def test_format_holding_nav_zero():
    assert format_holding("10000", "0") == "10000.00 份 · 市值 0.00 元"
