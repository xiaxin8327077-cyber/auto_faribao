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

    def calculate_confirmed_as_of(self, product_id: str, as_of) -> Position:
        return self._calculate(
            product_id,
            as_of=as_of,
            use_confirmation_date=True,
        )

    def _calculate(
        self,
        product_id: str,
        conn=None,
        as_of=None,
        use_confirmation_date=False,
    ) -> Position:
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
            effective_date = (
                transaction.confirmation_date or transaction.trade_date
                if use_confirmation_date
                else transaction.trade_date
            )
            if as_of is not None and effective_date > as_of:
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
            if transaction.transaction_type in {
                TransactionType.CASH_DIVIDEND,
                TransactionType.PROFIT_ADJUSTMENT,
                TransactionType.LATEST_PROFIT_ADJUSTMENT,
            }:
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
        reversals = [
            transaction
            for transaction in transactions
            if (
                transaction.transaction_type is TransactionType.REVERSAL
                and transaction.status in _APPLIED_STATUSES
            )
        ]
        original_by_reversal_id = {}
        reversal_by_original_id = {}

        for reversal in reversals:
            if not reversal.linked_transaction_id:
                raise ValueError("invalid reversal link")
            original = transactions_by_id.get(reversal.linked_transaction_id)
            if (
                original is None
                or original.product_id != reversal.product_id
            ):
                raise ValueError("invalid reversal link")
            if original.id in reversal_by_original_id:
                raise ValueError("duplicate reversal link")
            if (
                original.status is not TransactionStatus.REVERSED
                or reversal.status
                not in {
                    TransactionStatus.CONFIRMED,
                    TransactionStatus.REVERSED,
                }
            ):
                raise ValueError("invalid reversal status")
            if (
                original.shares is None
                or original.amount is None
                or reversal.shares is None
                or reversal.amount is None
                or reversal.shares != -original.shares
                or reversal.amount != -original.amount
            ):
                raise ValueError("invalid reversal values")
            original_by_reversal_id[reversal.id] = original
            reversal_by_original_id[original.id] = reversal

        for transaction in transactions:
            if (
                transaction.status is TransactionStatus.REVERSED
                and transaction.id not in reversal_by_original_id
            ):
                raise ValueError("invalid reversal status")

        confirmed_leaves = []
        for reversal in reversals:
            if reversal.status is TransactionStatus.CONFIRMED:
                if reversal.id in reversal_by_original_id:
                    raise ValueError("invalid reversal status")
                confirmed_leaves.append(reversal)
            elif reversal.id not in reversal_by_original_id:
                raise ValueError("invalid reversal status")

        neutralized_event_ids = set()
        reached_reversal_ids = set()
        for leaf in confirmed_leaves:
            chain = [leaf]
            current = leaf
            local_reversal_ids = set()
            while current.transaction_type is TransactionType.REVERSAL:
                if current.id in local_reversal_ids:
                    raise ValueError("invalid reversal link")
                local_reversal_ids.add(current.id)
                reached_reversal_ids.add(current.id)
                original = original_by_reversal_id.get(current.id)
                if original is None:
                    raise ValueError("invalid reversal link")
                chain.append(original)
                current = original

            for index in range(0, len(chain) - 1, 2):
                pair = {chain[index].id, chain[index + 1].id}
                if neutralized_event_ids.intersection(pair):
                    raise ValueError("duplicate reversal link")
                neutralized_event_ids.update(pair)

        if reached_reversal_ids != {
            reversal.id for reversal in reversals
        }:
            raise ValueError("invalid reversal status")

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
