from dataclasses import dataclass
from datetime import date, timedelta
import json
import logging

from src.beijing_time import now as beijing_now
from src.portfolio_market import MarketProduct, QuoteSyncService
from src.portfolio_models import ProductType, SipPlanStatus
from src.portfolio_positions import PositionProjector
from src.portfolio_providers import get_market_provider
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService
from src.portfolio_income import CashIncomeService


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioCycleResult:
    quotes_synced: int
    intents_created: int
    trades_settled: int
    income_accrued: int


def _slot_key(now):
    return now.strftime("%Y-%m-%dT%H:") + ("00" if now.minute < 30 else "30")


class PortfolioJobs:
    def __init__(
        self,
        sync_quotes,
        create_intents,
        settle_pending,
        accrue_income,
    ):
        self.sync_quotes = sync_quotes
        self.create_intents = create_intents
        self.settle_pending = settle_pending
        self.accrue_income = accrue_income
        self._last_slot = None

    def run_cycle(self, now):
        slot = _slot_key(now)
        if slot == self._last_slot:
            return PortfolioCycleResult(0, 0, 0, 0)
        self._last_slot = slot
        day = now.date()
        quotes = int(self.sync_quotes(day) or 0)
        intents = int(self.create_intents(day) or 0)
        settled = int(self.settle_pending(day) or 0)
        income = int(self.accrue_income(day) or 0)
        return PortfolioCycleResult(quotes, intents, settled, income)


def _product_metadata(product):
    if not product.metadata_json:
        return None
    try:
        data = json.loads(product.metadata_json)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        return data
    return None


def build_portfolio_jobs(runtime, strict=False):
    repository = runtime.repository
    projector = PositionProjector(repository)
    transaction_service = PortfolioTransactionService(repository, projector)
    sip_service = SipService(repository, projector, transaction_service)
    income_service = CashIncomeService(repository, projector)
    sync_service = QuoteSyncService(repository, get_market_provider)

    def sync_quotes(day):
        synced = 0
        for product in repository.list_products(active_only=True):
            market_product = MarketProduct(
                provider=product.provider,
                code=product.code,
                name=product.name,
                product_type=product.product_type,
                registration_code=product.registration_code,
                metadata=_product_metadata(product),
            )
            try:
                quotes = sync_service.sync_product(
                    market_product, day - timedelta(days=1), day
                )
            except Exception:
                logger.error(
                    "Portfolio quote sync failed for %s",
                    product.code,
                    exc_info=True,
                )
                if strict:
                    raise
                continue
            synced += len(quotes)
        return synced

    def create_intents(day):
        created = 0
        for plan in repository.list_plans():
            if plan.status is not SipPlanStatus.ACTIVE:
                continue
            try:
                existing_ids = {
                    execution.id
                    for execution in repository.list_plan_executions(plan.id)
                }
                executions = sip_service.backfill_plan(
                    plan.id,
                    day,
                    settle=False,
                )
            except Exception:
                logger.error(
                    "Portfolio SIP intent failed for plan %s",
                    plan.id,
                    exc_info=True,
                )
                if strict:
                    raise
                continue
            created += sum(
                execution.id not in existing_ids
                and execution.status == "pending_quote"
                for execution in executions
            )
        return created

    def settle_pending(day):
        manual = transaction_service.settle_pending(day)
        sip = sip_service.settle_pending(day)
        return len(manual) + len(sip)

    def accrue_income(day):
        accrued = 0
        for product in repository.list_products(active_only=True):
            if product.product_type is not ProductType.CASH_MANAGEMENT:
                continue
            try:
                result = income_service.accrue(product.id, day)
            except Exception:
                logger.error(
                    "Portfolio income accrual failed for %s",
                    product.code,
                    exc_info=True,
                )
                if strict:
                    raise
                continue
            if result is not None:
                accrued += 1
        return accrued

    return PortfolioJobs(sync_quotes, create_intents, settle_pending, accrue_income)


def run_portfolio_cycle(runtime, now=None, raise_on_error=False):
    now = now or beijing_now()
    if not getattr(runtime, "write_enabled", False):
        logger.warning("Portfolio runtime is read-only; skipping cycle")
        return PortfolioCycleResult(0, 0, 0, 0)
    jobs = build_portfolio_jobs(runtime, strict=raise_on_error)
    try:
        result = jobs.run_cycle(now)
    except Exception:
        logger.error("Portfolio cycle failed", exc_info=True)
        if raise_on_error:
            raise
        return PortfolioCycleResult(0, 0, 0, 0)
    logger.info(
        "Portfolio cycle: quotes=%d intents=%d settled=%d income=%d",
        result.quotes_synced,
        result.intents_created,
        result.trades_settled,
        result.income_accrued,
    )
    return result
