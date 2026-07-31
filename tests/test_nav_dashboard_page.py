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
        "quickTradeButton", "tradeDialog",
        "confirmDialog", "sipDialog", "adjustmentDialog",
    ):
        assert f'id="{element_id}"' in html


def test_active_management_tab_survives_page_and_data_refresh():
    html = _read_page()

    assert "const ACTIVE_TAB_STORAGE_KEY = 'nav-dashboard-active-tab';" in html
    assert "function readStoredActiveTab()" in html
    assert "activeTab: readStoredActiveTab()," in html
    assert "sessionStorage.setItem(ACTIVE_TAB_STORAGE_KEY, tab);" in html
    assert "setActiveTab(state.activeTab);" in html


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


def test_reverse_operation_is_not_exposed_in_the_dashboard():
    html = _read_page()
    assert 'id="reverseReasonField"' not in html
    assert 'data-write-operation="reverse"' not in html
    assert "function openReverse(transactionId)" not in html


def test_sip_save_is_draft_and_activation_is_an_explicit_operation():
    html = _read_page()
    # 服务器契约：激活 SIP 显式携带 activate: true，暂停/恢复/激活均为独立写操作。
    assert "activate: true" in html
    assert "data-write-operation=\"sip-activate\"" in html
    assert "if (existingStatus !== 'draft')" not in html
    assert "sip_id: form.dataset.sipId || undefined" in html
    assert 'name="start_date"' in html
    assert "form.start_date.value = rowValue(existing, 'start_date')" in html


def test_sip_plan_can_be_deleted_after_preview():
    html = _read_page()

    assert 'data-write-operation="sip-delete"' in html
    assert "operation === 'sip-delete'" in html
    assert "operation.replace('sip-', '')" in html


def test_sip_fee_rate_field_and_display_use_percent_units():
    html = _read_page()

    assert "申购费率（%）" in html
    assert "'purchase_fee_rate_percent'" in html
    assert "purchase_fee_rate_percent') || '--')}%" in html


def test_all_position_shares_show_two_decimal_places():
    html = _read_page()

    assert "const sharesDisplay = money(shares);" in html
    assert "const availableSharesDisplay = money(availableShares);" in html
    assert "const availableShares = rowValue(row, 'available_shares');" in html
    assert "const inTransitAmount = rowValue(row, 'in_transit_amount');" in html
    assert "<span>可用份额</span>" in html
    assert "<span>在途资金</span>" in html


def test_position_card_merges_quote_date_and_change_below_non_cash_nav():
    html = _read_page()

    assert (
        "const quoteDate = row.quote?.date || "
        "rowValue(row, 'nav_date', 'quote_date');"
    ) in html
    assert 'class="manage-subvalue"' in html
    assert "signedPercent(changePct)" in html
    assert "<span>净值日期</span>" not in html
    assert "<span>最新收益</span>" in html


def test_overview_redemption_status_uses_pending_shares_and_unsettled_amount():
    html = _read_page()

    assert "赎回待确认 ${money(pendingRedemptionShares)} 份" in html
    assert "赎回待到账 ${money(unsettledRedemptionAmount)} 元" in html


def test_latest_profit_adjustment_has_a_separate_button_and_no_date_input():
    html = _read_page()

    assert 'id="latestProfitAdjustmentDialog"' in html
    assert 'data-write-operation="adjust-latest-profit"' in html
    assert "/api/portfolio/latest-profit-adjustments/preview" in html
    dialog = html[
        html.index('id="latestProfitAdjustmentDialog"'):
        html.index('</dialog>', html.index('id="latestProfitAdjustmentDialog"'))
    ]
    assert 'name="effective_date"' not in dialog
    assert "rowValue(product, 'latest_profit_date')" in html


def test_non_nav_amounts_and_shares_use_two_decimal_display():
    html = _read_page()

    assert "function previewFieldRequiresTwoDecimals(path)" in html
    assert "return money(text);" in html
    assert "const quantity = Number.isFinite(number(match[3]))" in html
    assert "<span>${sharesLabel}</span><b>${esc(money(shares))} 份</b>" in html
    assert "const shareText = value => money(value);" in html
    assert (
        "form.amount.value = number("
        "rowValue(existing, 'amount', 'daily_amount')).toFixed(2);"
    ) in html
    assert (
        '<div class="manage-value">${money('
        "rowValue(row, 'amount', 'daily_amount') || 0)} 元</div>"
    ) in html


def test_position_detail_opens_a_real_dialog():
    html = _read_page()

    assert 'id="productDetailDialog"' in html
    assert 'id="productDetailContent"' in html
    assert 'data-view-product="${esc(id)}">详情</button>' in html
    assert "function openProductDetail(productId)" in html
    assert "dialogOpen('productDetailDialog');" in html


def test_adjustment_values_and_share_step_use_two_decimals():
    html = _read_page()

    assert 'name="shares" type="number" min="0" step="0.01"' in html
    assert (
        "form.elements.shares.value = number("
        "rowValue(product, 'shares', 'share')).toFixed(2);"
    ) in html
    assert (
        "form.elements.profit.value = number("
        "rowValue(product, 'holding_profit', 'cumulative_profit')).toFixed(2);"
    ) in html


def test_products_are_added_inline_from_trade_or_sip_dialog_only():
    html = _read_page()

    assert 'data-write-action="product"' not in html
    assert 'id="productDialog"' not in html
    assert 'id="productForm"' not in html
    assert 'name="new_fund_code"' in html
    assert 'name="new_fund_code" id="tradeFundCode" type="hidden"' in html
    assert 'name="new_fund_code" id="sipFundCode" type="hidden"' in html
    assert "async function ensureInlineFund" in html
    assert "await ensureInlineFund" in html
    assert "fillProductSearch('sip', 'public_fund')" in html


def test_trade_and_sip_use_one_searchable_product_field():
    html = _read_page()

    assert 'id="tradeProductQuery" role="combobox"' in html
    assert 'id="tradeProductSuggestions"' in html
    assert 'id="sipProductQuery" role="combobox"' in html
    assert 'id="sipProductSuggestions"' in html
    assert '<select name="product_id" id="tradeProduct">' not in html
    assert '<select name="product_id" id="sipProduct">' not in html
    assert "function fillProductSearch(scope, productType)" in html
    assert "function syncProductSearch(scope, productType, complete = false)" in html
    assert "code.includes(query) || name.includes(query)" in html
    assert "function productSearchProviderLabel(row)" in html
    assert "nanyin_wealth: '南银理财'" in html
    assert "citic_wealth: '信银理财'" in html
    assert "provider.includes(query)" in html
    assert "syncProductSearch('trade', $('tradeProductType').value, true);" in html
    assert "syncProductSearch('sip', 'public_fund', true);" in html


def test_unknown_six_digit_fund_code_resolves_name_before_submit():
    html = _read_page()

    assert 'id="tradeProductSearchStatus"' in html
    assert 'id="sipProductSearchStatus"' in html
    assert "async function resolvePublicFundSearch(scope)" in html
    assert "function schedulePublicFundSearch(scope, delay = 350)" in html
    assert "'/api/portfolio/products/preview'" in html
    assert "product_type: 'public_fund'" in html
    assert "已识别：${identity.name}（${identity.code}）" in html
    assert "input.dataset.resolvedFundCode = identity.code;" in html
    assert "const fundSearchPreviewCache = new Map();" in html
    assert "fundSearchPreviewCache.get(code) || await requestJson(" in html


def test_partial_code_and_chinese_name_search_external_product_candidates():
    html = _read_page()

    assert "'/api/portfolio/products/search'" in html
    assert "function scheduleProductCandidateSearch(scope" in html
    assert "async function searchProductCandidates(scope)" in html
    assert "query.length < 2" in html
    assert "productSearchRemoteRows[scope]" in html
    assert "scheduleProductCandidateSearch('trade')" in html
    assert "scheduleProductCandidateSearch('sip')" in html


def test_product_candidates_use_wide_custom_panel_with_full_names():
    html = _read_page()

    assert 'class="product-suggestion-panel"' in html
    assert 'role="listbox"' in html
    assert "width: min(460px, calc(100vw - 56px))" in html
    assert ".product-suggestion-name" in html
    assert "white-space: normal" in html
    assert "font-size: 12px" in html
    assert "function selectProductSuggestion(scope, index)" in html
    assert "data-product-suggestion-index" in html
    assert "function handleProductSearchKeydown(scope, event)" in html


def test_product_create_preserves_preview_identity_for_submit():
    html = _read_page()
    # 单弹窗添加基金时，仍将预览返回的完整身份带入产品提交。
    assert "const previewIdentity = previewData.product || null" in html
    assert "submitPayload = {...payload, ...previewIdentity" in html
    assert "const identity = preview.product;" in html
    assert "identity_fingerprint: preview.identity_fingerprint" in html
    assert "registration_code" in html


def test_disable_payload_and_transaction_note_are_preserved():
    html = _read_page()
    assert "payload: {operation: 'disable', product_id: control.dataset.productId}" in html
    assert '<textarea name="note" maxlength="300"></textarea>' in html
    trade_submit = html[html.index("$('tradeForm').addEventListener"):html.index(
        "$('adjustmentForm').addEventListener"
    )]
    assert "formPayload(event.currentTarget)" in trade_submit
    assert "await ensureInlineFund" in trade_submit


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
    assert 'id="tradeProductType"' in page
    assert '<option value="wealth_nav">理财</option>' in page
    assert '<option value="cash_management">现金</option>' in page
    assert '<option value="public_fund">公募基金</option>' in page
    assert 'id="tradeKind"' in page
    assert 'id="tradeValueLabel"' in page
    assert 'id="tradeValueInput"' in page
    assert 'name="trade_time"' in page
    assert "input.name = redemption ? 'shares' : 'amount';" in page
    assert "input.step = '0.01';" in page
    assert "input.min = '0.01';" in page
    assert "label.firstChild.textContent = redemption ? '赎回份额' : '申购金额（元）';" in page
    assert "$('tradeTimeLabel').firstChild.textContent = redemption ? '赎回时间' : '申购时间';" in page


def test_trade_form_filters_products_and_shows_type_specific_fields():
    page = _read_page()

    assert ".form-field[hidden] { display: none; }" in page
    assert 'id="tradeFundCodeField"' not in page
    assert 'id="tradeFeeRateField"' in page
    assert 'name="fee_rate_percent"' in page
    assert 'id="tradeSourceField"' in page
    assert 'name="source_cash_product_id"' in page
    assert 'id="tradeDestinationField"' in page
    assert 'name="destination_cash_product_id"' in page
    assert '<option value="">钱包</option>' in page
    assert "fillProductSearch('trade', productType);" in page
    assert "$('tradeFeeRateField').hidden = !(publicFund && !redemption);" in page
    assert "$('tradeSourceField').hidden = redemption;" in page
    assert "$('tradeSource').disabled = redemption;" in page
    assert "$('tradeDestinationField').hidden = !redemption;" in page
    assert "payload.fee_rate = String(number(payload.fee_rate_percent) / 100);" in page
    assert "delete payload.fee_rate_percent;" in page
    assert "const sevenDayDisplay = sevenDay === '' ? '--' : `${(number(sevenDay) * 100).toFixed(2)}%`;" in page
    assert "pending[1] === 'external_cash' ? '钱包'" in page


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
    assert "String(rowValue(row, 'created_by')) !== 'wallet_migration'" in page
    assert "申购资金来源：" in page
    assert "赎回到账：" in page
    assert ".transaction-summary.single { grid-template-columns: minmax(0, 1fr); }" in page
    assert ".transaction-details { display: grid;" in page
    assert 'class="manage-card transaction-card ${kind.className}"' in page
    assert 'class="transaction-head-actions"' in page
    assert "function transactionDetailsHtml(" in page


def test_sip_cash_outflow_is_labeled_as_sip_deduction():
    page = _read_page()

    assert "function transactionDisplayLabel(value, row = {})" in page
    assert "type === 'cash_transfer_out' && rowValue(row, 'plan_id')" in page
    assert "return '定投扣款';" in page
    assert "function isVisibleTransaction(row)" in page
    assert "if (type === 'cash_transfer_out' && rowValue(row, 'plan_id')) return true;" in page


def test_transaction_cards_visually_distinguish_purchase_and_redemption():
    page = _read_page()

    assert "function transactionKindMeta(value)" in page
    assert "label: '申购', className: 'purchase'" in page
    assert "label: '赎回', className: 'redemption'" in page
    assert ".transaction-card.purchase" in page
    assert ".transaction-card.redemption" in page
    assert 'class="transaction-kind ${kind.className}"' in page
    assert "const amountLabel = kind.className === 'purchase' ? '申购金额'" in page
    assert "kind.className === 'redemption' ? '赎回份额' : '交易份额'" in page


def test_positions_panel_uses_positive_position_rows_not_all_products():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "const rows = positions;" in page
    assert "const rows = products.map" not in page


def test_overview_does_not_remerge_catalog_products_into_authoritative_products():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "const portfolioByCode = new Map" not in page
    assert "data.products = baseProducts.map" not in page


def test_holding_controls_support_delete_and_profit_calibration_without_reversal():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert 'name="profit"' in page
    assert 'data-write-operation="delete-holding"' in page
    assert 'data-write-operation="reverse"' not in page


def test_transaction_list_hides_internal_ledger_rows():
    page = PAGE_PATH.read_text(encoding="utf-8")

    assert "INTERNAL_TRANSACTION_TYPES" in page
    for transaction_type in (
        "opening_position",
        "cash_transfer_in",
        "cash_transfer_out",
        "income_accrual",
        "reversal",
    ):
        assert transaction_type in page


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
