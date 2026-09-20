"""Reconcile complete business operations inside their SQLite transaction.

Stored positions are checked against the ledger, and the change in assets is
checked independently against primary trade instructions. Cash legs must balance
their purchase/redemption; merely rebuilding a wrong ledger is not sufficient.
"""
from contextlib import contextmanager
from decimal import Decimal
import json
import logging
from uuid import uuid4

from src.portfolio_models import (
    ProductType, TransactionType as Kind, TransactionStatus as Status,
    RedemptionSettlementStatus as Settlement, Position, decimal_text,
)
from src.portfolio_positions import PositionProjector, pending_purchase_is_in_current_position

ZERO = Decimal("0")
ONE = Decimal("1")
PENDING = {Status.PENDING_QUOTE, Status.PENDING_CONFIRMATION}
PURCHASES = {Kind.MANUAL_PURCHASE, Kind.SIP_PURCHASE}
# Decimal division can leave sub-fen residues. Never tolerate a cent of drift.
TOLERANCE = Decimal("0.000000000001")
logger = logging.getLogger(__name__)


class ReconciliationError(ValueError):
    pass


def transit_assets(products, transactions):
    """Only funds absent from current holdings add to portfolio assets."""
    by_id = {tx.id: tx for tx in transactions}
    pending_buy = ZERO
    pending_redemption = ZERO
    for tx in transactions:
        if tx.status in PENDING and tx.transaction_type in PURCHASES:
            if pending_purchase_is_in_current_position(products[tx.product_id], tx):
                continue
            funding = by_id.get(tx.linked_transaction_id)
            if funding is not None and funding.transaction_type is Kind.CASH_TRANSFER_OUT and funding.status in PENDING:
                # Locked source cash is still owned and already in holdings.
                continue
            pending_buy += tx.amount or ZERO
        elif (tx.transaction_type is Kind.MANUAL_REDEMPTION
              and tx.status is Status.CONFIRMED
              and tx.settlement_status is Settlement.PENDING):
            pending_redemption += tx.amount or ZERO
    # Pending redemption shares are locked, not removed, so do not subtract them.
    return pending_buy, pending_redemption


def _snapshot(repository, conn):
    transactions = repository.list_transactions(conn=conn)
    positions = {
        row["product_id"]: repository._position_from_row(row)
        for row in conn.execute("SELECT * FROM positions")
    }
    return transactions, positions


def _active(transactions):
    neutralized = PositionProjector._linked_reversal_pair_ids(transactions)
    return [tx for tx in transactions if tx.id not in neutralized]


def _instruction_value(transactions, products, prices):
    """Net value implied by primary trades, independent of cash-leg amounts."""
    total = ZERO
    for tx in _active(transactions):
        applied = tx.status in {Status.CONFIRMED, Status.REVERSED}
        pending = tx.status in PENDING
        if not applied and not pending:
            continue
        kind = tx.transaction_type
        amount, shares = tx.amount or ZERO, tx.shares or ZERO
        price = prices[tx.product_id]
        if kind in PURCHASES:
            if applied or pending_purchase_is_in_current_position(products[tx.product_id], tx):
                value = shares * price
            else:
                value = amount
            # Internal funding and redemption arrivals are transfers, not inflows.
            total += value - (amount if tx.linked_transaction_id or tx.origin_transaction_id else ZERO)
        elif kind is Kind.MANUAL_REDEMPTION and applied:
            internal_or_receivable = (
                tx.linked_transaction_id or tx.destination_cash_product_id
                or tx.settlement_status is Settlement.PENDING
            )
            total += (amount if internal_or_receivable else ZERO) - shares * price
        elif kind is Kind.CASH_DIVIDEND and applied:
            if tx.linked_transaction_id:
                total += amount
        elif kind in {Kind.OPENING_POSITION, Kind.INCOME_ACCRUAL,
                      Kind.HOLDING_ADJUSTMENT, Kind.LATEST_PROFIT_ADJUSTMENT} and applied:
            total += shares * price
        elif kind in {Kind.CASH_TRANSFER_IN, Kind.CASH_TRANSFER_OUT} and applied and not tx.linked_transaction_id:
            total += shares * price * (ONE if kind is Kind.CASH_TRANSFER_IN else -ONE)
    return total


def _assets(snapshot, products, prices):
    transactions, positions = snapshot
    holdings = sum((p.total_shares * prices[pid] for pid, p in positions.items()), ZERO)
    buy, sell = transit_assets(products, transactions)
    return holdings + buy + sell


def _fail(detail):
    raise ReconciliationError(f"自动对账失败，交易未提交：{detail}")


def _validate_cash_receipt(tx, by_id, products):
    parent = by_id.get(tx.linked_transaction_id)
    if parent is None or products[tx.product_id].product_type is not ProductType.CASH_MANAGEMENT:
        _fail(f"到账流水缺少有效关联 {tx.id}")
    if parent.transaction_type in {Kind.CASH_DIVIDEND, Kind.MANUAL_REDEMPTION}:
        valid_link = parent.linked_transaction_id == tx.id
        expected_status = parent.status
        confirmation_date = parent.confirmation_date
        actor = parent.created_by
    elif parent.transaction_type is Kind.CASH_TRANSFER_OUT:
        purchase = by_id.get(parent.linked_transaction_id)
        valid_link = (
            parent.product_id == tx.product_id
            and parent.status is Status.CONFIRMED
            and purchase is not None and purchase.transaction_type in PURCHASES
            and purchase.linked_transaction_id == parent.id
            and purchase.status is Status.CANCELLED
        )
        expected_status = Status.CONFIRMED
        confirmation_date = parent.trade_date
        actor = "sip" if purchase is not None and purchase.transaction_type is Kind.SIP_PURCHASE else parent.created_by
    else:
        _fail(f"到账流水关联类型错误 {tx.id}")
    if (not valid_link or tx.status is not expected_status
        or tx.amount != parent.amount or tx.shares != parent.amount
        or tx.trade_date != parent.trade_date or tx.plan_id != parent.plan_id
        or tx.created_by != actor or tx.fee_amount is not None or tx.fee_rate is not None
        or (tx.status in {Status.CONFIRMED, Status.REVERSED}
            and (tx.confirmation_nav != ONE or tx.confirmation_date != confirmation_date))):
        _fail(f"到账流水金额、状态或确认日期不匹配 {tx.id}")


def _validate_trade(tx, by_id, products):
    if tx.status not in {Status.CONFIRMED, Status.REVERSED, *PENDING}:
        return
    product = products[tx.product_id]
    if tx.transaction_type is Kind.CASH_TRANSFER_IN and tx.linked_transaction_id:
        _validate_cash_receipt(tx, by_id, products)
    if tx.transaction_type in PURCHASES and tx.status in {Status.CONFIRMED, Status.REVERSED}:
        if tx.amount is None or tx.shares is None or not tx.confirmation_nav:
            _fail(f"申购金额或份额不完整 {tx.id}")
        fee = tx.fee_amount or ZERO
        expected_fee = tx.amount * (tx.fee_rate or ZERO)
        expected_shares = (tx.amount if product.product_type is ProductType.CASH_MANAGEMENT
                           else (tx.amount - fee) / tx.confirmation_nav)
        if fee != expected_fee or tx.shares != expected_shares:
            _fail(f"申购份额、净值与费用不匹配 {tx.id}")
    if tx.transaction_type in PURCHASES and tx.linked_transaction_id:
        cash = by_id.get(tx.linked_transaction_id)
        if (cash is None or cash.transaction_type is not Kind.CASH_TRANSFER_OUT
            or cash.linked_transaction_id != tx.id or cash.amount != tx.amount
            or cash.shares != tx.amount):
            _fail(f"申购与资金转出不匹配 {tx.id}")
        source = products[cash.product_id]
        immediate = tx.transaction_type is Kind.SIP_PURCHASE or source.provider == "wallet_plus"
        expected_status = Status.CONFIRMED if tx.status in PENDING and immediate else tx.status
        allowed_confirmation_dates = {tx.confirmation_date}
        if immediate:
            allowed_confirmation_dates.add(tx.trade_date)
        if (source.product_type is not ProductType.CASH_MANAGEMENT
            or source.id == tx.product_id or cash.trade_date != tx.trade_date
            or cash.plan_id != tx.plan_id or cash.created_by != tx.created_by
            or cash.fee_amount is not None or cash.fee_rate is not None
            or cash.status is not expected_status
            or (tx.transaction_type is Kind.SIP_PURCHASE and not tx.plan_id)
            or (cash.status in {Status.CONFIRMED, Status.REVERSED}
                and (cash.confirmation_nav != ONE
                     or cash.confirmation_date not in allowed_confirmation_dates))):
            _fail(f"申购与扣款状态或来源不匹配 {tx.id}")
    if (tx.transaction_type is Kind.MANUAL_REDEMPTION
        and tx.status in {Status.CONFIRMED, Status.REVERSED}
        and tx.amount is not None and tx.shares is not None and tx.confirmation_nav is not None
        and abs(tx.amount - tx.shares * tx.confirmation_nav) > TOLERANCE):
        _fail(f"赎回金额与份额不匹配 {tx.id}")
    if tx.origin_transaction_id:
        origin = by_id.get(tx.origin_transaction_id)
        if (origin is None or origin.transaction_type is not Kind.MANUAL_REDEMPTION
            or origin.settlement_status is not Settlement.SETTLED
            or origin.destination_cash_product_id != tx.product_id
            or origin.amount != tx.amount):
            _fail(f"赎回到账与申购金额不匹配 {tx.id}")
    if (tx.transaction_type is Kind.MANUAL_REDEMPTION
        and tx.status is Status.CONFIRMED
        and tx.settlement_status is Settlement.SETTLED
        and tx.destination_cash_product_id and not tx.linked_transaction_id):
        arrivals = [r for r in by_id.values() if r.origin_transaction_id == tx.id
                    and r.status in {Status.CONFIRMED, *PENDING}]
        if (len(arrivals) != 1 or arrivals[0].amount != tx.amount
            or arrivals[0].product_id != tx.destination_cash_product_id):
            _fail(f"赎回到账缺失或重复 {tx.id}")


def reconcile_operation(repository, conn, before, after, operation):
    old_tx, old_positions = before
    transactions, positions = after
    old = {tx.id: tx for tx in old_tx}
    current = {tx.id: tx for tx in transactions}
    changed = {key for key in old.keys() | current.keys() if old.get(key) != current.get(key)}
    changed_positions = {pid for pid in old_positions.keys() | positions.keys()
                         if old_positions.get(pid) != positions.get(pid)}
    if not changed and not changed_positions:
        return
    products = {p.id: p for p in repository.list_products(conn=conn)}
    affected = {tx.product_id for key in changed for tx in (old.get(key), current.get(key)) if tx is not None}
    affected.update(changed_positions)
    projector = PositionProjector(repository)
    for pid in affected:
        expected = projector._calculate(pid, conn)
        stored = positions.get(pid, Position(pid, ZERO, ZERO, ZERO, ZERO))
        if stored != expected:
            _fail(f"持仓与交易流水不一致 {pid}")
        if stored.available_shares + stored.locked_shares != stored.total_shares:
            _fail(f"可用与冻结份额不平衡 {pid}")
    for key in changed:
        if key not in current:
            _fail(f"交易流水被删除 {key}")
        _validate_trade(current[key], current, products)
    prices = {}
    unquoted = []
    for pid, product in products.items():
        if product.product_type is ProductType.CASH_MANAGEMENT:
            prices[pid] = ONE
        else:
            quote = repository.latest_quote(pid, conn=conn)
            if quote is not None and quote.unit_nav is not None:
                prices[pid] = quote.unit_nav
            else:
                navs = [t.confirmation_nav for t in transactions if t.product_id == pid and t.confirmation_nav]
                prices[pid] = navs[-1] if navs else ONE
                unquoted.append(pid)
    # Freeze one set of prices across both snapshots: market movements are not
    # mistaken for lost cash, while fees and trade-price differences stay visible.
    before_assets = _assets(before, products, prices)
    after_assets = _assets(after, products, prices)
    expected_delta = _instruction_value(transactions, products, prices) - _instruction_value(old_tx, products, prices)
    actual_delta = after_assets - before_assets
    if abs(actual_delta - expected_delta) > TOLERANCE:
        _fail(f"资产变动不平衡，实际 {decimal_text(actual_delta)}，应为 {decimal_text(expected_delta)}")
    repository.append_audit(
        str(uuid4()), "portfolio_reconciliation", "transaction_batch", operation,
        before_json=json.dumps({"assets": decimal_text(before_assets)}),
        after_json=json.dumps({
            "assets": decimal_text(after_assets),
            "actual_delta": decimal_text(actual_delta),
            "expected_delta": decimal_text(expected_delta),
            "transaction_ids": sorted(changed), "product_ids": sorted(affected),
            "valuation_fallback_products": unquoted,
        }, ensure_ascii=False, sort_keys=True),
        source="automatic_reconciliation", conn=conn,
    )


@contextmanager
def reconciled_transaction(repository, operation):
    try:
        with repository.database.transaction() as conn:
            before = _snapshot(repository, conn)
            yield conn
            reconcile_operation(repository, conn, before, _snapshot(repository, conn), operation)
    except ReconciliationError:
        logger.exception("Portfolio reconciliation rejected operation %s", operation)
        raise
