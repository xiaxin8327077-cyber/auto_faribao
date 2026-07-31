from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import sqlite3
from uuid import NAMESPACE_URL, uuid5

from src.portfolio_models import (
    MarketQuote,
    Position,
    Product,
    ProductStatus,
    ProductType,
    SipPlan,
    SipPlanStatus,
    Transaction,
    TransactionStatus,
    TransactionType,
    optional_decimal_text,
)


_PRODUCT_COLUMNS = """
    id, provider, code, name, product_type, status, registration_code, currency,
    metadata_json
"""
_TRANSACTION_COLUMNS = """
    id, product_id, transaction_type, status, trade_date, confirmation_date,
    amount, shares, fee_amount, fee_rate, confirmation_nav,
    linked_transaction_id, plan_id, idempotency_key, note, created_by,
    trade_time, confirmed_at, settlement_date
"""
_POSITION_COLUMNS = """
    product_id, available_shares, locked_shares, total_shares, cost_basis
"""
_QUOTE_COLUMNS = """
    p.code AS product_code, q.quote_date, q.unit_nav, q.cumulative_nav,
    q.income_per_10k, q.seven_day_annualized_rate, q.source,
    q.source_timestamp, q.raw_hash
"""
_PLAN_COLUMNS = """
    id, product_id, daily_amount, purchase_fee_rate, source_cash_product_id,
    status, start_date
"""
_PLAN_EXECUTION_COLUMNS = """
    id, plan_id, intended_trade_date, status, reason, transaction_id
"""


class PortfolioRepository:
    def __init__(self, database):
        self.database = database

    def add_product(
        self,
        product: Product,
        conn: sqlite3.Connection | None = None,
    ) -> Product:
        if conn is None:
            try:
                with self.database.transaction() as owned:
                    return self.add_product(product, owned)
            except sqlite3.IntegrityError as exc:
                raise ValueError("product already exists") from exc
        try:
            conn.execute(
                """INSERT INTO products
                   (id, provider, code, name, product_type, status,
                    registration_code, currency, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product.id,
                    product.provider,
                    product.code,
                    product.name,
                    product.product_type.value,
                    product.status.value,
                    product.registration_code,
                    product.currency,
                    product.metadata_json,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("product already exists") from exc
        return product

    def get_product(
        self,
        product_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> Product | None:
        query = f"SELECT {_PRODUCT_COLUMNS} FROM products WHERE id = ?"
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, (product_id,)).fetchone()
        else:
            row = conn.execute(query, (product_id,)).fetchone()
        return self._product_from_row(row) if row else None

    def require_product(
        self,
        product_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> Product:
        product = self.get_product(product_id, conn=conn)
        if product is None:
            raise ValueError("product not found")
        return product

    def get_product_by_provider_code(
        self,
        provider: str,
        code: str,
        conn: sqlite3.Connection | None = None,
    ) -> Product | None:
        query = (
            f"SELECT {_PRODUCT_COLUMNS} FROM products "
            "WHERE provider = ? AND code = ?"
        )
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, (provider, code)).fetchone()
        else:
            row = conn.execute(query, (provider, code)).fetchone()
        return self._product_from_row(row) if row else None

    def list_products(
        self,
        active_only: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> list[Product]:
        query = f"SELECT {_PRODUCT_COLUMNS} FROM products"
        params: tuple[str, ...] = ()
        if active_only:
            query += " WHERE status = ?"
            params = (ProductStatus.ACTIVE.value,)
        query += " ORDER BY id"
        if conn is None:
            with self.database.connection() as owned:
                rows = owned.execute(query, params).fetchall()
        else:
            rows = conn.execute(query, params).fetchall()
        return [self._product_from_row(row) for row in rows]

    def create_transaction(
        self, tx: Transaction, conn: sqlite3.Connection | None = None
    ) -> Transaction:
        if not tx.trade_time:
            tx = replace(tx, trade_time=datetime.now().strftime("%H:%M:%S"))
        if conn is None:
            with self.database.transaction() as owned:
                return self.create_transaction(tx, owned)
        existing = conn.execute(
            f"""SELECT {_TRANSACTION_COLUMNS}
                FROM transactions WHERE idempotency_key = ?""",
            (tx.idempotency_key,),
        ).fetchone()
        if existing:
            return self._transaction_from_row(existing)
        conn.execute(
            """INSERT INTO transactions
               (id, product_id, transaction_type, status, trade_date,
                trade_time, confirmation_date, amount, shares, fee_amount, fee_rate,
                confirmation_nav, linked_transaction_id, plan_id,
                idempotency_key, note, created_by, settlement_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx.id,
                tx.product_id,
                tx.transaction_type.value,
                tx.status.value,
                tx.trade_date.isoformat(),
                tx.trade_time,
                tx.confirmation_date.isoformat() if tx.confirmation_date else None,
                optional_decimal_text(tx.amount),
                optional_decimal_text(tx.shares),
                optional_decimal_text(tx.fee_amount),
                optional_decimal_text(tx.fee_rate),
                optional_decimal_text(tx.confirmation_nav),
                tx.linked_transaction_id or None,
                tx.plan_id or None,
                tx.idempotency_key,
                tx.note,
                tx.created_by,
                tx.settlement_date.isoformat() if tx.settlement_date else None,
            ),
        )
        return tx

    def get_transaction_by_id(
        self,
        transaction_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> Transaction | None:
        query = (
            f"SELECT {_TRANSACTION_COLUMNS} "
            "FROM transactions WHERE id = ?"
        )
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, (transaction_id,)).fetchone()
        else:
            row = conn.execute(query, (transaction_id,)).fetchone()
        return self._transaction_from_row(row) if row else None

    def get_transaction_by_idempotency(
        self,
        idempotency_key: str,
        conn: sqlite3.Connection | None = None,
    ) -> Transaction | None:
        query = (
            f"SELECT {_TRANSACTION_COLUMNS} "
            "FROM transactions WHERE idempotency_key = ?"
        )
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, (idempotency_key,)).fetchone()
        else:
            row = conn.execute(query, (idempotency_key,)).fetchone()
        return self._transaction_from_row(row) if row else None

    def update_pending_transaction(
        self,
        tx: Transaction,
        conn: sqlite3.Connection | None = None,
    ) -> Transaction:
        if conn is None:
            with self.database.transaction() as owned:
                return self.update_pending_transaction(tx, owned)
        current = self.get_transaction_by_id(tx.id, conn=conn)
        if current is None:
            raise ValueError("transaction not found")
        if current.status not in {
            TransactionStatus.PENDING_QUOTE,
            TransactionStatus.PENDING_CONFIRMATION,
        }:
            raise ValueError("transaction is not pending")
        conn.execute(
            """UPDATE transactions
               SET product_id = ?, transaction_type = ?, status = ?,
                   trade_date = ?, confirmation_date = ?, amount = ?,
                   shares = ?, fee_amount = ?, fee_rate = ?,
                   confirmation_nav = ?, linked_transaction_id = ?,
                   plan_id = ?, idempotency_key = ?, note = ?, created_by = ?,
                   confirmed_at = CASE
                       WHEN ? = 'confirmed' THEN CURRENT_TIMESTAMP
                       ELSE confirmed_at
                   END
               WHERE id = ?""",
            (
                tx.product_id,
                tx.transaction_type.value,
                tx.status.value,
                tx.trade_date.isoformat(),
                tx.confirmation_date.isoformat() if tx.confirmation_date else None,
                optional_decimal_text(tx.amount),
                optional_decimal_text(tx.shares),
                optional_decimal_text(tx.fee_amount),
                optional_decimal_text(tx.fee_rate),
                optional_decimal_text(tx.confirmation_nav),
                tx.linked_transaction_id or None,
                tx.plan_id or None,
                tx.idempotency_key,
                tx.note,
                tx.created_by,
                tx.status.value,
                tx.id,
            ),
        )
        return tx

    def list_transactions(
        self,
        product_id: str | None = None,
        statuses=None,
        conn: sqlite3.Connection | None = None,
    ) -> list[Transaction]:
        query = f"SELECT {_TRANSACTION_COLUMNS} FROM transactions"
        conditions = []
        params = []
        if product_id is not None:
            conditions.append("product_id = ?")
            params.append(product_id)
        if statuses is not None:
            status_values = [
                status.value if isinstance(status, TransactionStatus) else status
                for status in statuses
            ]
            if not status_values:
                return []
            placeholders = ", ".join("?" for _ in status_values)
            conditions.append(f"status IN ({placeholders})")
            params.extend(status_values)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY trade_date, created_at, id"
        if conn is None:
            with self.database.connection() as owned:
                rows = owned.execute(query, tuple(params)).fetchall()
        else:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._transaction_from_row(row) for row in rows]

    def replace_position(
        self, position: Position, conn: sqlite3.Connection | None = None
    ) -> None:
        if conn is None:
            with self.database.transaction() as owned:
                self.replace_position(position, owned)
            return
        conn.execute(
            """INSERT INTO positions
               (product_id, available_shares, locked_shares, total_shares, cost_basis)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(product_id) DO UPDATE SET
                   available_shares = excluded.available_shares,
                   locked_shares = excluded.locked_shares,
                   total_shares = excluded.total_shares,
                   cost_basis = excluded.cost_basis""",
            (
                position.product_id,
                optional_decimal_text(position.available_shares),
                optional_decimal_text(position.locked_shares),
                optional_decimal_text(position.total_shares),
                optional_decimal_text(position.cost_basis),
            ),
        )

    def get_position(self, product_id: str) -> Position:
        with self.database.connection() as conn:
            row = conn.execute(
                f"SELECT {_POSITION_COLUMNS} FROM positions WHERE product_id = ?",
                (product_id,),
            ).fetchone()
        if row:
            return self._position_from_row(row)
        return Position(product_id, Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))

    def upsert_quote(
        self,
        product_id: str,
        quote: MarketQuote,
        fetched_at: str,
        conn: sqlite3.Connection | None = None,
    ) -> MarketQuote:
        quote_id = str(uuid5(NAMESPACE_URL, f"{product_id}:{quote.quote_date.isoformat()}"))
        if conn is None:
            with self.database.transaction() as owned:
                return self.upsert_quote(
                    product_id, quote, fetched_at, conn=owned
                )
        conn.execute(
            """INSERT INTO quotes (
                   id, product_id, quote_date, unit_nav, cumulative_nav,
                   income_per_10k, seven_day_annualized_rate, source,
                   source_timestamp, raw_hash, fetched_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, quote_date) DO UPDATE SET
                   unit_nav = excluded.unit_nav,
                   cumulative_nav = excluded.cumulative_nav,
                   income_per_10k = excluded.income_per_10k,
                   seven_day_annualized_rate = excluded.seven_day_annualized_rate,
                   source = excluded.source,
                   source_timestamp = excluded.source_timestamp,
                   raw_hash = excluded.raw_hash,
                   fetched_at = excluded.fetched_at""",
            (
                quote_id,
                product_id,
                quote.quote_date.isoformat(),
                optional_decimal_text(quote.unit_nav),
                optional_decimal_text(quote.cumulative_nav),
                optional_decimal_text(quote.income_per_10k),
                optional_decimal_text(quote.seven_day_annualized_rate),
                quote.source,
                quote.source_timestamp or None,
                quote.raw_hash,
                fetched_at,
            ),
        )
        return quote

    def get_quote(
        self,
        product_id: str,
        quote_date: date,
        conn: sqlite3.Connection | None = None,
    ) -> MarketQuote | None:
        query = (
            f"SELECT {_QUOTE_COLUMNS} FROM quotes q "
            "JOIN products p ON p.id = q.product_id "
            "WHERE q.product_id = ? AND q.quote_date = ?"
        )
        params = (product_id, quote_date.isoformat())
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, params).fetchone()
        else:
            row = conn.execute(query, params).fetchone()
        return self._quote_from_row(row) if row else None

    def latest_quote(
        self,
        product_id: str,
        on_or_before: date | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> MarketQuote | None:
        query = (
            f"SELECT {_QUOTE_COLUMNS} FROM quotes q "
            "JOIN products p ON p.id = q.product_id WHERE q.product_id = ?"
        )
        params: tuple[str, ...] = (product_id,)
        if on_or_before is not None:
            query += " AND q.quote_date <= ?"
            params += (on_or_before.isoformat(),)
        query += " ORDER BY q.quote_date DESC LIMIT 1"
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, params).fetchone()
        else:
            row = conn.execute(query, params).fetchone()
        return self._quote_from_row(row) if row else None

    def list_quotes(
        self,
        product_id: str,
        on_or_before: date | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[MarketQuote]:
        query = (
            f"SELECT {_QUOTE_COLUMNS} FROM quotes q "
            "JOIN products p ON p.id = q.product_id WHERE q.product_id = ?"
        )
        params: tuple[str, ...] = (product_id,)
        if on_or_before is not None:
            query += " AND q.quote_date <= ?"
            params += (on_or_before.isoformat(),)
        query += " ORDER BY q.quote_date"
        if conn is None:
            with self.database.connection() as owned:
                rows = owned.execute(query, params).fetchall()
        else:
            rows = conn.execute(query, params).fetchall()
        return [self._quote_from_row(row) for row in rows]

    def save_plan(
        self,
        plan: SipPlan,
        conn: sqlite3.Connection | None = None,
    ) -> SipPlan:
        if conn is None:
            with self.database.transaction() as owned:
                return self.save_plan(plan, owned)
        conn.execute(
            """INSERT INTO sip_plans
               (id, product_id, daily_amount, purchase_fee_rate,
                source_cash_product_id, status, start_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   product_id = excluded.product_id,
                   daily_amount = excluded.daily_amount,
                   purchase_fee_rate = excluded.purchase_fee_rate,
                   source_cash_product_id = excluded.source_cash_product_id,
                   status = excluded.status,
                   start_date = excluded.start_date,
                   updated_at = CURRENT_TIMESTAMP,
                   paused_at = CASE
                       WHEN excluded.status = 'paused'
                            AND sip_plans.status != 'paused'
                           THEN CURRENT_TIMESTAMP
                       WHEN excluded.status = 'paused'
                           THEN sip_plans.paused_at
                       ELSE NULL
                   END""",
            (
                plan.id,
                plan.product_id,
                optional_decimal_text(plan.daily_amount),
                optional_decimal_text(plan.purchase_fee_rate),
                plan.source_cash_product_id or None,
                plan.status.value,
                plan.start_date.isoformat(),
            ),
        )
        return plan

    def get_plan(
        self,
        plan_id: str,
        conn: sqlite3.Connection | None = None,
        *,
        include_deleted: bool = False,
    ) -> SipPlan | None:
        query = f"SELECT {_PLAN_COLUMNS} FROM sip_plans WHERE id = ?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, (plan_id,)).fetchone()
        else:
            row = conn.execute(query, (plan_id,)).fetchone()
        return self._plan_from_row(row) if row else None

    def list_plans(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[SipPlan]:
        query = f"SELECT {_PLAN_COLUMNS} FROM sip_plans"
        if not include_deleted:
            query += " WHERE deleted_at IS NULL"
        query += " ORDER BY created_at, id"
        if conn is None:
            with self.database.connection() as owned:
                rows = owned.execute(query).fetchall()
        else:
            rows = conn.execute(query).fetchall()
        return [self._plan_from_row(row) for row in rows]

    def delete_plan(
        self,
        plan_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is None:
            with self.database.transaction() as owned:
                self.delete_plan(plan_id, owned)
            return
        cursor = conn.execute(
            """UPDATE sip_plans
               SET status = 'paused',
                   paused_at = COALESCE(paused_at, CURRENT_TIMESTAMP),
                   deleted_at = COALESCE(deleted_at, CURRENT_TIMESTAMP),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND deleted_at IS NULL""",
            (plan_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("plan not found")

    def save_plan_execution(
        self,
        execution,
        conn: sqlite3.Connection | None = None,
    ):
        if conn is None:
            with self.database.transaction() as owned:
                return self.save_plan_execution(execution, owned)
        existing = conn.execute(
            f"""SELECT {_PLAN_EXECUTION_COLUMNS}
                FROM plan_executions
                WHERE plan_id = ? AND intended_trade_date = ?""",
            (
                execution.plan_id,
                execution.intended_trade_date.isoformat(),
            ),
        ).fetchone()
        if existing is not None and existing["id"] != execution.id:
            return self._plan_execution_from_row(existing)
        if existing is None:
            conn.execute(
                """INSERT INTO plan_executions
                   (id, plan_id, intended_trade_date, status, reason,
                    transaction_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    execution.id,
                    execution.plan_id,
                    execution.intended_trade_date.isoformat(),
                    execution.status,
                    execution.reason,
                    execution.transaction_id or None,
                ),
            )
        else:
            conn.execute(
                """UPDATE plan_executions
                   SET status = ?, reason = ?, transaction_id = ?
                   WHERE id = ?""",
                (
                    execution.status,
                    execution.reason,
                    execution.transaction_id or None,
                    execution.id,
                ),
            )
        return execution

    def get_plan_execution(
        self,
        plan_id: str,
        intended_trade_date: date,
        conn: sqlite3.Connection | None = None,
    ):
        query = (
            f"SELECT {_PLAN_EXECUTION_COLUMNS} FROM plan_executions "
            "WHERE plan_id = ? AND intended_trade_date = ?"
        )
        params = (plan_id, intended_trade_date.isoformat())
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, params).fetchone()
        else:
            row = conn.execute(query, params).fetchone()
        return self._plan_execution_from_row(row) if row else None

    def get_plan_execution_by_id(
        self,
        execution_id: str,
        conn: sqlite3.Connection | None = None,
    ):
        query = (
            f"SELECT {_PLAN_EXECUTION_COLUMNS} FROM plan_executions "
            "WHERE id = ?"
        )
        if conn is None:
            with self.database.connection() as owned:
                row = owned.execute(query, (execution_id,)).fetchone()
        else:
            row = conn.execute(query, (execution_id,)).fetchone()
        return self._plan_execution_from_row(row) if row else None

    def list_plan_executions(
        self,
        plan_id: str,
        conn: sqlite3.Connection | None = None,
    ):
        query = (
            f"SELECT {_PLAN_EXECUTION_COLUMNS} FROM plan_executions "
            "WHERE plan_id = ? ORDER BY intended_trade_date, created_at, id"
        )
        if conn is None:
            with self.database.connection() as owned:
                rows = owned.execute(query, (plan_id,)).fetchall()
        else:
            rows = conn.execute(query, (plan_id,)).fetchall()
        return [self._plan_execution_from_row(row) for row in rows]

    def enqueue_plan_execution_notification(
        self,
        execution,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is None:
            with self.database.transaction() as owned:
                self.enqueue_plan_execution_notification(execution, owned)
            return
        notification_id = str(
            uuid5(
                NAMESPACE_URL,
                f"portfolio-sip-notification:{execution.id}",
            )
        )
        conn.execute(
            """INSERT INTO audit_logs
               (id, action, object_type, object_id, before_json, after_json,
                result, source)
               VALUES (?, 'notify', 'plan_execution', ?, '{}', '{}',
                       'pending', 'sip')
               ON CONFLICT(id) DO NOTHING""",
            (notification_id, execution.id),
        )

    def list_pending_plan_execution_notifications(
        self,
        conn: sqlite3.Connection | None = None,
    ):
        query = """
            SELECT e.id, e.plan_id, e.intended_trade_date, e.status,
                   e.reason, e.transaction_id
            FROM audit_logs AS a
            JOIN plan_executions AS e ON e.id = a.object_id
            WHERE a.action = 'notify'
              AND a.object_type = 'plan_execution'
              AND a.result = 'pending'
              AND a.source = 'sip'
            ORDER BY a.created_at, a.id
        """
        if conn is None:
            with self.database.connection() as owned:
                rows = owned.execute(query).fetchall()
        else:
            rows = conn.execute(query).fetchall()
        return [self._plan_execution_from_row(row) for row in rows]

    def mark_plan_execution_notification_sent(
        self,
        execution_id: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is None:
            with self.database.transaction() as owned:
                self.mark_plan_execution_notification_sent(
                    execution_id, owned
                )
            return
        conn.execute(
            """UPDATE audit_logs
               SET result = 'success'
               WHERE action = 'notify'
                 AND object_type = 'plan_execution'
                 AND object_id = ?
                 AND result = 'pending'
                 AND source = 'sip'""",
            (execution_id,),
        )

    def append_audit(
        self,
        audit_id: str,
        action: str,
        object_type: str,
        object_id: str,
        before_json: str = "{}",
        after_json: str = "{}",
        result: str = "success",
        source: str = "system",
        conn: sqlite3.Connection | None = None,
    ) -> None:
        if conn is None:
            with self.database.transaction() as owned:
                self.append_audit(
                    audit_id, action, object_type, object_id, before_json,
                    after_json, result, source, owned,
                )
            return
        conn.execute(
            """INSERT INTO audit_logs
               (id, action, object_type, object_id, before_json, after_json,
                result, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                audit_id,
                action,
                object_type,
                object_id,
                before_json,
                after_json,
                result,
                source,
            ),
        )

    @staticmethod
    def _product_from_row(row) -> Product:
        return Product(
            id=row["id"],
            provider=row["provider"],
            code=row["code"],
            name=row["name"],
            product_type=ProductType(row["product_type"]),
            status=ProductStatus(row["status"]),
            registration_code=row["registration_code"],
            currency=row["currency"],
            metadata_json=row["metadata_json"],
        )

    @staticmethod
    def _transaction_from_row(row) -> Transaction:
        return Transaction(
            id=row["id"],
            product_id=row["product_id"],
            transaction_type=TransactionType(row["transaction_type"]),
            status=TransactionStatus(row["status"]),
            trade_date=date.fromisoformat(row["trade_date"]),
            confirmation_date=(
                date.fromisoformat(row["confirmation_date"])
                if row["confirmation_date"]
                else None
            ),
            amount=Decimal(row["amount"]) if row["amount"] is not None else None,
            shares=Decimal(row["shares"]) if row["shares"] is not None else None,
            fee_amount=(
                Decimal(row["fee_amount"]) if row["fee_amount"] is not None else None
            ),
            fee_rate=(
                Decimal(row["fee_rate"]) if row["fee_rate"] is not None else None
            ),
            confirmation_nav=(
                Decimal(row["confirmation_nav"])
                if row["confirmation_nav"] is not None
                else None
            ),
            linked_transaction_id=row["linked_transaction_id"] or "",
            plan_id=row["plan_id"] or "",
            idempotency_key=row["idempotency_key"],
            trade_time=row["trade_time"] or "",
            note=row["note"],
            created_by=row["created_by"],
            confirmed_at=row["confirmed_at"] or "",
            settlement_date=(
                date.fromisoformat(row["settlement_date"])
                if row["settlement_date"]
                else None
            ),
        )

    @staticmethod
    def _position_from_row(row) -> Position:
        return Position(
            product_id=row["product_id"],
            available_shares=Decimal(row["available_shares"]),
            locked_shares=Decimal(row["locked_shares"]),
            total_shares=Decimal(row["total_shares"]),
            cost_basis=Decimal(row["cost_basis"]),
        )

    @staticmethod
    def _plan_from_row(row) -> SipPlan:
        return SipPlan(
            id=row["id"],
            product_id=row["product_id"],
            daily_amount=Decimal(row["daily_amount"]),
            purchase_fee_rate=Decimal(row["purchase_fee_rate"]),
            source_cash_product_id=row["source_cash_product_id"] or "",
            status=SipPlanStatus(row["status"]),
            start_date=date.fromisoformat(row["start_date"]),
        )

    @staticmethod
    def _plan_execution_from_row(row):
        from src.portfolio_sip import PlanExecution

        return PlanExecution(
            id=row["id"],
            plan_id=row["plan_id"],
            intended_trade_date=date.fromisoformat(
                row["intended_trade_date"]
            ),
            status=row["status"],
            reason=row["reason"],
            transaction_id=row["transaction_id"] or "",
        )

    @staticmethod
    def _quote_from_row(row) -> MarketQuote:
        return MarketQuote(
            product_code=row["product_code"],
            quote_date=date.fromisoformat(row["quote_date"]),
            unit_nav=Decimal(row["unit_nav"]) if row["unit_nav"] is not None else None,
            cumulative_nav=(
                Decimal(row["cumulative_nav"])
                if row["cumulative_nav"] is not None
                else None
            ),
            income_per_10k=(
                Decimal(row["income_per_10k"])
                if row["income_per_10k"] is not None
                else None
            ),
            seven_day_annualized_rate=(
                Decimal(row["seven_day_annualized_rate"])
                if row["seven_day_annualized_rate"] is not None
                else None
            ),
            source=row["source"],
            source_timestamp=row["source_timestamp"] or "",
            raw_hash=row["raw_hash"],
        )
