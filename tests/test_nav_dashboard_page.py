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


def test_preview_and_submit_share_one_idempotency_key_from_the_first_request():
    html = _read_page()
    assert "const idempotencyKey = newIdempotencyKey();" in html
    assert "requestJson(previewPath, payload, idempotencyKey)" in html
    assert (
        "state.pendingAction = {submitPath, payload: submitPayload, "
        "idempotencyKey, originDialog"
    ) in html


def test_reverse_collects_a_nonempty_reason_before_preview():
    html = _read_page()
    assert 'id="reverseReasonField"' in html
    assert 'id="reverseReason"' in html
    assert "function openReverse(transactionId)" in html
    assert "if (!reason)" in html
    assert "payload: {operation: 'reverse', transaction_id: action.transactionId, reason}" in html
    assert "if (operation === 'reverse') openReverse(control.dataset.transactionId);" in html


def test_sip_save_is_draft_and_activation_is_an_explicit_operation():
    html = _read_page()
    # 服务器契约：激活 SIP 显式携带 activate: true，暂停/恢复/激活均为独立写操作。
    assert "activate: true" in html
    assert "data-write-operation=\"sip-activate\"" in html
    assert "if (existingStatus !== 'draft')" not in html
    assert "sip_id: form.dataset.sipId || undefined" in html
    assert 'name="start_date"' in html
    assert "form.start_date.value = rowValue(existing, 'start_date')" in html


def test_product_create_preserves_preview_identity_for_submit():
    html = _read_page()
    # 服务器契约：previewAction 统一从 previewData.product 取产品身份。
    assert "const previewIdentity = previewData.product || null" in html
    assert "submitPayload = {...payload, ...previewIdentity" in html
    assert "capturePreviewIdentity: true" in html
    assert "registration_code" in html


def test_disable_payload_and_transaction_note_are_preserved():
    html = _read_page()
    assert "payload: {operation: 'disable', product_id: control.dataset.productId}" in html
    assert '<textarea name="note" maxlength="300"></textarea>' in html
    trade_submit = html[html.index("$('tradeForm').addEventListener"):html.index(
        "$('adjustmentForm').addEventListener"
    )]
    assert "payload: formPayload(event.currentTarget)" in trade_submit


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


def test_overview_count_uses_merged_root_product_list_without_double_counting():
    page = _read_page()
    assert "const totalConfigured = (data.products || []).length;" in page
    assert "(data.configured_count || 0) + pfProducts.length" not in page


def test_trade_form_switches_between_purchase_amount_and_redemption_shares():
    page = _read_page()
    assert 'id="tradeKind"' in page
    assert 'id="tradeValueLabel"' in page
    assert 'id="tradeValueInput"' in page
    assert 'name="trade_time"' in page
    assert "input.name = redemption ? 'shares' : 'amount';" in page
    assert "label.firstChild.textContent = redemption ? '赎回份额' : '申购金额（元）';" in page
    assert "$('tradeTimeLabel').firstChild.textContent = redemption ? '赎回时间' : '申购时间';" in page


def test_transactions_and_preview_use_chinese_business_labels():
    page = _read_page()
    assert "function transactionTypeLabel(value)" in page
    assert "manual_redemption: '手工赎回'" in page
    assert "opening_position: '初始持仓'" in page
    assert "function previewFieldLabel(path)" in page
    assert "'normalized_input.amount': '申购金额（元）'" in page
    assert "'status_prediction': '预计状态'" in page
    assert "The trade will remain pending until a quote arrives." in page
    assert "等待交易日净值后自动确认" in page


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
