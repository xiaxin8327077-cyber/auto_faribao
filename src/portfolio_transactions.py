from dataclasses import replace
from decimal import Decimal, InvalidOperation
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

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

    def cancel_pending(
        self,
        transaction_id,
        idempotency_key,
        actor="web",
    ) -> Transaction:
        idempotency_key = self._nonempty_text(
            idempotency_key, "idempotency_key"
        )
        actor = self._nonempty_text(actor, "actor")
        request = {
            "actor": actor,
            "transaction_id": transaction_id,
        }
        with self.repository.database.transaction() as conn:
            retry = self._operation_retry(
                idempotency_key,
                "cancel_pending",
                "transaction",
                transaction_id,
                request,
                conn,
            )
            if retry is not None:
                transaction = self.repository.get_transaction_by_id(
                    transaction_id, conn=conn
                )
                if (
                    transaction is None
                    or transaction.status is not TransactionStatus.CANCELLED
                ):
                    raise ValueError(
                        "idempotency key conflicts with existing request"
                    )
                return transaction
            self._reject_transaction_idempotency_collision(
                idempotency_key, conn
            )

            transaction = self.repository.get_transaction_by_id(
                transaction_id, conn=conn
            )
            if transaction is None:
                raise ValueError("transaction not found")
            if transaction.status not in _PENDING_STATUSES:
                raise ValueError("confirmed transaction cannot be cancelled")

            linked = self._require_consistent_link(transaction, conn)
            before = self._transaction_audit_state(transaction, linked)
            cancelled = replace(
                transaction,
                status=TransactionStatus.CANCELLED,
            )
            self.repository.update_pending_transaction(cancelled, conn)
            affected_product_ids = {cancelled.product_id}
            if linked is not None:
                self.repository.update_pending_transaction(
                    replace(linked, status=TransactionStatus.CANCELLED),
                    conn,
                )
                affected_product_ids.add(linked.product_id)

            self._rebuild_positions(affected_product_ids, conn)
            after = {
                **request,
                "status": TransactionStatus.CANCELLED.value,
                "linked_transaction_id": linked.id if linked else "",
            }
            self._append_operation_audit(
                idempotency_key,
                "cancel_pending",
                "transaction",
                transaction.id,
                before,
                after,
                actor,
                conn,
            )
            return cancelled

    def reverse_confirmed(
        self,
        transaction_id,
        reason,
        idempotency_key,
        actor="web",
    ) -> Transaction:
        reason = self._nonempty_text(reason, "reason")
        idempotency_key = self._nonempty_text(
            idempotency_key, "idempotency_key"
        )
        actor = self._nonempty_text(actor, "actor")
        request = {
            "actor": actor,
            "reason": reason,
            "transaction_id": transaction_id,
        }
        with self.repository.database.transaction() as conn:
            retry = self._operation_retry(
                idempotency_key,
                "reverse_confirmed",
                "transaction",
                transaction_id,
                request,
                conn,
            )
            if retry is not None:
                reversal = self.repository.get_transaction_by_idempotency(
                    idempotency_key, conn=conn
                )
                if not self._matching_reversal_retry(
                    reversal,
                    retry,
                    transaction_id,
                    reason,
                    actor,
                    conn,
                ):
                    raise ValueError(
                        "idempotency key conflicts with existing request"
                    )
                return reversal
            self._reject_transaction_idempotency_collision(
                idempotency_key, conn
            )

            transaction = self.repository.get_transaction_by_id(
                transaction_id, conn=conn
            )
            if transaction is None:
                raise ValueError("transaction not found")
            if transaction.status is not TransactionStatus.CONFIRMED:
                raise ValueError("transaction is not confirmed")
            if self._reversal_for(transaction.id, conn) is not None:
                raise ValueError("transaction already has a reversal")
            if transaction.shares is None or transaction.amount is None:
                raise ValueError(
                    "transaction cannot be reversed without shares and amount"
                )

            linked = (
                self._require_consistent_link(transaction, conn)
                if transaction.transaction_type
                in {
                    TransactionType.MANUAL_PURCHASE,
                    TransactionType.MANUAL_REDEMPTION,
                }
                else None
            )
            before = self._transaction_audit_state(transaction, linked)
            reversal = Transaction(
                id=str(uuid4()),
                product_id=transaction.product_id,
                transaction_type=TransactionType.REVERSAL,
                status=TransactionStatus.CONFIRMED,
                trade_date=transaction.trade_date,
                idempotency_key=idempotency_key,
                amount=-transaction.amount,
                shares=-transaction.shares,
                confirmation_nav=transaction.confirmation_nav,
                confirmation_date=transaction.confirmation_date,
                linked_transaction_id=transaction.id,
                note=reason,
                created_by=actor,
            )
            self._mark_reversed(transaction.id, conn)
            self.repository.create_transaction(reversal, conn)
            affected_product_ids = {transaction.product_id}
            linked_reversal = None
            if linked is not None:
                if linked.shares is None or linked.amount is None:
                    raise ValueError(
                        "transaction cannot be reversed without shares and amount"
                    )
                if self._reversal_for(linked.id, conn) is not None:
                    raise ValueError("transaction already has a reversal")
                linked_reversal = Transaction(
                    id=str(uuid4()),
                    product_id=linked.product_id,
                    transaction_type=TransactionType.REVERSAL,
                    status=TransactionStatus.CONFIRMED,
                    trade_date=linked.trade_date,
                    idempotency_key=f"linked:{reversal.id}",
                    amount=-linked.amount,
                    shares=-linked.shares,
                    confirmation_nav=linked.confirmation_nav,
                    confirmation_date=linked.confirmation_date,
                    linked_transaction_id=linked.id,
                    note=reason,
                    created_by=actor,
                )
                self._mark_reversed(linked.id, conn)
                self.repository.create_transaction(linked_reversal, conn)
                affected_product_ids.add(linked.product_id)

            self._rebuild_positions(affected_product_ids, conn)
            after = {
                **request,
                "linked_reversal_id": (
                    linked_reversal.id if linked_reversal else ""
                ),
                "reversal_id": reversal.id,
                "status": TransactionStatus.REVERSED.value,
            }
            self._append_operation_audit(
                idempotency_key,
                "reverse_confirmed",
                "transaction",
                transaction.id,
                before,
                after,
                actor,
                conn,
            )
            return reversal

    def adjust_holding(
        self,
        product_id,
        actual_shares,
        effective_date,
        reason,
        idempotency_key,
        actor="web",
    ) -> Transaction:
        try:
            actual_shares = Decimal(str(actual_shares))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(
                "actual_shares must be non-negative and finite"
            ) from exc
        if not actual_shares.is_finite() or actual_shares < ZERO:
            raise ValueError("actual_shares must be non-negative and finite")
        reason = self._nonempty_text(reason, "reason")
        idempotency_key = self._nonempty_text(
            idempotency_key, "idempotency_key"
        )
        actor = self._nonempty_text(actor, "actor")
        request = {
            "actor": actor,
            "actual_shares": self._decimal_audit_text(actual_shares),
            "effective_date": effective_date.isoformat(),
            "product_id": product_id,
            "reason": reason,
        }

        with self.repository.database.transaction() as conn:
            retry = self._operation_retry(
                idempotency_key,
                "adjust_holding",
                "product",
                product_id,
                request,
                conn,
            )
            if retry is not None:
                adjustment = self.repository.get_transaction_by_idempotency(
                    idempotency_key, conn=conn
                )
                if not self._matching_adjustment_retry(
                    adjustment,
                    retry,
                    product_id,
                    effective_date,
                    reason,
                    actor,
                ):
                    raise ValueError(
                        "idempotency key conflicts with existing request"
                    )
                return adjustment
            self._reject_transaction_idempotency_collision(
                idempotency_key, conn
            )

            self.repository.require_product(product_id, conn=conn)
            current = self.projector._calculate(product_id, conn)
            adjustment = Transaction(
                id=str(uuid4()),
                product_id=product_id,
                transaction_type=TransactionType.HOLDING_ADJUSTMENT,
                status=TransactionStatus.CONFIRMED,
                trade_date=effective_date,
                idempotency_key=idempotency_key,
                amount=ZERO,
                shares=actual_shares - current.total_shares,
                confirmation_date=effective_date,
                note=reason,
                created_by=actor,
            )
            self.repository.create_transaction(adjustment, conn)
            self._rebuild_positions({product_id}, conn)
            self._append_operation_audit(
                idempotency_key,
                "adjust_holding",
                "product",
                product_id,
                {
                    "product_id": product_id,
                    "total_shares": self._decimal_audit_text(
                        current.total_shares
                    ),
                },
                {
                    **request,
                    "adjustment_id": adjustment.id,
                    "difference": self._decimal_audit_text(
                        adjustment.shares
                    ),
                },
                actor,
                conn,
            )
            return adjustment

    @staticmethod
    def _nonempty_text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must not be empty")
        return value.strip()

    @staticmethod
    def _decimal_audit_text(value):
        return format(value.normalize(), "f")

    @staticmethod
    def _operation_audit_id(idempotency_key):
        return str(
            uuid5(
                NAMESPACE_URL,
                f"portfolio-operation:{idempotency_key}",
            )
        )

    def _operation_retry(
        self,
        idempotency_key,
        action,
        object_type,
        object_id,
        request,
        conn,
    ):
        row = conn.execute(
            """SELECT action, object_type, object_id, after_json, source
               FROM audit_logs WHERE id = ?""",
            (self._operation_audit_id(idempotency_key),),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["after_json"])
        except (TypeError, ValueError):
            payload = None
        if (
            row["action"] != action
            or row["object_type"] != object_type
            or row["object_id"] != object_id
            or row["source"] != request["actor"]
            or not isinstance(payload, dict)
            or any(payload.get(key) != value for key, value in request.items())
        ):
            raise ValueError(
                "idempotency key conflicts with existing request"
            )
        return payload

    def _reject_transaction_idempotency_collision(
        self, idempotency_key, conn
    ):
        if self.repository.get_transaction_by_idempotency(
            idempotency_key, conn=conn
        ) is not None:
            raise ValueError(
                "idempotency key conflicts with existing request"
            )

    def _append_operation_audit(
        self,
        idempotency_key,
        action,
        object_type,
        object_id,
        before,
        after,
        actor,
        conn,
    ):
        self.repository.append_audit(
            self._operation_audit_id(idempotency_key),
            action,
            object_type,
            object_id,
            before_json=json.dumps(
                before,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            after_json=json.dumps(
                after,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            result="success",
            source=actor,
            conn=conn,
        )

    @staticmethod
    def _transaction_audit_state(transaction, linked):
        return {
            "linked_status": linked.status.value if linked else "",
            "linked_transaction_id": linked.id if linked else "",
            "status": transaction.status.value,
            "transaction_id": transaction.id,
        }

    @staticmethod
    def _mark_reversed(transaction_id, conn):
        conn.execute(
            """UPDATE transactions
               SET status = 'reversed', reversed_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (transaction_id,),
        )

    def _reversal_for(self, transaction_id, conn):
        return next(
            (
                candidate
                for candidate in self.repository.list_transactions(conn=conn)
                if (
                    candidate.transaction_type is TransactionType.REVERSAL
                    and candidate.linked_transaction_id == transaction_id
                )
            ),
            None,
        )

    def _matching_reversal_retry(
        self,
        reversal,
        audit_payload,
        transaction_id,
        reason,
        actor,
        conn,
    ):
        original = self.repository.get_transaction_by_id(
            transaction_id, conn=conn
        )
        return (
            reversal is not None
            and reversal.id == audit_payload.get("reversal_id")
            and reversal.transaction_type is TransactionType.REVERSAL
            and reversal.linked_transaction_id == transaction_id
            and reversal.note == reason
            and reversal.created_by == actor
            and original is not None
            and original.status is TransactionStatus.REVERSED
            and original.shares is not None
            and original.amount is not None
            and reversal.shares == -original.shares
            and reversal.amount == -original.amount
        )

    @staticmethod
    def _matching_adjustment_retry(
        adjustment,
        audit_payload,
        product_id,
        effective_date,
        reason,
        actor,
    ):
        return (
            adjustment is not None
            and adjustment.id == audit_payload.get("adjustment_id")
            and adjustment.transaction_type
            is TransactionType.HOLDING_ADJUSTMENT
            and adjustment.status is TransactionStatus.CONFIRMED
            and adjustment.product_id == product_id
            and adjustment.trade_date == effective_date
            and adjustment.note == reason
            and adjustment.created_by == actor
        )

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
