from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from src.portfolio_models import (
    ProductType,
    RedemptionSettlementStatus,
    TransactionStatus,
    TransactionType,
    decimal_text,
)
from src.portfolio_confirmation import confirmation_schedule
from src.portfolio_profit import calculate_holding_profit, calculate_latest_profit
from src.portfolio_wallet import is_wallet_plus_product


_ZERO = Decimal("0")
# 只统计真实收益：收入计提 + 现金分红。
# 排除 profit_adjustment / latest_profit_adjustment（迁移/校准分录，非真实收益）。
_LEDGER_PROFIT_TYPES = {
    TransactionType.INCOME_ACCRUAL,
    TransactionType.CASH_DIVIDEND,
}
_PENDING_STATUSES = {
    TransactionStatus.PENDING_QUOTE,
    TransactionStatus.PENDING_CONFIRMATION,
}
_PURCHASE_TYPES = {
    TransactionType.MANUAL_PURCHASE,
    TransactionType.SIP_PURCHASE,
}


def build_portfolio_payload(repository, as_of=None) -> dict:
    """Serialize the ledger projections for the dashboard and mobile views."""
    as_of = as_of or date.today()
    products = repository.list_products()
    products_by_id = {product.id: product for product in products}
    transactions = repository.list_transactions()
    rows = [
        _product_row(repository, product, as_of)
        for product in products
    ]
    positions = [
        row
        for row in rows
        if _is_overview_position(row, transactions, as_of)
    ]
    profit_history = _profit_history(repository)
    summary = _summary(rows, profit_history, as_of, repository=repository)
    transactions_by_id = {
        transaction.id: transaction for transaction in transactions
    }
    ordered_transactions = sorted(
        transactions,
        key=lambda transaction: (
            transaction.status_updated_at
            or transaction.created_at
            or "",
            transaction.trade_time
            or f"{transaction.trade_date.isoformat()}T00:00:00",
            transaction.id,
        ),
        reverse=True,
    )
    return {
        "summary": summary,
        "products": rows,
        "positions": positions,
        "transactions": [
            _transaction_row(
                transaction,
                products_by_id,
                transactions_by_id,
            )
            for transaction in ordered_transactions
        ],
        "sip_plans": [
            _sip_plan_row(repository, plan, products_by_id)
            for plan in repository.list_plans()
        ],
        "profit_history": profit_history,
        "write_enabled": True,
    }


def build_product_history_payload(repository, product_id, as_of=None) -> dict:
    """Serialize one product's disclosed quote and profit history."""
    as_of = as_of or date.today()
    product = repository.require_product(product_id)
    quotes = repository.list_quotes(product.id, on_or_before=as_of)
    # 只展示 8 月及之后的明细
    quotes = [q for q in quotes if q.quote_date >= date(2026, 8, 1)]
    history = []
    previous_quote = None
    for quote in quotes:
        profit_date, profit = calculate_latest_profit(
            repository,
            product,
            quote.quote_date,
            bundle_non_trading_days=False,
        )
        daily_profit = profit if profit_date == quote.quote_date else None
        if product.product_type is ProductType.CASH_MANAGEMENT:
            history.append(
                {
                    "date": quote.quote_date.isoformat(),
                    "income_per_10k": _optional_decimal_text(
                        quote.income_per_10k
                    ),
                    "seven_day_yield": _optional_decimal_text(
                        quote.seven_day_annualized_rate
                    ),
                    "profit": _optional_decimal_text(daily_profit),
                }
            )
        else:
            change_pct = None
            if (
                previous_quote is not None
                and previous_quote.unit_nav not in (None, _ZERO)
                and quote.unit_nav is not None
            ):
                change_pct = (
                    (quote.unit_nav - previous_quote.unit_nav)
                    / previous_quote.unit_nav
                    * Decimal("100")
                )
            history.append(
                {
                    "date": quote.quote_date.isoformat(),
                    "unit_nav": _optional_decimal_text(quote.unit_nav),
                    "change_pct": _optional_decimal_text(change_pct),
                    "profit": _optional_decimal_text(daily_profit),
                }
            )
        previous_quote = quote
    return {
        "product": {
            "id": product.id,
            "code": product.code,
            "name": product.name,
            "product_type": product.product_type.value,
        },
        "history": list(reversed(history)),
    }


def _is_overview_position(row, transactions, as_of):
    if Decimal(row["shares"]) > _ZERO:
        return True
    product_id = row["product_id"]
    for transaction in transactions:
        if transaction.product_id != product_id:
            continue
        if (
            transaction.status in _PENDING_STATUSES
            and transaction.transaction_type
            in _PURCHASE_TYPES | {TransactionType.MANUAL_REDEMPTION}
        ):
            return True
        if (
            transaction.transaction_type is TransactionType.MANUAL_REDEMPTION
            and transaction.status is TransactionStatus.CONFIRMED
            and transaction.settlement_date is not None
            and transaction.settlement_date >= as_of
        ):
            return True
    return False


def _product_row(repository, product, as_of):
    position = repository.get_position(product.id)
    quote = repository.latest_quote(product.id, on_or_before=as_of)
    quote_payload = _quote_payload(product.product_type, quote)
    market_value = _market_value(product.product_type, position.total_shares, quote)
    in_transit_amount = sum(
        (
            transaction.amount or _ZERO
            for transaction in repository.list_transactions(
                product_id=product.id,
            )
            if (
                transaction.status in _PENDING_STATUSES
                and transaction.transaction_type in _PURCHASE_TYPES
            )
        ),
        _ZERO,
    )
    latest_profit_date, latest_profit = calculate_latest_profit(
        repository,
        product,
        as_of,
    )
    # 持仓卡片上的"累计持有收益"：8月及之后该产品的每日收益总和
    holding_profit = _ZERO
    for quote in repository.list_quotes(product.id, on_or_before=as_of):
        if quote.quote_date < date(2026, 8, 1):
            continue
        pd, profit = calculate_latest_profit(
            repository,
            product,
            quote.quote_date,
            bundle_non_trading_days=False,
        )
        if pd == quote.quote_date and profit is not None:
            holding_profit += profit
    row = {
        "id": product.id,
        "product_id": product.id,
        "provider": product.provider,
        "code": product.code,
        "name": product.name,
        "product_type": product.product_type.value,
        "type": product.product_type.value,
        "wealth_type": product.product_type.value,
        "status": product.status.value,
        "currency": product.currency,
        "registration_code": product.registration_code,
        "shares": decimal_text(position.total_shares),
        "available_shares": decimal_text(position.available_shares),
        "locked_shares": decimal_text(position.locked_shares),
        "in_transit_amount": decimal_text(in_transit_amount),
        "cost_basis": decimal_text(position.cost_basis),
        "market_value": _optional_decimal_text(market_value),
        "latest_profit": _optional_decimal_text(latest_profit),
        "latest_profit_date": (
            latest_profit_date.isoformat() if latest_profit_date else None
        ),
        "holding_profit": decimal_text(holding_profit),
        "cumulative_profit": decimal_text(holding_profit),
        "quote_status": "ready" if market_value is not None else "pending",
        "quote": quote_payload,
    }
    if product.product_type is ProductType.CASH_MANAGEMENT:
        row.update(
            {
                "latest_nav": None,
                "per_10k_yield": quote_payload.get("income_per_10k"),
                "seven_day_yield": quote_payload.get(
                    "seven_day_annualized_rate"
                ),
            }
        )
    else:
        unit_nav = quote_payload.get("unit_nav")
        previous_quote = (
            repository.latest_quote(
                product.id,
                on_or_before=quote.quote_date - timedelta(days=1),
            )
            if quote is not None
            else None
        )
        change_pct = None
        if (
            quote is not None
            and quote.unit_nav is not None
            and previous_quote is not None
            and previous_quote.unit_nav not in (None, _ZERO)
        ):
            change_pct = (
                (quote.unit_nav - previous_quote.unit_nav)
                / previous_quote.unit_nav
                * Decimal("100")
            )
        row.update(
            {
                "latest_nav": unit_nav,
                "unit_nav": unit_nav,
                "change_pct": _optional_decimal_text(change_pct),
            }
        )
    return row


def _quote_payload(product_type, quote):
    if quote is None:
        return {}
    payload = {
        "date": quote.quote_date.isoformat(),
        "source": quote.source,
    }
    if product_type is ProductType.CASH_MANAGEMENT:
        if quote.income_per_10k is not None:
            payload["income_per_10k"] = decimal_text(quote.income_per_10k)
        if quote.seven_day_annualized_rate is not None:
            payload["seven_day_annualized_rate"] = decimal_text(
                quote.seven_day_annualized_rate
            )
    else:
        if quote.unit_nav is not None:
            payload["unit_nav"] = decimal_text(quote.unit_nav)
        if quote.cumulative_nav is not None:
            payload["cumulative_nav"] = decimal_text(quote.cumulative_nav)
    return payload


def _market_value(product_type, shares, quote):
    if product_type is ProductType.CASH_MANAGEMENT:
        return shares
    if quote is None or quote.unit_nav is None:
        return None
    return shares * quote.unit_nav


def _profit_history(repository):
    rows = _legacy_profit_rows(repository)
    ledger_by_date_and_source = defaultdict(lambda: _ZERO)
    for transaction in repository.list_transactions():
        if (
            transaction.status is TransactionStatus.CONFIRMED
            and transaction.transaction_type in _LEDGER_PROFIT_TYPES
            # 只统计 8 月及之后的收益（历史已清零）
            and transaction.trade_date >= date(2026, 8, 1)
        ):
            source = (
                "ledger_cumulative_adjustment"
                if transaction.transaction_type
                is TransactionType.PROFIT_ADJUSTMENT
                else "ledger_income"
            )
            ledger_by_date_and_source[
                (transaction.trade_date.isoformat(), source)
            ] += (
                transaction.amount or _ZERO
            )
    rows.extend(
        {
            "date": profit_date,
            "amount": decimal_text(amount),
            "source": source,
        }
        for (profit_date, source), amount
        in ledger_by_date_and_source.items()
    )
    return sorted(rows, key=lambda row: (row["date"], row["source"]))


def _legacy_profit_rows(repository):
    with repository.database.connection() as connection:
        rows = connection.execute(
            """SELECT profit_date, amount, source_kind
               FROM legacy_profit_history
               ORDER BY profit_date, id"""
        ).fetchall()
    return [
        {
            "date": row["profit_date"],
            "amount": decimal_text(row["amount"]),
            "source": f"legacy_{row['source_kind']}",
        }
        for row in rows
    ]


def _summary(products, profit_history, as_of, repository=None):
    market_value = sum(
        (Decimal(row["market_value"]) for row in products if row["market_value"] is not None),
        _ZERO,
    )
    cost_basis = sum((Decimal(row["cost_basis"]) for row in products), _ZERO)
    # 累计收益 = 各产品 holding_profit（即 latest_profit）之和
    # 与 NAV 看板的 daily_profits 总和一致
    cumulative_profit = sum(
        (Decimal(row.get("holding_profit") or "0") for row in products), _ZERO
    )
    # 最新收益：取各产品 latest_profit（现金含周末合并披露）在全局最新日期上的合计
    latest_date = max(
        (row.get("latest_profit_date") or "" for row in products),
        default="",
    )
    latest_profit = sum(
        (
            Decimal(row["latest_profit"])
            for row in products
            if (
                row.get("latest_profit_date") == latest_date
                and row.get("latest_profit") not in (None, "")
            )
        ),
        _ZERO,
    )
    return {
        "as_of": as_of.isoformat(),
        "market_value": decimal_text(market_value),
        "cost_basis": decimal_text(cost_basis),
        "unrealized_profit": decimal_text(market_value - cost_basis),
        "cumulative_profit": decimal_text(cumulative_profit),
        "latest_profit_date": latest_date,
        "latest_profit": decimal_text(latest_profit),
        "product_count": len(products),
        "quote_pending_count": sum(
            1 for row in products if row["quote_status"] == "pending"
        ),
    }


def _parse_trade_time(trade_time: str, trade_date: date) -> datetime:
    """兼容两种 trade_time 格式: HH:MM:SS 和 YYYY-MM-DDTHH:MM:SS"""
    if trade_time and 'T' in trade_time:
        return datetime.fromisoformat(trade_time)
    return datetime.combine(trade_date, time.fromisoformat(trade_time))


def _transaction_row(transaction, products_by_id, transactions_by_id):
    product = products_by_id.get(transaction.product_id)
    linked = transactions_by_id.get(transaction.linked_transaction_id)
    linked_product = (
        products_by_id.get(linked.product_id) if linked is not None else None
    )
    destination_product = products_by_id.get(
        transaction.destination_cash_product_id
    )
    origin = transactions_by_id.get(transaction.origin_transaction_id)
    origin_product = (
        products_by_id.get(origin.product_id) if origin is not None else None
    )
    purchase_origin = (
        f"{origin_product.name}赎回到账"
        if origin_product is not None
        and transaction.transaction_type is TransactionType.MANUAL_PURCHASE
        else ""
    )
    display_status = transaction.status.value
    if (
        transaction.transaction_type is TransactionType.MANUAL_REDEMPTION
        and transaction.status is TransactionStatus.CONFIRMED
    ):
        if (
            transaction.settlement_status
            is RedemptionSettlementStatus.PENDING
        ):
            display_status = "pending_settlement"
        elif (
            transaction.settlement_status
            is RedemptionSettlementStatus.SETTLED
        ):
            display_status = "settled"
    expected_confirmation_date = transaction.confirmation_date
    wallet_plus_redemption = (
        product is not None
        and is_wallet_plus_product(product)
        and transaction.transaction_type is TransactionType.MANUAL_REDEMPTION
    )
    if wallet_plus_redemption and expected_confirmation_date is None:
        expected_confirmation_date = (
            transaction.settlement_date or transaction.trade_date
        )
    elif (
        expected_confirmation_date is None
        and product is not None
        and transaction.transaction_type
        in {
            TransactionType.MANUAL_PURCHASE,
            TransactionType.MANUAL_REDEMPTION,
            TransactionType.SIP_PURCHASE,
        }
        and (
            transaction.trade_time
            or transaction.transaction_type is TransactionType.SIP_PURCHASE
        )
    ):
        expected_confirmation_date = confirmation_schedule(
            product,
            transaction.transaction_type,
            (
                _parse_trade_time(transaction.trade_time, transaction.trade_date)
                if transaction.trade_time
                else datetime.combine(
                    transaction.trade_date, datetime.min.time()
                )
            ),
        ).confirmation_date
    income_start_date = None
    income_stop_date = None
    if transaction.transaction_type in {
        TransactionType.MANUAL_PURCHASE,
        TransactionType.SIP_PURCHASE,
    }:
        income_start_date = expected_confirmation_date
    elif transaction.transaction_type is TransactionType.MANUAL_REDEMPTION:
        income_stop_date = expected_confirmation_date
    row = {
        "id": transaction.id,
        "product_id": transaction.product_id,
        "product_name": product.name if product else "",
        "product_code": product.code if product else "",
        "transaction_type": transaction.transaction_type.value,
        "status": transaction.status.value,
        "display_status": display_status,
        "trade_date": transaction.trade_date.isoformat(),
        "trade_time": transaction.trade_time or None,
        "effective_trade_date": transaction.trade_date.isoformat(),
        "expected_confirmation_date": (
            expected_confirmation_date.isoformat()
            if expected_confirmation_date else None
        ),
        "income_start_date": (
            income_start_date.isoformat() if income_start_date else None
        ),
        "income_stop_date": (
            income_stop_date.isoformat() if income_stop_date else None
        ),
        "confirmation_date": (
            transaction.confirmation_date.isoformat()
            if transaction.confirmation_date else None
        ),
        "settlement_date": (
            transaction.settlement_date.isoformat()
            if transaction.settlement_date else None
        ),
        "settlement_status": (
            transaction.settlement_status.value
            if transaction.settlement_status is not None
            else None
        ),
        "settled_at": transaction.settled_at,
        "created_at": transaction.created_at,
        "status_updated_at": transaction.status_updated_at,
        "amount": _optional_decimal_text(transaction.amount),
        "shares": _optional_decimal_text(transaction.shares),
        "fee_amount": _optional_decimal_text(transaction.fee_amount),
        "fee_rate": _optional_decimal_text(transaction.fee_rate),
        "confirmation_nav": _optional_decimal_text(transaction.confirmation_nav),
        "linked_transaction_id": transaction.linked_transaction_id,
        "plan_id": transaction.plan_id,
        "note": transaction.note,
        "created_by": transaction.created_by,
        "origin_transaction_id": transaction.origin_transaction_id,
        "purchase_origin": purchase_origin or None,
    }
    if transaction.transaction_type in {
        TransactionType.MANUAL_PURCHASE,
        TransactionType.SIP_PURCHASE,
    }:
        row["funding_source"] = purchase_origin or (
            linked_product.name if linked_product is not None else "钱包"
        )
    elif transaction.transaction_type is TransactionType.MANUAL_REDEMPTION:
        row["redemption_destination"] = (
            destination_product.name
            if destination_product is not None
            else linked_product.name if linked_product is not None else "钱包"
        )
    return row


def _sip_purchase_for_deduction(deduction, transactions):
    for transaction in transactions:
        if transaction.transaction_type is not TransactionType.SIP_PURCHASE:
            continue
        if transaction.linked_transaction_id == deduction.id:
            return transaction
        if deduction.linked_transaction_id == transaction.id:
            return transaction
    return None


def _sip_plan_records(deductions, transactions):
    records = []
    for deduction in sorted(
        deductions,
        key=lambda row: (row.trade_date.isoformat(), row.id),
        reverse=True,
    ):
        purchase = _sip_purchase_for_deduction(deduction, transactions)
        status = purchase.status if purchase is not None else deduction.status
        shares = purchase.shares if purchase is not None else None
        records.append(
            {
                "date": deduction.trade_date.isoformat(),
                "amount": decimal_text(deduction.amount or _ZERO),
                "status": status.value,
                "shares": _optional_decimal_text(shares),
            }
        )
    return records


def _sip_plan_row(repository, plan, products_by_id):
    product = products_by_id.get(plan.product_id)
    source = products_by_id.get(plan.source_cash_product_id)
    executions = repository.list_plan_executions(plan.id)
    last_execution = next(
        (
            execution
            for execution in reversed(executions)
            if execution.status in {"pending_quote", "confirmed"}
            and execution.transaction_id
        ),
        None,
    )
    transactions = repository.list_transactions()
    transactions_by_id = {transaction.id: transaction for transaction in transactions}
    execution_cash_ids = set()
    active_execution_cash_ids = set()
    for execution in executions:
        purchase = transactions_by_id.get(execution.transaction_id)
        cash_id = purchase.linked_transaction_id if purchase is not None else ""
        if not cash_id:
            continue
        execution_cash_ids.add(cash_id)
        cash = transactions_by_id.get(cash_id)
        if (
            execution.status in {"pending_quote", "confirmed"}
            and cash is not None
            and cash.status is TransactionStatus.CONFIRMED
        ):
            active_execution_cash_ids.add(cash_id)
    refunded_cash_ids = {
        transaction.linked_transaction_id
        for transaction in transactions
        if (
            transaction.plan_id == plan.id
            and transaction.transaction_type
            is TransactionType.CASH_TRANSFER_IN
            and transaction.status is TransactionStatus.CONFIRMED
            and transaction.linked_transaction_id
        )
    }
    deductions = [
        transaction
        for transaction in transactions
        if (
            transaction.plan_id == plan.id
            and transaction.transaction_type
            is TransactionType.CASH_TRANSFER_OUT
            and (
                transaction.id in active_execution_cash_ids
                or (
                    transaction.id not in execution_cash_ids
                    and transaction.status is TransactionStatus.CONFIRMED
                    and transaction.id not in refunded_cash_ids
                )
            )
        )
    ]
    return {
        "id": plan.id,
        "product_id": plan.product_id,
        "product_name": product.name if product else "",
        "source_cash_product_id": plan.source_cash_product_id,
        "source": source.name if source else "",
        "daily_amount": decimal_text(plan.daily_amount),
        "purchase_fee_rate": decimal_text(plan.purchase_fee_rate),
        "purchase_fee_rate_percent": decimal_text(
            plan.purchase_fee_rate * Decimal("100")
        ),
        "status": plan.status.value,
        "start_date": plan.start_date.isoformat(),
        "frequency": plan.frequency.value,
        "schedule_day": plan.schedule_day,
        "last_execution": (
            last_execution.intended_trade_date.isoformat()
            if last_execution else ""
        ),
        "deduction_count": len(deductions),
        "deduction_amount": decimal_text(
            sum((row.amount or Decimal("0") for row in deductions), Decimal("0"))
        ),
        "records": _sip_plan_records(deductions, transactions),
        "skip_reason": "",
    }


def _optional_decimal_text(value):
    return decimal_text(value) if value is not None else None
