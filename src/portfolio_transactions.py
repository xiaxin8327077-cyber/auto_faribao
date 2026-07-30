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
        idempotency_key = self._nonempty_text(
            idempotency_key, "idempotency_key"
        )
        amount = self._positive_decimal(amount, "amount")
        fee_rate = self._fee_rate(fee_rate)

        with self.repository.database.transaction() as conn:
            self._reject_operation_audit_collision(
                idempotency_key, conn
            )
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
        idempotency_key = self._nonempty_text(
            idempotency_key, "idempotency_key"
        )
        shares = self._positive_decimal(shares, "shares")

        with self.repository.database.transaction() as conn:
            self._reject_operation_audit_collision(
                idempotency_key, conn
            )
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
                if not self._matching_cancel_retry(
                    transaction,
                    retry,
                    conn,
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
                "linked_status": (
                    TransactionStatus.CANCELLED.value if linked else ""
                ),
                "status": TransactionStatus.CANCELLED.value,
                "target_status": TransactionStatus.CANCELLED.value,
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
            group = self._reversal_business_group(transaction, conn)
            for member in group:
                if member.status is not TransactionStatus.CONFIRMED:
                    raise ValueError("transaction is not confirmed")
                if member.shares is None or member.amount is None:
                    raise ValueError(
                        "transaction cannot be reversed without shares and amount"
                    )
                if self._reversal_children(member.id, conn):
                    raise ValueError("transaction already has a reversal")

            before = {
                "targets": [
                    self._reversal_target_state(member, conn)
                    for member in group
                ]
            }
            reversals = []
            for index, member in enumerate(group):
                reversal = Transaction(
                    id=str(uuid4()),
                    product_id=member.product_id,
                    transaction_type=TransactionType.REVERSAL,
                    status=TransactionStatus.CONFIRMED,
                    trade_date=member.trade_date,
                    idempotency_key=(
                        idempotency_key
                        if index == 0
                        else f"linked:{reversals[0].id}"
                    ),
                    amount=-member.amount,
                    shares=-member.shares,
                    confirmation_nav=member.confirmation_nav,
                    confirmation_date=member.confirmation_date,
                    linked_transaction_id=member.id,
                    note=reason,
                    created_by=actor,
                )
                self.repository.create_transaction(reversal, conn)
                reversals.append(reversal)
            for member in group:
                self._mark_reversed(member.id, conn)

            affected_product_ids = {
                member.product_id for member in group
            }
            self._rebuild_positions(affected_product_ids, conn)
            target_states = [
                self._reversal_target_state(member, conn)
                for member in group
            ]
            after = {
                **request,
                "linked_reversed_at": (
                    target_states[1]["reversed_at"]
                    if len(target_states) == 2
                    else ""
                ),
                "linked_reversal_id": (
                    reversals[1].id if len(reversals) == 2 else ""
                ),
                "linked_status": (
                    target_states[1]["status"]
                    if len(target_states) == 2
                    else ""
                ),
                "linked_transaction_id": (
                    group[1].id if len(group) == 2 else ""
                ),
                "reversal_id": reversals[0].id,
                "reversals": [
                    self._reversal_audit_state(reversal)
                    for reversal in reversals
                ],
                "status": TransactionStatus.REVERSED.value,
                "target_reversed_at": target_states[0]["reversed_at"],
                "target_status": target_states[0]["status"],
                "targets": target_states,
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
            return reversals[0]

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
                include_before=True,
            )
            if retry is not None:
                before_retry, after_retry = retry
                adjustment = self.repository.get_transaction_by_idempotency(
                    idempotency_key, conn=conn
                )
                if not self._matching_adjustment_retry(
                    adjustment,
                    before_retry,
                    after_retry,
                    product_id,
                    effective_date,
                    reason,
                    actor,
                    conn,
                ):
                    raise ValueError(
                        "idempotency key conflicts with existing request"
                    )
                return adjustment
            self._reject_transaction_idempotency_collision(
                idempotency_key, conn
            )

            product = self.repository.require_product(product_id, conn=conn)
            current = self.projector._calculate(product_id, conn)
            difference = actual_shares - current.total_shares
            amount, cost_rule, unit_cost = self._adjustment_cost(
                product,
                current,
                difference,
                effective_date,
                conn,
            )
            adjustment = Transaction(
                id=str(uuid4()),
                product_id=product_id,
                transaction_type=TransactionType.HOLDING_ADJUSTMENT,
                status=TransactionStatus.CONFIRMED,
                trade_date=effective_date,
                idempotency_key=idempotency_key,
                amount=amount,
                shares=difference,
                confirmation_date=effective_date,
                confirmation_nav=unit_cost,
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
                    "amount": self._decimal_audit_text(adjustment.amount),
                    "cost_basis_rule": cost_rule,
                    "difference": self._decimal_audit_text(
                        adjustment.shares
                    ),
                    "unit_cost": (
                        self._decimal_audit_text(unit_cost)
                        if unit_cost is not None
                        else ""
                    ),
                },
                actor,
                conn,
            )
            return adjustment

    def _adjustment_cost(
        self,
        product,
        current,
        difference,
        effective_date,
        conn,
    ):
        if difference <= ZERO:
            return (
                ZERO,
                (
                    "zero_difference"
                    if difference == ZERO
                    else "average_cost_reduction"
                ),
                None,
            )
        if product.product_type is ProductType.CASH_MANAGEMENT:
            return difference, "cash_unit_price", ONE
        if current.total_shares > ZERO:
            if current.cost_basis <= ZERO:
                raise ValueError(
                    "positive adjustment requires a positive average cost"
                )
            unit_cost = current.cost_basis / current.total_shares
            return (
                difference * unit_cost,
                "preserve_average_cost",
                unit_cost,
            )
        quote = self.repository.get_quote(
            product.id,
            effective_date,
            conn=conn,
        )
        if quote is None or quote.unit_nav is None:
            raise ValueError(
                "positive adjustment from zero requires an exact quote"
            )
        try:
            unit_cost = self._quote_nav(quote)
        except ValueError as exc:
            raise ValueError(
                "positive adjustment from zero requires an exact quote"
            ) from exc
        return difference * unit_cost, "exact_quote", unit_cost

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
        *,
        include_before=False,
    ):
        row = conn.execute(
            """SELECT action, object_type, object_id,
                      before_json, after_json, source
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
        if include_before:
            try:
                before_payload = json.loads(row["before_json"])
            except (TypeError, ValueError):
                before_payload = None
            if not isinstance(before_payload, dict):
                raise ValueError(
                    "idempotency key conflicts with existing request"
                )
            return before_payload, payload
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

    def _reject_operation_audit_collision(self, idempotency_key, conn):
        if conn.execute(
            "SELECT 1 FROM audit_logs WHERE id = ?",
            (self._operation_audit_id(idempotency_key),),
        ).fetchone():
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

    def _reversal_children(self, transaction_id, conn):
        return [
            candidate
            for candidate in self.repository.list_transactions(conn=conn)
            if (
                candidate.transaction_type is TransactionType.REVERSAL
                and candidate.linked_transaction_id == transaction_id
            )
        ]

    def _reversal_business_group(self, transaction, conn):
        if transaction.transaction_type in {
            TransactionType.MANUAL_PURCHASE,
            TransactionType.MANUAL_REDEMPTION,
        }:
            linked = self._require_consistent_link(transaction, conn)
            return [transaction, linked] if linked is not None else [transaction]
        if transaction.transaction_type in {
            TransactionType.CASH_TRANSFER_OUT,
            TransactionType.CASH_TRANSFER_IN,
        }:
            return [
                transaction,
                self._manual_sibling_for_cash(transaction, conn),
            ]
        if transaction.transaction_type is not TransactionType.REVERSAL:
            return [transaction]
        try:
            chain = self._validated_reversal_chain(transaction, conn)
            sibling_root = self._business_sibling_for_root(chain[0], conn)
            if sibling_root is None:
                return [transaction]
            sibling = sibling_root
            for target_node in chain[1:]:
                children = self._reversal_children(sibling.id, conn)
                if len(children) != 1:
                    raise ValueError("inconsistent reversal group")
                sibling_child = children[0]
                self._require_exact_reversal_edge(sibling_child, sibling)
                if (
                    sibling_child.status is not target_node.status
                    or sibling_child.note != target_node.note
                    or sibling_child.created_by != target_node.created_by
                ):
                    raise ValueError("inconsistent reversal group")
                sibling = sibling_child
            return [transaction, sibling]
        except ValueError as exc:
            if str(exc) == "inconsistent reversal group":
                raise
            raise ValueError("inconsistent reversal group") from exc

    def _validated_reversal_chain(self, transaction, conn):
        chain = [transaction]
        current = transaction
        seen = set()
        while current.transaction_type is TransactionType.REVERSAL:
            if current.id in seen or not current.linked_transaction_id:
                raise ValueError("inconsistent reversal group")
            seen.add(current.id)
            parent = self.repository.get_transaction_by_id(
                current.linked_transaction_id, conn=conn
            )
            if parent is None:
                raise ValueError("inconsistent reversal group")
            children = self._reversal_children(parent.id, conn)
            if len(children) != 1 or children[0].id != current.id:
                raise ValueError("inconsistent reversal group")
            self._require_exact_reversal_edge(current, parent)
            if parent.status is not TransactionStatus.REVERSED:
                raise ValueError("inconsistent reversal group")
            chain.append(parent)
            current = parent
        chain.reverse()
        return chain

    @staticmethod
    def _require_exact_reversal_edge(child, parent):
        if (
            child.product_id != parent.product_id
            or child.shares is None
            or parent.shares is None
            or child.amount is None
            or parent.amount is None
            or child.shares != -parent.shares
            or child.amount != -parent.amount
        ):
            raise ValueError("inconsistent reversal group")

    def _business_sibling_for_root(self, root, conn):
        if root.transaction_type in {
            TransactionType.MANUAL_PURCHASE,
            TransactionType.MANUAL_REDEMPTION,
        }:
            return self._require_consistent_link(root, conn)
        if root.transaction_type in {
            TransactionType.CASH_TRANSFER_OUT,
            TransactionType.CASH_TRANSFER_IN,
        }:
            return self._manual_sibling_for_cash(root, conn)
        return None

    def _manual_sibling_for_cash(self, cash_transaction, conn):
        if not cash_transaction.linked_transaction_id:
            raise ValueError("inconsistent reversal group")
        manual = self.repository.get_transaction_by_id(
            cash_transaction.linked_transaction_id, conn=conn
        )
        if (
            manual is None
            or manual.transaction_type
            not in {
                TransactionType.MANUAL_PURCHASE,
                TransactionType.MANUAL_REDEMPTION,
            }
        ):
            raise ValueError("inconsistent reversal group")
        try:
            linked = self._require_consistent_link(manual, conn)
        except ValueError as exc:
            raise ValueError("inconsistent reversal group") from exc
        if linked is None or linked.id != cash_transaction.id:
            raise ValueError("inconsistent reversal group")
        return manual

    @staticmethod
    def _reversal_audit_state(reversal):
        return {
            "amount": PortfolioTransactionService._decimal_audit_text(
                reversal.amount
            ),
            "id": reversal.id,
            "original_id": reversal.linked_transaction_id,
            "product_id": reversal.product_id,
            "shares": PortfolioTransactionService._decimal_audit_text(
                reversal.shares
            ),
            "status": reversal.status.value,
        }

    @staticmethod
    def _reversal_target_state(transaction, conn):
        row = conn.execute(
            """SELECT status, reversed_at
               FROM transactions WHERE id = ?""",
            (transaction.id,),
        ).fetchone()
        return {
            "id": transaction.id,
            "product_id": transaction.product_id,
            "reversed_at": row["reversed_at"] or "",
            "status": row["status"],
        }

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
        audit_reversals = audit_payload.get("reversals")
        if (
            reversal is None
            or reversal.id != audit_payload.get("reversal_id")
            or original is None
            or original.status is not TransactionStatus.REVERSED
            or not isinstance(audit_reversals, list)
            or not audit_reversals
        ):
            return False
        try:
            parent_group = self._reversal_business_group(original, conn)
        except ValueError:
            return False
        if {
            member.id for member in parent_group
        } != {
            entry.get("original_id") for entry in audit_reversals
        }:
            return False
        for entry in audit_reversals:
            candidate = self.repository.get_transaction_by_id(
                entry.get("id", ""), conn=conn
            )
            parent = self.repository.get_transaction_by_id(
                entry.get("original_id", ""), conn=conn
            )
            if (
                candidate is None
                or parent is None
                or candidate.transaction_type is not TransactionType.REVERSAL
                or candidate.note != reason
                or candidate.created_by != actor
                or candidate.product_id != entry.get("product_id")
                or candidate.linked_transaction_id != parent.id
                or candidate.shares is None
                or candidate.amount is None
                or self._decimal_audit_text(candidate.shares)
                != entry.get("shares")
                or self._decimal_audit_text(candidate.amount)
                != entry.get("amount")
                or parent.status is not TransactionStatus.REVERSED
                or len(self._reversal_children(parent.id, conn)) != 1
            ):
                return False
            try:
                self._require_exact_reversal_edge(candidate, parent)
            except ValueError:
                return False
        return True

    def _matching_cancel_retry(self, transaction, audit_payload, conn):
        if (
            transaction is None
            or transaction.status is not TransactionStatus.CANCELLED
            or audit_payload.get("target_status")
            != TransactionStatus.CANCELLED.value
        ):
            return False
        try:
            linked = self._require_consistent_link(transaction, conn)
        except ValueError:
            return False
        linked_id = audit_payload.get("linked_transaction_id", "")
        if linked is None:
            return (
                not transaction.linked_transaction_id
                and linked_id == ""
                and audit_payload.get("linked_status", "") == ""
            )
        return (
            linked.id == linked_id
            and linked.status is TransactionStatus.CANCELLED
            and audit_payload.get("linked_status")
            == TransactionStatus.CANCELLED.value
        )

    def _matching_adjustment_retry(
        self,
        adjustment,
        before_payload,
        audit_payload,
        product_id,
        effective_date,
        reason,
        actor,
        conn,
    ):
        base_matches = (
            adjustment is not None
            and adjustment.id == audit_payload.get("adjustment_id")
            and adjustment.transaction_type
            is TransactionType.HOLDING_ADJUSTMENT
            and adjustment.status
            in {
                TransactionStatus.CONFIRMED,
                TransactionStatus.REVERSED,
            }
            and adjustment.product_id == product_id
            and adjustment.trade_date == effective_date
            and adjustment.note == reason
            and adjustment.created_by == actor
            and adjustment.shares is not None
            and adjustment.amount is not None
            and PortfolioTransactionService._decimal_audit_text(
                adjustment.shares
            )
            == audit_payload.get("difference")
            and PortfolioTransactionService._decimal_audit_text(
                adjustment.amount
            )
            == audit_payload.get("amount")
            and (
                (
                    audit_payload.get("unit_cost", "") == ""
                    and adjustment.confirmation_nav is None
                )
                or (
                    adjustment.confirmation_nav is not None
                    and PortfolioTransactionService._decimal_audit_text(
                        adjustment.confirmation_nav
                    )
                    == audit_payload.get("unit_cost")
                )
            )
        )
        if not base_matches:
            return False
        try:
            difference = Decimal(audit_payload["difference"])
            amount = Decimal(audit_payload["amount"])
            actual_shares = Decimal(audit_payload["actual_shares"])
            before_shares = Decimal(before_payload["total_shares"])
            unit_cost = (
                None
                if audit_payload.get("unit_cost", "") == ""
                else Decimal(audit_payload["unit_cost"])
            )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return False
        numbers = [difference, amount, actual_shares, before_shares]
        if unit_cost is not None:
            numbers.append(unit_cost)
        if (
            any(not number.is_finite() for number in numbers)
            or actual_shares < ZERO
            or before_shares < ZERO
            or actual_shares - before_shares != difference
        ):
            return False
        try:
            product = self.repository.require_product(product_id, conn=conn)
        except ValueError:
            return False
        rule = audit_payload.get("cost_basis_rule")
        if difference == ZERO:
            return (
                rule == "zero_difference"
                and amount == ZERO
                and unit_cost is None
            )
        if difference < ZERO:
            return (
                rule == "average_cost_reduction"
                and amount == ZERO
                and unit_cost is None
            )
        if product.product_type is ProductType.CASH_MANAGEMENT:
            return (
                rule == "cash_unit_price"
                and amount == difference
                and unit_cost == ONE
            )
        if (
            unit_cost is None
            or unit_cost <= ZERO
            or amount != difference * unit_cost
        ):
            return False
        if before_shares > ZERO:
            return rule == "preserve_average_cost"
        if rule != "exact_quote":
            return False
        quote = self.repository.get_quote(
            product_id,
            effective_date,
            conn=conn,
        )
        if quote is None or quote.unit_nav is None:
            return False
        try:
            return self._quote_nav(quote) == unit_cost
        except ValueError:
            return False

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
        reverse_cash_links = [
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
        if not transaction.linked_transaction_id:
            if reverse_cash_links:
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
            or len(reverse_cash_links) != 1
            or reverse_cash_links[0].id != linked.id
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
        if transaction.status in {
            *_PENDING_STATUSES,
            TransactionStatus.CANCELLED,
        }:
            return (
                product.product_type is not ProductType.CASH_MANAGEMENT
                and transaction.shares is None
                and transaction.fee_amount is None
                and transaction.confirmation_nav is None
                and transaction.confirmation_date is None
                and linked.confirmation_nav is None
                and linked.confirmation_date is None
            )
        if transaction.status not in {
            TransactionStatus.CONFIRMED,
            TransactionStatus.REVERSED,
        }:
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
        if transaction.status in {
            *_PENDING_STATUSES,
            TransactionStatus.CANCELLED,
        }:
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
        if transaction.status not in {
            TransactionStatus.CONFIRMED,
            TransactionStatus.REVERSED,
        }:
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
