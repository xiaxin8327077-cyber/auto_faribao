from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional


class ProductType(str, Enum):
    WEALTH_NAV = "wealth_nav"
    CASH_MANAGEMENT = "cash_management"
    PUBLIC_FUND = "public_fund"


class ProductStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class TransactionType(str, Enum):
    OPENING_POSITION = "opening_position"
    MANUAL_PURCHASE = "manual_purchase"
    MANUAL_REDEMPTION = "manual_redemption"
    SIP_PURCHASE = "sip_purchase"
    CASH_TRANSFER_OUT = "cash_transfer_out"
    CASH_TRANSFER_IN = "cash_transfer_in"
    INCOME_ACCRUAL = "income_accrual"
    CASH_DIVIDEND = "cash_dividend"
    REVERSAL = "reversal"
    HOLDING_ADJUSTMENT = "holding_adjustment"
    PROFIT_ADJUSTMENT = "profit_adjustment"
    LATEST_PROFIT_ADJUSTMENT = "latest_profit_adjustment"


class TransactionStatus(str, Enum):
    PENDING_QUOTE = "pending_quote"
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    REVERSED = "reversed"
    FAILED = "failed"


class SipPlanStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"


class SipFrequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


def decimal_text(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"decimal value must be finite: {value!r}")
    return format(number.normalize(), "f")


def optional_decimal_text(value) -> Optional[str]:
    if value in (None, ""):
        return None
    return decimal_text(value)


@dataclass(frozen=True)
class Product:
    id: str
    provider: str
    code: str
    name: str
    product_type: ProductType
    status: ProductStatus = ProductStatus.ACTIVE
    registration_code: str = ""
    currency: str = "CNY"
    metadata_json: str = "{}"


@dataclass(frozen=True)
class MarketProduct:
    provider: str
    code: str
    name: str
    product_type: ProductType
    registration_code: str = ""
    metadata: Optional[Mapping[str, str]] = None

    def __post_init__(self) -> None:
        metadata = {} if self.metadata is None else dict(self.metadata)
        object.__setattr__(self, "metadata", MappingProxyType(metadata))


@dataclass(frozen=True)
class MarketQuote:
    product_code: str
    quote_date: date
    source: str
    raw_hash: str
    unit_nav: Optional[Decimal] = None
    cumulative_nav: Optional[Decimal] = None
    income_per_10k: Optional[Decimal] = None
    seven_day_annualized_rate: Optional[Decimal] = None
    source_timestamp: str = ""


@dataclass(frozen=True)
class Transaction:
    id: str
    product_id: str
    transaction_type: TransactionType
    status: TransactionStatus
    trade_date: date
    idempotency_key: str
    trade_time: str = ""
    amount: Optional[Decimal] = None
    shares: Optional[Decimal] = None
    fee_amount: Optional[Decimal] = None
    fee_rate: Optional[Decimal] = None
    confirmation_nav: Optional[Decimal] = None
    confirmation_date: Optional[date] = None
    linked_transaction_id: str = ""
    plan_id: str = ""
    note: str = ""
    created_by: str = "system"
    confirmed_at: Optional[str] = None
    settlement_date: Optional[date] = None


@dataclass(frozen=True)
class Position:
    product_id: str
    available_shares: Decimal
    locked_shares: Decimal
    total_shares: Decimal
    cost_basis: Decimal


@dataclass(frozen=True)
class SipPlan:
    id: str
    product_id: str
    daily_amount: Decimal
    purchase_fee_rate: Decimal
    source_cash_product_id: str
    status: SipPlanStatus
    start_date: date
    frequency: SipFrequency = SipFrequency.DAILY
    schedule_day: Optional[int] = None
    # 周期规则生效下界：修改周期后新规则只从该日起，避免追补历史。
    schedule_effective_date: Optional[date] = None
