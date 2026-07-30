from datetime import date
from decimal import Decimal
import sqlite3

from src.portfolio_models import (
    Position,
    Product,
    ProductStatus,
    ProductType,
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
    linked_transaction_id, plan_id, idempotency_key, note, created_by
"""
_POSITION_COLUMNS = """
    product_id, available_shares, locked_shares, total_shares, cost_basis
"""


class PortfolioRepository:
    def __init__(self, database):
        self.database = database

    def add_product(self, product: Product) -> Product:
        try:
            with self.database.transaction() as conn:
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

    def get_product(self, product_id: str) -> Product | None:
        with self.database.connection() as conn:
            row = conn.execute(
                f"SELECT {_PRODUCT_COLUMNS} FROM products WHERE id = ?",
                (product_id,),
            ).fetchone()
        return self._product_from_row(row) if row else None

    def list_products(self, active_only: bool = False) -> list[Product]:
        query = f"SELECT {_PRODUCT_COLUMNS} FROM products"
        params: tuple[str, ...] = ()
        if active_only:
            query += " WHERE status = ?"
            params = (ProductStatus.ACTIVE.value,)
        query += " ORDER BY id"
        with self.database.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._product_from_row(row) for row in rows]

    def create_transaction(
        self, tx: Transaction, conn: sqlite3.Connection | None = None
    ) -> Transaction:
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
                confirmation_date, amount, shares, fee_amount, fee_rate,
                confirmation_nav, linked_transaction_id, plan_id,
                idempotency_key, note, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx.id,
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
            ),
        )
        return tx

    def get_transaction_by_idempotency(
        self, idempotency_key: str
    ) -> Transaction | None:
        with self.database.connection() as conn:
            row = conn.execute(
                f"""SELECT {_TRANSACTION_COLUMNS}
                    FROM transactions WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
        return self._transaction_from_row(row) if row else None

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
            note=row["note"],
            created_by=row["created_by"],
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
