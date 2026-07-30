from collections import defaultdict
from datetime import date
from decimal import Decimal

from src.portfolio_models import (
    ProductType,
    TransactionStatus,
    TransactionType,
    decimal_text,
)


_ZERO = Decimal("0")
_LEDGER_PROFIT_TYPES = {
    TransactionType.INCOME_ACCRUAL,
    TransactionType.CASH_DIVIDEND,
}


def build_portfolio_payload(repository, as_of=None) -> dict:
    """Serialize the ledger projections for the dashboard and mobile views."""
    as_of = as_of or date.today()
    products = repository.list_products()
    products_by_id = {product.id: product for product in products}
    rows = [
        _product_row(repository, product, as_of)
        for product in products
    ]
    profit_history = _profit_history(repository)
    summary = _summary(rows, profit_history, as_of)
    return {
        "summary": summary,
        "products": rows,
        "transactions": [
            _transaction_row(transaction, products_by_id)
            for transaction in repository.list_transactions()
        ],
        "sip_plans": [
            _sip_plan_row(repository, plan, products_by_id)
            for plan in repository.list_plans()
        ],
        "profit_history": profit_history,
        "write_enabled": True,
    }


def _product_row(repository, product, as_of):
    position = repository.get_position(product.id)
    quote = repository.latest_quote(product.id, on_or_before=as_of)
    quote_payload = _quote_payload(product.product_type, quote)
    market_value = _market_value(product.product_type, position.total_shares, quote)
    latest_profit = _latest_product_profit(repository, product, as_of)
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
        "cost_basis": decimal_text(position.cost_basis),
        "market_value": _optional_decimal_text(market_value),
        "latest_profit": _optional_decimal_text(latest_profit),
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
        row.update({"latest_nav": unit_nav, "unit_nav": unit_nav})
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


def _latest_product_profit(repository, product, as_of):
    if product.product_type is not ProductType.CASH_MANAGEMENT:
        return None
    entries = [
        transaction.amount or _ZERO
        for transaction in repository.list_transactions(product_id=product.id)
        if (
            transaction.transaction_type is TransactionType.INCOME_ACCRUAL
            and transaction.status is TransactionStatus.CONFIRMED
            and transaction.trade_date == as_of
        )
    ]
    return sum(entries, _ZERO) if entries else None


def _profit_history(repository):
    rows = _legacy_profit_rows(repository)
    ledger_by_date = defaultdict(lambda: _ZERO)
    for transaction in repository.list_transactions():
        if (
            transaction.status is TransactionStatus.CONFIRMED
            and transaction.transaction_type in _LEDGER_PROFIT_TYPES
        ):
            ledger_by_date[transaction.trade_date.isoformat()] += (
                transaction.amount or _ZERO
            )
    rows.extend(
        {
            "date": profit_date,
            "amount": decimal_text(amount),
            "source": "ledger_income",
        }
        for profit_date, amount in ledger_by_date.items()
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


def _summary(products, profit_history, as_of):
    market_value = sum(
        (Decimal(row["market_value"]) for row in products if row["market_value"] is not None),
        _ZERO,
    )
    cost_basis = sum((Decimal(row["cost_basis"]) for row in products), _ZERO)
    cumulative_profit = sum(
        (Decimal(row["amount"]) for row in profit_history), _ZERO
    )
    latest_date = max((row["date"] for row in profit_history), default="")
    latest_profit = sum(
        (Decimal(row["amount"]) for row in profit_history if row["date"] == latest_date),
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


def _transaction_row(transaction, products_by_id):
    product = products_by_id.get(transaction.product_id)
    return {
        "id": transaction.id,
        "product_id": transaction.product_id,
        "product_name": product.name if product else "",
        "product_code": product.code if product else "",
        "transaction_type": transaction.transaction_type.value,
        "status": transaction.status.value,
        "trade_date": transaction.trade_date.isoformat(),
        "confirmation_date": (
            transaction.confirmation_date.isoformat()
            if transaction.confirmation_date else None
        ),
        "amount": _optional_decimal_text(transaction.amount),
        "shares": _optional_decimal_text(transaction.shares),
        "fee_amount": _optional_decimal_text(transaction.fee_amount),
        "fee_rate": _optional_decimal_text(transaction.fee_rate),
        "confirmation_nav": _optional_decimal_text(transaction.confirmation_nav),
        "linked_transaction_id": transaction.linked_transaction_id,
        "plan_id": transaction.plan_id,
        "note": transaction.note,
        "created_by": transaction.created_by,
    }


def _sip_plan_row(repository, plan, products_by_id):
    product = products_by_id.get(plan.product_id)
    source = products_by_id.get(plan.source_cash_product_id)
    executions = repository.list_plan_executions(plan.id)
    last_execution = executions[-1] if executions else None
    return {
        "id": plan.id,
        "product_id": plan.product_id,
        "product_name": product.name if product else "",
        "source_cash_product_id": plan.source_cash_product_id,
        "source": source.name if source else "",
        "daily_amount": decimal_text(plan.daily_amount),
        "purchase_fee_rate": decimal_text(plan.purchase_fee_rate),
        "status": plan.status.value,
        "start_date": plan.start_date.isoformat(),
        "last_execution": (
            last_execution.intended_trade_date.isoformat()
            if last_execution else ""
        ),
        "skip_reason": last_execution.reason if last_execution else "",
    }


def _optional_decimal_text(value):
    return decimal_text(value) if value is not None else None
