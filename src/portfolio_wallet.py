from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from src.portfolio_models import (
    MarketProduct,
    MarketQuote,
    Product,
    ProductStatus,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_confirmation import (
    MissingTradingCalendarError,
    is_trading_day,
)


WALLET_PRODUCT_ID = "wallet-plus"
WALLET_PROVIDER = "wallet_plus"
WALLET_CODE = "WALLETPLUS"
WALLET_NAME = "钱包Plus"
WALLET_ANNUALIZED_RATE = Decimal("0.0133")
WALLET_INCOME_PER_10K = Decimal("0.3644")
_MIGRATION_AUDIT_ID = str(
    uuid5(NAMESPACE_URL, "portfolio-wallet-plus-consolidation-v1")
)


class WalletPlusProvider:
    provider = WALLET_PROVIDER

    def resolve_product(self, code: str) -> MarketProduct:
        if str(code or "").strip().upper() != WALLET_CODE:
            raise ValueError("钱包Plus产品代码无效")
        return MarketProduct(
            provider=self.provider,
            code=WALLET_CODE,
            name=WALLET_NAME,
            product_type=ProductType.CASH_MANAGEMENT,
            registration_code=WALLET_CODE,
            metadata={"source": "wallet_plus_fixed"},
        )

    def fetch_quotes(
        self,
        product: MarketProduct,
        start_date: date,
        end_date: date,
    ) -> list[MarketQuote]:
        if (
            product.provider != self.provider
            or product.code.upper() != WALLET_CODE
            or product.product_type is not ProductType.CASH_MANAGEMENT
        ):
            raise ValueError("钱包Plus产品身份不匹配")
        if end_date < start_date:
            return []
        quotes = []
        current = start_date
        while current <= end_date:
            if not is_trading_day(current):
                current += timedelta(days=1)
                continue
            raw = {
                "annualized_rate": str(WALLET_ANNUALIZED_RATE),
                "income_per_10k": str(WALLET_INCOME_PER_10K),
                "quote_date": current.isoformat(),
            }
            raw_hash = hashlib.sha256(
                json.dumps(raw, sort_keys=True).encode("utf-8")
            ).hexdigest()
            quotes.append(
                MarketQuote(
                    product_code=WALLET_CODE,
                    quote_date=current,
                    source="wallet_plus_fixed",
                    raw_hash=raw_hash,
                    income_per_10k=WALLET_INCOME_PER_10K,
                    seven_day_annualized_rate=WALLET_ANNUALIZED_RATE,
                )
            )
            current += timedelta(days=1)
        return sorted(quotes, key=lambda quote: quote.quote_date, reverse=True)


@dataclass(frozen=True)
class CashConsolidationResult:
    migrated_balance: Decimal
    migrated_products: int


def _upsert_wallet_quote(repository, quote_date, conn):
    provider = WalletPlusProvider()
    try:
        quotes = provider.fetch_quotes(
            provider.resolve_product(WALLET_CODE),
            quote_date,
            quote_date,
        )
    except MissingTradingCalendarError:
        return
    if not quotes:
        return
    repository.upsert_quote(
        WALLET_PRODUCT_ID,
        quotes[0],
        f"{quote_date.isoformat()}T00:00:00+08:00",
        conn=conn,
    )


def consolidate_cash_products(
    repository,
    effective_date: date,
) -> CashConsolidationResult:
    projector = PositionProjector(repository)
    with repository.database.transaction() as conn:
        existing_audit = conn.execute(
            "SELECT 1 FROM audit_logs WHERE id = ?",
            (_MIGRATION_AUDIT_ID,),
        ).fetchone()
        if existing_audit is not None:
            _upsert_wallet_quote(repository, effective_date, conn)
            return CashConsolidationResult(Decimal("0"), 0)

        wallet = repository.get_product(WALLET_PRODUCT_ID, conn=conn)
        if wallet is None:
            repository.add_product(
                Product(
                    id=WALLET_PRODUCT_ID,
                    provider=WALLET_PROVIDER,
                    code=WALLET_CODE,
                    name=WALLET_NAME,
                    product_type=ProductType.CASH_MANAGEMENT,
                    registration_code=WALLET_CODE,
                    metadata_json=json.dumps(
                        {
                            "annualized_rate": str(WALLET_ANNUALIZED_RATE),
                            "income_per_10k": str(WALLET_INCOME_PER_10K),
                            "source": "wallet_plus_fixed",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
                conn=conn,
            )
        elif (
            wallet.product_type is not ProductType.CASH_MANAGEMENT
            or wallet.provider != WALLET_PROVIDER
            or wallet.code.upper() != WALLET_CODE
        ):
            raise ValueError("钱包Plus产品身份冲突")
        else:
            conn.execute(
                """UPDATE products
                   SET name = ?, status = 'active',
                       registration_code = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (WALLET_NAME, WALLET_CODE, WALLET_PRODUCT_ID),
            )

        old_cash = [
            product
            for product in repository.list_products(conn=conn)
            if (
                product.product_type is ProductType.CASH_MANAGEMENT
                and product.id != WALLET_PRODUCT_ID
            )
        ]
        old_ids = [product.id for product in old_cash]
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            conn.execute(
                f"""UPDATE transactions
                    SET product_id = ?
                    WHERE product_id IN ({placeholders})
                      AND status IN (
                          'pending_quote', 'pending_confirmation'
                      )""",
                (WALLET_PRODUCT_ID, *old_ids),
            )
            conn.execute(
                f"""UPDATE sip_plans
                    SET source_cash_product_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE source_cash_product_id IN ({placeholders})""",
                (WALLET_PRODUCT_ID, *old_ids),
            )

        migrated_balance = Decimal("0")
        for product in old_cash:
            position = projector._calculate(product.id, conn)
            balance = position.total_shares
            if balance > 0:
                repository.create_transaction(
                    Transaction(
                        id=str(
                            uuid5(
                                NAMESPACE_URL,
                                f"wallet-plus-out:{product.id}",
                            )
                        ),
                        product_id=product.id,
                        transaction_type=TransactionType.HOLDING_ADJUSTMENT,
                        status=TransactionStatus.CONFIRMED,
                        trade_date=effective_date,
                        idempotency_key=f"wallet-plus-out:{product.id}",
                        amount=Decimal("0"),
                        shares=-balance,
                        confirmation_date=effective_date,
                        note="现金产品余额合并至钱包Plus",
                        created_by="wallet_migration",
                    ),
                    conn=conn,
                )
                migrated_balance += balance
            conn.execute(
                """UPDATE products
                   SET status = 'inactive', updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (product.id,),
            )

        if migrated_balance > 0:
            repository.create_transaction(
                Transaction(
                    id=str(uuid5(NAMESPACE_URL, "wallet-plus-in:v1")),
                    product_id=WALLET_PRODUCT_ID,
                    transaction_type=TransactionType.HOLDING_ADJUSTMENT,
                    status=TransactionStatus.CONFIRMED,
                    trade_date=effective_date,
                    idempotency_key="wallet-plus-in:v1",
                    amount=migrated_balance,
                    shares=migrated_balance,
                    confirmation_nav=Decimal("1"),
                    confirmation_date=effective_date,
                    note="合并原现金产品余额",
                    created_by="wallet_migration",
                ),
                conn=conn,
            )

        _upsert_wallet_quote(repository, effective_date, conn)

        repository.append_audit(
            _MIGRATION_AUDIT_ID,
            "consolidate_wallet_plus",
            "product",
            WALLET_PRODUCT_ID,
            before_json=json.dumps(
                {"cash_product_ids": old_ids},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            after_json=json.dumps(
                {
                    "migrated_balance": str(migrated_balance),
                    "migrated_products": len(old_cash),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            result="success",
            source="migration",
            conn=conn,
        )
        return CashConsolidationResult(migrated_balance, len(old_cash))
