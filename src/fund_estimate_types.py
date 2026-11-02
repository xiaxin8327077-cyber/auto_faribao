"""估值数据契约。对外金额和百分数用十进制字符串，不写入正式账本。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal, Optional


EstimateStatus = Literal[
    "pending",
    "reference",
    "experimental",
    "ready",
    "stale",
    "unsupported",
    "error",
]

TARGET = "mark_to_market_total_return"
FACTOR_SET_ID = "bond_factor_v1"
FACTOR_NAMES = (
    "gov_3y_bp",
    "gov_slope_10y_3y_bp",
    "cdb_spread_5y_bp",
    "credit_aaa_spread_3y_bp",
    "calendar_days",
)
FACTOR_UNITS = {
    "gov_3y_bp": "bp",
    "gov_slope_10y_3y_bp": "bp",
    "cdb_spread_5y_bp": "bp",
    "credit_aaa_spread_3y_bp": "bp",
    "calendar_days": "day",
}
CONFIDENCE_LABELS = {
    "pending": "数据准备中",
    "reference": "同类参考",
    "experimental": "试验",
    "ready": "已验证",
    "stale": "陈旧",
    "unsupported": "未支持",
    "error": "错误",
}

ESTIMATE_SCHEMA_VERSION = 1
DEFAULT_DB_NAME = "fund_estimation.db"

# 质量门槛未审定前不得自动标成已验证。None 表示该门槛尚未设定。
DEFAULT_CONFIG = {
    "enabled": False,
    "display_mode": "experimental",
    "enable_ready": False,
    "min_fit_samples": 60,
    "min_oos_residuals": 20,
    "half_life_days": 30,
    "ridge_alpha": 1.0,
    "training_windows": (60, 120, 250),
    "stale_minutes": 15,
    "intraday_stale_minutes": 15,
    "max_mae_bp": None,
    "min_direction_accuracy": None,
    "min_interval_coverage": None,
    "min_intraday_samples": 20,
    "shadow_times": ("09:45", "11:30", "14:30", "16:30"),
    "research_enabled": True,
    "research_history_days": 400,
    "research_windows": (20, 60, 120),
    "research_min_holdout": 20,
    "retention_years": 3,
}


class MissingDividendCoverage(ValueError):
    """分红覆盖未核验。禁止把缺失当成每份分红 0。"""


class EstimateInputError(ValueError):
    """模型输入的因子名称、顺序或单位不匹配。"""


def decimal_text(value) -> str:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"decimal value must be finite: {value!r}")
    return format(number.normalize(), "f")


def pct_text(value) -> str:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("percent value must be finite")
    quantized = number.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return format(quantized.normalize(), "f")


def optional_decimal_text(value) -> Optional[str]:
    if value is None or value == "":
        return None
    return decimal_text(value)


@dataclass(frozen=True)
class FundIdentity:
    product_id: str
    code: str
    provider: str
    share_class: str
    portfolio_group_id: Optional[str]
    fund_type: str
    currency: str = "CNY"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProfileVersion:
    profile_version: str
    identity: FundIdentity
    report_as_of: Optional[str]
    published_at: Optional[str]
    fetched_at: str
    source_url: str
    document_hash: str
    fields: dict
    evidence: list
    status: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["identity"] = self.identity.to_dict()
        return payload


@dataclass(frozen=True)
class NavObservation:
    code: str
    nav_date: str
    unit_nav: str
    cash_dividend_per_share: Optional[str]
    available_at: str
    source: str
    source_version: str
    event_status: str
    dividend_coverage: str = "missing"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FactorObservation:
    factor_set_id: str
    source_family: str
    as_of: str
    published_at: Optional[str]
    fetched_at: str
    market_as_of: Optional[str]
    values_bp: dict
    calendar_days: Optional[int]
    basis_status: str
    missing_fields: list
    basis_date: Optional[str] = None
    source_version: str = ""
    mapping_evidence: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TrainingSet:
    identity: FundIdentity
    cutoff_at: str
    rows: list
    feature_names: tuple
    source_versions: list
    profile_version: Optional[str]
    gaps: list = field(default_factory=list)
    source_family: str = "daily_close"

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["identity"] = self.identity.to_dict()
        payload["feature_names"] = list(self.feature_names)
        return payload


@dataclass(frozen=True)
class ModelVersion:
    model_version: str
    identity: FundIdentity
    model_kind: str
    trained_through: str
    available_at: str
    feature_names: tuple
    feature_units: dict
    coefficients: dict
    preprocessing: dict
    source_versions: list
    profile_version: Optional[str]
    validation: dict
    artifact_hash: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["identity"] = self.identity.to_dict()
        payload["feature_names"] = list(self.feature_names)
        return payload


@dataclass(frozen=True)
class EstimateSnapshot:
    product_id: str
    code: str
    trade_date: str
    target: str
    status: str
    reason_codes: list
    base_nav_date: Optional[str]
    base_nav: Optional[str]
    market_as_of: Optional[str]
    profile_as_of: Optional[str]
    report_published_at: Optional[str]
    trained_through: Optional[str]
    model_version: Optional[str]
    return_pct: Optional[str]
    lower_pct: Optional[str]
    upper_pct: Optional[str]
    interval_kind: str
    confidence_label: str
    market_reference: Optional[dict]
    generated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def snapshot_pending(identity: FundIdentity, trade_date: str, generated_at: str, reasons: list) -> EstimateSnapshot:
    return EstimateSnapshot(
        product_id=identity.product_id,
        code=identity.code,
        trade_date=trade_date,
        target=TARGET,
        status="pending",
        reason_codes=list(reasons),
        base_nav_date=None,
        base_nav=None,
        market_as_of=None,
        profile_as_of=None,
        report_published_at=None,
        trained_through=None,
        model_version=None,
        return_pct=None,
        lower_pct=None,
        upper_pct=None,
        interval_kind="none",
        confidence_label=CONFIDENCE_LABELS["pending"],
        market_reference=None,
        generated_at=generated_at,
    )


def ensure_aware(value: datetime, tz) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)
