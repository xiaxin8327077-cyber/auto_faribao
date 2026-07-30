from dataclasses import replace
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from src.portfolio_models import (
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)


ZERO = Decimal("0")
ONE = Decimal("1")
_PENDING_STATUSES = {
    TransactionStatus.PENDING_QUOTE,
    TransactionStatus.PENDING_CONFIRMATION,
}


class PortfolioTransactionService:
    def __init__(self, repository, projector):
        self.repository = repository
        self.projector = projector

    def record_purchase(
        self,
        product_id,
        amount,
        trade_date,
        idempotency_key,
        source_cash_product_id="",
        fee_rate=Decimal("0"),
        created_by="web",
    ) -> Transaction:
        amount = self._positive_decimal(amount, "amount")
        fee_rate = self._fee_rate(fee_rate)

        with self.repository.database.transaction() as conn:
            existing = self.repository.get_transaction_by_idempotency(
                idempotency_key, conn=conn
            )
            if existing is not None:
                return self._require_matching_purchase_request(
                    existing,
                    product_id=product_id,
                    amount=amount,
                    trade_date=trade_date,
                    source_cash_product_id=source_cash_product_id or "",
                    fee_rate=fee_rate,
                    created_by=created_by,
                    conn=conn,
                )

            product = self.repository.require_product(product_id, conn=conn)
            source = None
            if source_cash_product_id:
                source = self.repository.require_product(
                    source_cash_product_id, conn=conn
                )
                if source.product_type is not ProductType.CASH_MANAGEMENT:
                    raise ValueError("source must be cash_management")
                source_position = self.projector._calculate(source.id, conn)
                if source_position.available_shares < amount:
                    raise ValueError("insufficient available shares")

            status, nav = self._status_and_nav(product, trade_date, conn)
            fee_amount = amount * fee_rate if nav is not None else None
            shares = None
            if nav is not None:
                shares = (
                    amount
                    if product.product_type is ProductType.CASH_MANAGEMENT
                    else (amount - fee_amount) / nav
                )
                self._require_finite_positive(shares, "shares")

            purchase = Transaction(
                id=str(uuid4()),
                product_id=product.id,
                transaction_type=TransactionType.MANUAL_PURCHASE,
                status=status,
                trade_date=trade_date,
                idempotency_key=idempotency_key,
                amount=amount,
                shares=shares,
                fee_amount=fee_amount,
                fee_rate=fee_rate,
                confirmation_nav=nav,
                confirmation_date=trade_date if nav is not None else None,
                created_by=created_by,
            )
            affected_product_ids = {product.id}
            if source is not None:
                purchase = self._create_linked_pair(
                    purchase,
                    Transaction(
                        id=str(uuid4()),
                        product_id=source.id,
                        transaction_type=TransactionType.CASH_TRANSFER_OUT,
                        status=status,
                        trade_date=trade_date,
                        idempotency_key=f"linked:{purchase.id}",
                        amount=amount,
                        shares=amount,
                        confirmation_nav=ONE if nav is not None else None,
                        confirmation_date=trade_date if nav is not None else None,
                        linked_transaction_id=purchase.id,
                        created_by=created_by,
                    ),
                    conn,
                )
                affected_product_ids.add(source.id)
            else:
                self.repository.create_transaction(purchase, conn)

            self._rebuild_positions(affected_product_ids, conn)
            return purchase

    def record_redemption(
        self,
        product_id,
        shares,
        trade_date,
        idempotency_key,
        destination_cash_product_id="",
        created_by="web",
    ) -> Transaction:
        shares = self._positive_decimal(shares, "shares")

        with self.repository.database.transaction() as conn:
            existing = self.repository.get_transaction_by_idempotency(
                idempotency_key, conn=conn
            )
            if existing is not None:
                return self._require_matching_redemption_request(
                    existing,
                    product_id=product_id,
                    shares=shares,
                    trade_date=trade_date,
                    destination_cash_product_id=(
                        destination_cash_product_id or ""
                    ),
                    created_by=created_by,
                    conn=conn,
                )

            product = self.repository.require_product(product_id, conn=conn)
            destination = None
            if destination_cash_product_id:
                destination = self.repository.require_product(
                    destination_cash_product_id, conn=conn
                )
                if destination.product_type is not ProductType.CASH_MANAGEMENT:
                    raise ValueError("destination must be cash_management")

            position = self.projector._calculate(product.id, conn)
            if position.available_shares < shares:
                raise ValueError("insufficient available shares")

            status, nav = self._status_and_nav(product, trade_date, conn)
            amount = shares * nav if nav is not None else None
            if amount is not None:
                self._require_finite_positive(amount, "amount")
            redemption = Transaction(
                id=str(uuid4()),
                product_id=product.id,
                transaction_type=TransactionType.MANUAL_REDEMPTION,
                status=status,
                trade_date=trade_date,
                idempotency_key=idempotency_key,
                amount=amount,
                shares=shares,
                confirmation_nav=nav,
                confirmation_date=trade_date if nav is not None else None,
                created_by=created_by,
            )
            affected_product_ids = {product.id}
            if destination is not None:
                redemption = self._create_linked_pair(
                    redemption,
                    Transaction(
                        id=str(uuid4()),
                        product_id=destination.id,
                        transaction_type=TransactionType.CASH_TRANSFER_IN,
                        status=status,
                        trade_date=trade_date,
                        idempotency_key=f"linked:{redemption.id}",
                        amount=amount,
                        shares=amount,
                        confirmation_nav=ONE if nav is not None else None,
                        confirmation_date=trade_date if nav is not None else None,
                        linked_transaction_id=redemption.id,
                        created_by=created_by,
                    ),
                    conn,
                )
                affected_product_ids.add(destination.id)
            else:
                self.repository.create_transaction(redemption, conn)

            self._rebuild_positions(affected_product_ids, conn)
            return redemption

    def confirm_pending(self, transaction_id, quote) -> Transaction:
        with self.repository.database.transaction() as conn:
            transaction = self.repository.get_transaction_by_id(
                transaction_id, conn=conn
            )
            if transaction is None:
                raise ValueError("transaction not found")
            if transaction.transaction_type not in {
                TransactionType.MANUAL_PURCHASE,
                TransactionType.MANUAL_REDEMPTION,
            }:
                raise ValueError("unsupported pending transaction type")

            product = self.repository.require_product(
                transaction.product_id, conn=conn
            )
            if transaction.status is TransactionStatus.CONFIRMED:
                self._require_matching_confirmation_quote(
                    transaction, product, quote, conn
                )
                return transaction
            if transaction.status not in _PENDING_STATUSES:
                raise ValueError("transaction is not pending")
            if quote.product_code != product.code:
                raise ValueError("quote product does not match")
            if quote.quote_date != transaction.trade_date:
                raise ValueError("quote date does not match")
            nav = self._quote_nav(quote)

            linked = self._require_consistent_link(transaction, conn)
            if transaction.transaction_type is TransactionType.MANUAL_PURCHASE:
                amount = self._positive_decimal(transaction.amount, "amount")
                fee_rate = self._fee_rate(transaction.fee_rate or ZERO)
                fee_amount = amount * fee_rate
                shares = (amount - fee_amount) / nav
                self._require_finite_positive(shares, "shares")
                confirmed = replace(
                    transaction,
                    status=TransactionStatus.CONFIRMED,
                    shares=shares,
                    fee_amount=fee_amount,
                    confirmation_nav=nav,
                    confirmation_date=quote.quote_date,
                )
                linked_amount = amount
            else:
                shares = self._positive_decimal(transaction.shares, "shares")
                amount = shares * nav
                self._require_finite_positive(amount, "amount")
                confirmed = replace(
                    transaction,
                    status=TransactionStatus.CONFIRMED,
                    amount=amount,
                    confirmation_nav=nav,
                    confirmation_date=quote.quote_date,
                )
                linked_amount = amount

            self.repository.update_pending_transaction(confirmed, conn)
            affected_product_ids = {confirmed.product_id}
            if linked is not None:
                confirmed_linked = replace(
                    linked,
                    status=TransactionStatus.CONFIRMED,
                    amount=linked_amount,
                    shares=linked_amount,
                    confirmation_nav=ONE,
                    confirmation_date=quote.quote_date,
                )
                self.repository.update_pending_transaction(
                    confirmed_linked, conn
                )
                affected_product_ids.add(confirmed_linked.product_id)

            self._rebuild_positions(affected_product_ids, conn)
            return confirmed

    def _require_matching_purchase_request(
        self,
        existing,
        *,
        product_id,
        amount,
        trade_date,
        source_cash_product_id,
        fee_rate,
        created_by,
        conn,
    ):
        if (
            existing.transaction_type is not TransactionType.MANUAL_PURCHASE
            or existing.product_id != product_id
            or existing.amount != amount
            or existing.trade_date != trade_date
            or (existing.fee_rate or ZERO) != fee_rate
            or existing.created_by != created_by
            or self._linked_product_id(existing, conn)
            != source_cash_product_id
        ):
            raise ValueError(
                "idempotency key conflicts with existing request"
            )
        return existing

    def _require_matching_redemption_request(
        self,
        existing,
        *,
        product_id,
        shares,
        trade_date,
        destination_cash_product_id,
        created_by,
        conn,
    ):
        if (
            existing.transaction_type is not TransactionType.MANUAL_REDEMPTION
            or existing.product_id != product_id
            or existing.shares != shares
            or existing.trade_date != trade_date
            or existing.created_by != created_by
            or self._linked_product_id(existing, conn)
            != destination_cash_product_id
        ):
            raise ValueError(
                "idempotency key conflicts with existing request"
            )
        return existing

    def _linked_product_id(self, transaction, conn):
        if not transaction.linked_transaction_id:
            return ""
        try:
            linked = self._require_consistent_link(transaction, conn)
        except ValueError as exc:
            raise ValueError(
                "idempotency key conflicts with existing request"
            ) from exc
        return linked.product_id

    def _require_matching_confirmation_quote(
        self, transaction, product, quote, conn
    ):
        try:
            if quote.product_code != product.code:
                raise ValueError("quote product does not match")
            if quote.quote_date != transaction.trade_date:
                raise ValueError("quote date does not match")
            nav = self._quote_nav(quote)
        except (AttributeError, ValueError) as exc:
            raise ValueError(
                "confirmation quote conflicts with original confirmation"
            ) from exc
        if (
            transaction.confirmation_nav != nav
            or transaction.confirmation_date != quote.quote_date
        ):
            raise ValueError(
                "confirmation quote conflicts with original confirmation"
            )
        self._require_consistent_link(transaction, conn)

    def _status_and_nav(self, product, trade_date, conn):
        if product.product_type is ProductType.CASH_MANAGEMENT:
            return TransactionStatus.CONFIRMED, ONE
        quote = self.repository.get_quote(product.id, trade_date, conn=conn)
        if quote is None or quote.unit_nav is None:
            return TransactionStatus.PENDING_QUOTE, None
        return TransactionStatus.CONFIRMED, self._quote_nav(quote)

    def _create_linked_pair(self, primary, linked, conn):
        initial_status = (
            primary.status
            if primary.status in _PENDING_STATUSES
            else TransactionStatus.PENDING_CONFIRMATION
        )
        initial = replace(primary, status=initial_status)
        self.repository.create_transaction(initial, conn)
        self.repository.create_transaction(linked, conn)
        primary = replace(primary, linked_transaction_id=linked.id)
        self.repository.update_pending_transaction(primary, conn)
        return primary

    def _require_consistent_link(self, transaction, conn):
        if transaction.transaction_type not in {
            TransactionType.MANUAL_PURCHASE,
            TransactionType.MANUAL_REDEMPTION,
        }:
            raise ValueError("inconsistent linked transaction")
        if not transaction.linked_transaction_id:
            orphaned_cash_links = [
                candidate
                for candidate in self.repository.list_transactions(conn=conn)
                if (
                    candidate.linked_transaction_id == transaction.id
                    and candidate.transaction_type
                    in {
                        TransactionType.CASH_TRANSFER_OUT,
                        TransactionType.CASH_TRANSFER_IN,
                    }
                )
            ]
            if orphaned_cash_links:
                raise ValueError("inconsistent linked transaction")
            return None
        linked = self.repository.get_transaction_by_id(
            transaction.linked_transaction_id, conn=conn
        )
        expected_type = (
            TransactionType.CASH_TRANSFER_OUT
            if transaction.transaction_type is TransactionType.MANUAL_PURCHASE
            else TransactionType.CASH_TRANSFER_IN
        )
        if (
            linked is None
            or linked.linked_transaction_id != transaction.id
            or linked.transaction_type is not expected_type
            or linked.status is not transaction.status
            or linked.trade_date != transaction.trade_date
            or linked.created_by != transaction.created_by
            or linked.fee_amount is not None
            or linked.fee_rate is not None
        ):
            raise ValueError("inconsistent linked transaction")
        linked_product = self.repository.require_product(
            linked.product_id, conn=conn
        )
        if linked_product.product_type is not ProductType.CASH_MANAGEMENT:
            raise ValueError("inconsistent linked transaction")
        product = self.repository.require_product(
            transaction.product_id, conn=conn
        )
        if transaction.transaction_type is TransactionType.MANUAL_PURCHASE:
            consistent = self._purchase_link_is_consistent(
                transaction, linked, product
            )
        else:
            consistent = self._redemption_link_is_consistent(
                transaction, linked, product
            )
        if not consistent:
            raise ValueError("inconsistent linked transaction")
        return linked

    @classmethod
    def _purchase_link_is_consistent(cls, transaction, linked, product):
        if (
            not cls._is_finite_positive(transaction.amount)
            or not cls._is_valid_fee_rate(transaction.fee_rate)
            or linked.amount != transaction.amount
            or linked.shares != transaction.amount
        ):
            return False
        if transaction.status in _PENDING_STATUSES:
            return (
                product.product_type is not ProductType.CASH_MANAGEMENT
                and transaction.shares is None
                and transaction.fee_amount is None
                and transaction.confirmation_nav is None
                and transaction.confirmation_date is None
                and linked.confirmation_nav is None
                and linked.confirmation_date is None
            )
        if transaction.status is not TransactionStatus.CONFIRMED:
            return False
        if (
            not cls._is_finite_positive(transaction.confirmation_nav)
            or transaction.confirmation_date != transaction.trade_date
            or transaction.fee_amount
            != transaction.amount * transaction.fee_rate
            or linked.confirmation_nav != ONE
            or linked.confirmation_date != transaction.confirmation_date
            or (
                product.product_type is ProductType.CASH_MANAGEMENT
                and transaction.confirmation_nav != ONE
            )
        ):
            return False
        expected_shares = (
            transaction.amount
            if product.product_type is ProductType.CASH_MANAGEMENT
            else (
                transaction.amount - transaction.fee_amount
            ) / transaction.confirmation_nav
        )
        return (
            cls._is_finite_positive(transaction.shares)
            and transaction.shares == expected_shares
        )

    @classmethod
    def _redemption_link_is_consistent(cls, transaction, linked, product):
        if (
            not cls._is_finite_positive(transaction.shares)
            or transaction.fee_amount is not None
            or transaction.fee_rate is not None
        ):
            return False
        if transaction.status in _PENDING_STATUSES:
            return (
                product.product_type is not ProductType.CASH_MANAGEMENT
                and transaction.amount is None
                and transaction.confirmation_nav is None
                and transaction.confirmation_date is None
                and linked.amount is None
                and linked.shares is None
                and linked.confirmation_nav is None
                and linked.confirmation_date is None
            )
        if transaction.status is not TransactionStatus.CONFIRMED:
            return False
        if (
            not cls._is_finite_positive(transaction.amount)
            or not cls._is_finite_positive(transaction.confirmation_nav)
            or transaction.confirmation_date != transaction.trade_date
            or transaction.amount
            != transaction.shares * transaction.confirmation_nav
            or linked.amount != transaction.amount
            or linked.shares != transaction.amount
            or linked.confirmation_nav != ONE
            or linked.confirmation_date != transaction.confirmation_date
        ):
            return False
        if product.product_type is ProductType.CASH_MANAGEMENT:
            return transaction.confirmation_nav == ONE
        return True

    @staticmethod
    def _is_finite_positive(value):
        return (
            isinstance(value, Decimal)
            and value.is_finite()
            and value > ZERO
        )

    @staticmethod
    def _is_valid_fee_rate(value):
        return (
            isinstance(value, Decimal)
            and value.is_finite()
            and ZERO <= value < ONE
        )

    def _rebuild_positions(self, product_ids, conn):
        positions = [
            self.projector._calculate(product_id, conn)
            for product_id in sorted(product_ids)
        ]
        for position in positions:
            self.repository.replace_position(position, conn)

    @classmethod
    def _positive_decimal(cls, value, name):
        number = cls._decimal(value, name)
        cls._require_finite_positive(number, name)
        return number

    @staticmethod
    def _decimal(value, name):
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be positive and finite") from exc

    @staticmethod
    def _require_finite_positive(value, name):
        if not value.is_finite() or value <= ZERO:
            raise ValueError(f"{name} must be positive and finite")

    @classmethod
    def _fee_rate(cls, value):
        try:
            rate = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "fee_rate must be finite and between 0 and 1"
            ) from exc
        if not rate.is_finite() or rate < ZERO or rate >= ONE:
            raise ValueError("fee_rate must be finite and between 0 and 1")
        return rate

    @classmethod
    def _quote_nav(cls, quote):
        try:
            nav = Decimal(str(quote.unit_nav))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "quote must contain a finite positive unit NAV"
            ) from exc
        if not nav.is_finite() or nav <= ZERO:
            raise ValueError("quote must contain a finite positive unit NAV")
        return nav
