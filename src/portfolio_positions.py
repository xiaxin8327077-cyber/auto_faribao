from decimal import Decimal

from src.portfolio_models import (
    Position,
    ProductType,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_wallet_allocation import (
    redemption_settlement_purchase_is_start_of_day,
    wallet_pending_purchase_allocations,
)


ZERO = Decimal("0")

SHARE_DIRECTION = {
    TransactionType.OPENING_POSITION: Decimal("1"),
    TransactionType.MANUAL_PURCHASE: Decimal("1"),
    TransactionType.SIP_PURCHASE: Decimal("1"),
    TransactionType.CASH_TRANSFER_IN: Decimal("1"),
    TransactionType.INCOME_ACCRUAL: Decimal("1"),
    TransactionType.LATEST_PROFIT_ADJUSTMENT: Decimal("1"),
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
    TransactionType.LATEST_PROFIT_ADJUSTMENT,
}
_AVERAGE_COST_OUTFLOW_TYPES = {
    TransactionType.MANUAL_REDEMPTION,
    TransactionType.CASH_TRANSFER_OUT,
}
_WALLET_PLUS_PROVIDER = "wallet_plus"


def pending_purchase_is_in_current_position(
    product,
    transaction,
    *,
    use_confirmation_date=False,
):
    return (
        not use_confirmation_date
        and product.provider == _WALLET_PLUS_PROVIDER
        and product.product_type is ProductType.CASH_MANAGEMENT
        and transaction.status in _PENDING_STATUSES
        and transaction.transaction_type is TransactionType.MANUAL_PURCHASE
        and (transaction.shares or ZERO) > ZERO
    )


def wallet_purchase_is_unconfirmed_liquidity(
    product,
    transaction,
    *,
    as_of,
    use_confirmation_date,
):
    return (
        use_confirmation_date
        and as_of is not None
        and product.provider == _WALLET_PLUS_PROVIDER
        and product.product_type is ProductType.CASH_MANAGEMENT
        and transaction.transaction_type is TransactionType.MANUAL_PURCHASE
        and transaction.status in (_APPLIED_STATUSES | _PENDING_STATUSES)
        and transaction.trade_date <= as_of
        and (
            transaction.confirmation_date is None
            or transaction.confirmation_date > as_of
        )
        and (transaction.shares or ZERO) > ZERO
    )


def wallet_purchase_has_future_confirmation(product, transaction):
    return (
        product.provider == _WALLET_PLUS_PROVIDER
        and product.product_type is ProductType.CASH_MANAGEMENT
        and transaction.transaction_type is TransactionType.MANUAL_PURCHASE
        and transaction.status in _APPLIED_STATUSES
        and transaction.confirmation_date is not None
        and transaction.confirmation_date > transaction.trade_date
        and (transaction.shares or ZERO) > ZERO
    )


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
        unconfirmed_wallet_liquidity = ZERO
        product = self.repository.require_product(product_id, conn=conn)
        transactions = sorted(
            self.repository.list_transactions(
                product_id=product_id,
                conn=conn,
            ),
            key=lambda transaction: (
                transaction.trade_date,
                0
                if redemption_settlement_purchase_is_start_of_day(
                    product,
                    transaction,
                )
                else (
                    1
                    if (
                        pending_purchase_is_in_current_position(
                            product,
                            transaction,
                        )
                        or wallet_purchase_has_future_confirmation(
                            product,
                            transaction,
                        )
                    )
                    else 2
                ),
                transaction.created_at,
                transaction.id,
            ),
        )
        neutralized_event_ids = self._linked_reversal_pair_ids(transactions)
        wallet_allocations = wallet_pending_purchase_allocations(
            product,
            transactions,
            include_confirmed_purchases=use_confirmation_date,
            neutralized_event_ids=neutralized_event_ids,
        )

        for transaction in transactions:
            if transaction.id in neutralized_event_ids:
                continue
            shares = transaction.shares or ZERO
            original_shares = shares
            if wallet_purchase_is_unconfirmed_liquidity(
                product,
                transaction,
                as_of=as_of,
                use_confirmation_date=use_confirmation_date,
            ):
                # 待确认资金可转出，但确认前不计入生息份额。
                unconfirmed_wallet_liquidity += shares
                continue
            effective_date = (
                transaction.confirmation_date or transaction.trade_date
                if use_confirmation_date
                else transaction.trade_date
            )
            if as_of is not None and effective_date > as_of:
                continue
            if (
                transaction.status in _PENDING_STATUSES
                and transaction.transaction_type in _LOCKING_TYPES
                and shares > ZERO
            ):
                locked_shares += shares
                continue
            realtime_wallet_purchase = pending_purchase_is_in_current_position(
                product,
                transaction,
                use_confirmation_date=use_confirmation_date,
            )
            if (
                transaction.status not in _APPLIED_STATUSES
                and not realtime_wallet_purchase
            ):
                continue
            if transaction.transaction_type in {
                TransactionType.CASH_DIVIDEND,
                TransactionType.PROFIT_ADJUSTMENT,
            }:
                continue
            # 净值产品的最新收益校准 shares=0，仅改展示；现金校准有非零份额。
            if (
                transaction.transaction_type
                is TransactionType.LATEST_PROFIT_ADJUSTMENT
                and shares == ZERO
            ):
                continue

            if use_confirmation_date:
                if transaction.id in wallet_allocations.confirmed_shares_by_purchase:
                    shares = wallet_allocations.confirmed_shares_by_purchase[
                        transaction.id
                    ]
                elif transaction.transaction_type in _AVERAGE_COST_OUTFLOW_TYPES:
                    shares -= wallet_allocations.allocated_by_outflow.get(
                        transaction.id,
                        ZERO,
                    )
                elif transaction.transaction_type is TransactionType.CASH_TRANSFER_IN:
                    shares -= wallet_allocations.restored_by_inflow.get(
                        transaction.id,
                        ZERO,
                    )
                if transaction.transaction_type in _AVERAGE_COST_OUTFLOW_TYPES:
                    unconfirmed_wallet_liquidity -= (
                        wallet_allocations.allocated_by_outflow.get(
                            transaction.id,
                            ZERO,
                        )
                    )
                elif transaction.transaction_type is TransactionType.CASH_TRANSFER_IN:
                    unconfirmed_wallet_liquidity += (
                        wallet_allocations.restored_by_inflow.get(
                            transaction.id,
                            ZERO,
                        )
                    )

            share_delta = (
                shares * SHARE_DIRECTION[transaction.transaction_type]
            )
            next_total = total_shares + share_delta
            if next_total < ZERO:
                shortfall = -next_total
                can_use_unconfirmed_wallet_liquidity = (
                    use_confirmation_date
                    and product.provider == _WALLET_PLUS_PROVIDER
                    and product.product_type is ProductType.CASH_MANAGEMENT
                    and transaction.transaction_type
                    is TransactionType.HOLDING_ADJUSTMENT
                    and share_delta < ZERO
                    and unconfirmed_wallet_liquidity >= shortfall
                )
                if not can_use_unconfirmed_wallet_liquidity:
                    raise ValueError("negative portfolio shares")
                unconfirmed_wallet_liquidity -= shortfall
                share_delta = -total_shares
                next_total = ZERO

            amount = transaction.amount or ZERO
            if use_confirmation_date and original_shares > ZERO:
                amount *= shares / original_shares
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
