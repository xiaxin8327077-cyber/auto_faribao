from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from src.beijing_time import TZ_CN
from src.portfolio_models import ProductType, TransactionStatus, TransactionType


ZERO = Decimal("0")
_WALLET_PLUS_PROVIDER = "wallet_plus"
_PENDING_STATUSES = {
    TransactionStatus.PENDING_QUOTE,
    TransactionStatus.PENDING_CONFIRMATION,
}
_APPLIED_STATUSES = {
    TransactionStatus.CONFIRMED,
    TransactionStatus.REVERSED,
}
_PURCHASE_TYPES = {
    TransactionType.MANUAL_PURCHASE,
    TransactionType.SIP_PURCHASE,
}
_OUTFLOW_TYPES = {
    TransactionType.CASH_TRANSFER_OUT,
    TransactionType.MANUAL_REDEMPTION,
}


@dataclass(frozen=True)
class WalletPendingPurchaseAllocations:
    remaining_by_purchase: dict[str, Decimal]
    confirmed_shares_by_purchase: dict[str, Decimal]
    allocated_by_outflow: dict[str, Decimal]
    restored_by_inflow: dict[str, Decimal]
    allocations_by_outflow: dict[str, tuple[tuple[str, Decimal], ...]]


def is_wallet_plus_product(product) -> bool:
    return (
        product.provider == _WALLET_PLUS_PROVIDER
        and product.product_type is ProductType.CASH_MANAGEMENT
    )


def redemption_settlement_purchase_is_start_of_day(product, transaction):
    return (
        is_wallet_plus_product(product)
        and transaction.transaction_type is TransactionType.MANUAL_PURCHASE
        and transaction.created_by == "redemption_settlement"
        and bool(transaction.origin_transaction_id)
        and (transaction.shares or ZERO) > ZERO
    )


def wallet_pending_purchase_allocations(
    product,
    transactions,
    *,
    include_confirmed_purchases=False,
    neutralized_event_ids=(),
) -> WalletPendingPurchaseAllocations:
    """Allocate wallet outflows to the pending purchase lots available then.

    A confirmation moves only the unspent part of its purchase lot into
    confirmed shares.  Later events never revisit an earlier outflow.
    """
    if not is_wallet_plus_product(product):
        return WalletPendingPurchaseAllocations({}, {}, {}, {}, {})

    neutralized_event_ids = set(neutralized_event_ids)
    transactions = [
        transaction
        for transaction in transactions
        if transaction.id not in neutralized_event_ids
    ]
    candidates = [
        transaction
        for transaction in transactions
        if _is_pending_purchase_candidate(
            transaction,
            include_confirmed_purchases=include_confirmed_purchases,
        )
    ]
    candidate_ids = {transaction.id for transaction in candidates}
    events = [
        (wallet_economic_event_key(product, transaction), "transaction", transaction)
        for transaction in transactions
        if (
            transaction.id in candidate_ids
            or (
                transaction.status in _APPLIED_STATUSES
                and transaction.transaction_type
                in _OUTFLOW_TYPES | {TransactionType.CASH_TRANSFER_IN}
            )
        )
    ]
    if include_confirmed_purchases:
        for transaction in candidates:
            if _confirmation_moves_pending_to_confirmed(transaction):
                events.append((
                    (
                        f"{transaction.confirmation_date.isoformat()}T00:00:00",
                        0,
                        transaction.created_at or "",
                        transaction.id,
                    ),
                    "confirmation",
                    transaction,
                ))

    remaining_by_purchase = OrderedDict()
    confirmed_shares_by_purchase = {}
    allocations_by_outflow = {}
    allocated_by_outflow = {}
    restored_by_inflow = {}
    for _, event_kind, transaction in sorted(events, key=lambda event: event[0]):
        if event_kind == "confirmation":
            confirmed_shares_by_purchase[transaction.id] = (
                remaining_by_purchase.pop(transaction.id, ZERO)
            )
            continue

        shares = _transaction_shares(transaction)
        if transaction.id in candidate_ids:
            remaining_by_purchase[transaction.id] = shares
            continue
        if transaction.transaction_type in _OUTFLOW_TYPES:
            amount_left = shares
            allocations = []
            for purchase_id, balance in remaining_by_purchase.items():
                allocated = min(balance, amount_left)
                if allocated > ZERO:
                    remaining_by_purchase[purchase_id] -= allocated
                    allocations.append((purchase_id, allocated))
                    amount_left -= allocated
                if amount_left <= ZERO:
                    break
            allocations_by_outflow[transaction.id] = tuple(allocations)
            allocated_by_outflow[transaction.id] = shares - amount_left
        elif transaction.transaction_type is TransactionType.CASH_TRANSFER_IN:
            amount_left = shares
            for purchase_id, allocated in allocations_by_outflow.get(
                transaction.linked_transaction_id,
                (),
            ):
                if purchase_id not in remaining_by_purchase:
                    continue
                restored = min(allocated, amount_left)
                if restored > ZERO:
                    remaining_by_purchase[purchase_id] += restored
                    amount_left -= restored
                if amount_left <= ZERO:
                    break
            restored_by_inflow[transaction.id] = shares - amount_left

    return WalletPendingPurchaseAllocations(
        remaining_by_purchase=dict(remaining_by_purchase),
        confirmed_shares_by_purchase=confirmed_shares_by_purchase,
        allocated_by_outflow=allocated_by_outflow,
        restored_by_inflow=restored_by_inflow,
        allocations_by_outflow=allocations_by_outflow,
    )


def wallet_economic_event_key(product, transaction):
    if redemption_settlement_purchase_is_start_of_day(product, transaction):
        event_time = f"{transaction.trade_date.isoformat()}T00:00:00"
        event_order = -1
    else:
        event_time = _event_time(transaction)
        event_order = {
            TransactionType.CASH_TRANSFER_OUT: 0,
            TransactionType.MANUAL_REDEMPTION: 0,
            TransactionType.CASH_TRANSFER_IN: 1,
            TransactionType.MANUAL_PURCHASE: 2,
            TransactionType.SIP_PURCHASE: 2,
        }.get(transaction.transaction_type, 3)
    return (
        event_time,
        event_order,
        transaction.created_at or "",
        transaction.id,
    )


def _is_pending_purchase_candidate(
    transaction,
    *,
    include_confirmed_purchases,
):
    if transaction.transaction_type not in _PURCHASE_TYPES:
        return False
    if _transaction_shares(transaction) <= ZERO:
        return False
    if transaction.status in _PENDING_STATUSES:
        return True
    return (
        include_confirmed_purchases
        and transaction.status in _APPLIED_STATUSES
        and transaction.confirmation_date is not None
        and transaction.confirmation_date > transaction.trade_date
    )


def _confirmation_moves_pending_to_confirmed(transaction):
    return (
        transaction.status in _APPLIED_STATUSES
        and transaction.confirmation_date is not None
        and transaction.confirmation_date > transaction.trade_date
    )


def _event_time(transaction):
    trade_time = transaction.trade_time or ""
    if "T" in trade_time:
        return trade_time
    clock_time = trade_time or "00:00:00"
    if not trade_time and transaction.created_at:
        created = datetime.fromisoformat(transaction.created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        local_created = created.astimezone(TZ_CN)
        if local_created.date() == transaction.trade_date:
            clock_time = local_created.strftime("%H:%M:%S")
    return f"{transaction.trade_date.isoformat()}T{clock_time}"


def _transaction_shares(transaction):
    return transaction.shares or transaction.amount or ZERO
