from decimal import Decimal

from src.portfolio_models import (
    Position,
    TransactionStatus,
    TransactionType,
)


ZERO = Decimal("0")

SHARE_DIRECTION = {
    TransactionType.OPENING_POSITION: Decimal("1"),
    TransactionType.MANUAL_PURCHASE: Decimal("1"),
    TransactionType.SIP_PURCHASE: Decimal("1"),
    TransactionType.CASH_TRANSFER_IN: Decimal("1"),
    TransactionType.INCOME_ACCRUAL: Decimal("1"),
    TransactionType.HOLDING_ADJUSTMENT: Decimal("1"),
    TransactionType.MANUAL_REDEMPTION: Decimal("-1"),
    TransactionType.CASH_TRANSFER_OUT: Decimal("-1"),
    TransactionType.REVERSAL: Decimal("1"),
}

_APPLIED_STATUSES = {
    TransactionStatus.CONFIRMED,
    TransactionStatus.REVERSED,
}
_PENDING_STATUSES = {
    TransactionStatus.PENDING_QUOTE,
    TransactionStatus.PENDING_CONFIRMATION,
}
_LOCKING_TYPES = {
    TransactionType.MANUAL_REDEMPTION,
    TransactionType.CASH_TRANSFER_OUT,
}
_COST_ADDITION_TYPES = {
    TransactionType.OPENING_POSITION,
    TransactionType.MANUAL_PURCHASE,
    TransactionType.SIP_PURCHASE,
    TransactionType.CASH_TRANSFER_IN,
    TransactionType.INCOME_ACCRUAL,
}
_AVERAGE_COST_OUTFLOW_TYPES = {
    TransactionType.MANUAL_REDEMPTION,
    TransactionType.CASH_TRANSFER_OUT,
}


class PositionProjector:
    def __init__(self, repository):
        self.repository = repository

    def calculate(self, product_id: str) -> Position:
        return self._calculate(product_id)

    def _calculate(self, product_id: str, conn=None) -> Position:
        total_shares = ZERO
        locked_shares = ZERO
        cost_basis = ZERO
        transactions = self.repository.list_transactions(
            product_id=product_id,
            conn=conn,
        )
        neutralized_event_ids = self._linked_reversal_pair_ids(transactions)

        for transaction in transactions:
            if transaction.id in neutralized_event_ids:
                continue
            shares = transaction.shares or ZERO
            if (
                transaction.status in _PENDING_STATUSES
                and transaction.transaction_type in _LOCKING_TYPES
                and shares > ZERO
            ):
                locked_shares += shares
                continue
            if transaction.status not in _APPLIED_STATUSES:
                continue
            if transaction.transaction_type is TransactionType.CASH_DIVIDEND:
                continue

            share_delta = (
                shares * SHARE_DIRECTION[transaction.transaction_type]
            )
            next_total = total_shares + share_delta
            if next_total < ZERO:
                raise ValueError("negative portfolio shares")

            amount = transaction.amount or ZERO
            if transaction.transaction_type in _COST_ADDITION_TYPES:
                cost_basis += amount
            elif transaction.transaction_type in _AVERAGE_COST_OUTFLOW_TYPES:
                cost_basis = self._reduce_average_cost(
                    cost_basis, total_shares, -share_delta
                )
            elif (
                transaction.transaction_type is TransactionType.HOLDING_ADJUSTMENT
            ):
                if share_delta > ZERO:
                    cost_basis += amount
                elif share_delta < ZERO:
                    cost_basis = self._reduce_average_cost(
                        cost_basis, total_shares, -share_delta
                    )
            elif transaction.transaction_type is TransactionType.REVERSAL:
                cost_basis = self._apply_signed_average_cost_change(
                    cost_basis, total_shares, share_delta
                )

            total_shares = next_total

        if locked_shares > total_shares:
            raise ValueError("locked shares exceed total shares")
        return Position(
            product_id=product_id,
            available_shares=total_shares - locked_shares,
            locked_shares=locked_shares,
            total_shares=total_shares,
            cost_basis=cost_basis,
        )

    def rebuild(self, product_id: str | None = None) -> list[Position]:
        with self.repository.database.transaction() as conn:
            product_ids = (
                [product_id]
                if product_id is not None
                else [
                    product.id
                    for product in self.repository.list_products(conn=conn)
                ]
            )
            positions = [
                self._calculate(requested_product_id, conn)
                for requested_product_id in product_ids
            ]
            for position in positions:
                self.repository.replace_position(position, conn)
        return positions

    @staticmethod
    def _linked_reversal_pair_ids(transactions) -> set[str]:
        transactions_by_id = {
            transaction.id: transaction for transaction in transactions
        }
        neutralized_event_ids = set()
        for transaction in transactions:
            if (
                transaction.transaction_type is not TransactionType.REVERSAL
                or transaction.status not in _APPLIED_STATUSES
                or not transaction.linked_transaction_id
            ):
                continue
            original = transactions_by_id.get(transaction.linked_transaction_id)
            if original is None or original.status not in _APPLIED_STATUSES:
                continue
            neutralized_event_ids.add(original.id)
            neutralized_event_ids.add(transaction.id)
        return neutralized_event_ids

    @staticmethod
    def _reduce_average_cost(
        cost_basis: Decimal,
        total_shares: Decimal,
        removed_shares: Decimal,
    ) -> Decimal:
        if removed_shares == ZERO:
            return cost_basis
        if removed_shares == total_shares:
            return ZERO
        return cost_basis - (cost_basis / total_shares * removed_shares)

    @staticmethod
    def _apply_signed_average_cost_change(
        cost_basis: Decimal,
        total_shares: Decimal,
        share_delta: Decimal,
    ) -> Decimal:
        if share_delta == ZERO:
            return cost_basis
        if total_shares + share_delta == ZERO:
            return ZERO
        if total_shares == ZERO:
            return ZERO
        average_cost = cost_basis / total_shares
        return cost_basis + (average_cost * share_delta)
