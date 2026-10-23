"""持仓发现、只读快照和后台队列。tick 只入队，不在请求线程里训练。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import hashlib
import logging
from pathlib import Path
import threading
import time as time_module

from src.beijing_time import TZ_CN
from src.fund_estimate_ai import AiSettingsError, EstimateAi, chat_completions_url, scrub_secret
from src.fund_estimate_models import present_snapshot, project_snapshot, train_models
from src.fund_estimate_profiles import ProfileService, discover_share_class, support_reason
from src.fund_estimate_sources import (
    CHINABOND_EOD,
    EstimateSources,
    category_market_reference,
    default_history_start,
    factors_from_southern_barometer,
    live_chinabond_factors,
    live_dividend_coverage,
)
from src.fund_estimate_store import FundEstimateStore, iso_now, parse_time
from src.fund_estimate_types import (
    DEFAULT_CONFIG,
    FACTOR_NAMES,
    FactorObservation,
    FundIdentity,
    ensure_aware,
    snapshot_pending,
)
from src.portfolio_models import ProductType, TransactionStatus, TransactionType
from src.portfolio_confirmation import is_trading_day
from src.fund_estimate_cfets import CFETS_REALTIME, CfetsCurveSource


logger = logging.getLogger(__name__)

PURCHASE_TYPES = {TransactionType.MANUAL_PURCHASE, TransactionType.SIP_PURCHASE}
PENDING_STATUSES = {TransactionStatus.PENDING_QUOTE, TransactionStatus.PENDING_CONFIRMATION}
_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "fund_estimation.db"


def discover_funds(repository, now: datetime | None = None) -> list[FundIdentity]:
    """持有份额大于 0，或存在有效待确认买入的公募基金。不按代码白名单过滤。"""
    del now
    if repository is None:
        return []
    found = []
    for product in repository.list_products():
        if product.product_type is not ProductType.PUBLIC_FUND:
            continue
        position = repository.get_position(product.id)
        transactions = repository.list_transactions(product.id)
        pending = any(
            item.transaction_type in PURCHASE_TYPES and item.status in PENDING_STATUSES
            for item in transactions
        )
        held = position.total_shares > 0
        if not held and not pending:
            continue
        metadata = _metadata(product.metadata_json)
        fund_type = str(metadata.get("fund_type") or product.name or "")
        if product.currency and product.currency != 'CNY':
            fund_type += ' 海外'
        if support_reason(fund_type, product.name):
            fund_type = f"{fund_type} {product.name}".strip()
        found.append(
            FundIdentity(
                product_id=product.id,
                code=str(product.code),
                provider=product.provider,
                share_class=discover_share_class(product.name, metadata),
                portfolio_group_id=None,
                fund_type=fund_type,
                currency=product.currency or "CNY",
            )
        )
    return sorted(found, key=lambda item: (item.code, item.share_class, item.product_id))


def intraday_session(now: datetime, is_bond_day) -> bool:
    current = ensure_aware(now, TZ_CN)
    if not is_bond_day(current.date()):
        return False
    clock = current.time()
    return time(9, 0) <= clock <= time(17, 30)


def nav_poll_session(now: datetime, is_bond_day) -> bool:
    current = ensure_aware(now, TZ_CN)
    clock = current.time()
    if not (clock >= time(18, 0) or clock < time(2, 0)):
        return False
    session_day = current.date() - timedelta(days=1) if clock < time(2, 0) else current.date()
    return bool(is_bond_day(session_day))


def profile_check_due(now: datetime, last_check: datetime | None, is_bond_day) -> bool:
    current = ensure_aware(now, TZ_CN)
    if current.hour == 7 and current.minute >= 30 and (last_check is None or last_check.date() < current.date()):
        return True
    if not is_bond_day(current.date()):
        return False
    if not (time(8, 0) <= current.time() <= time(23, 0)):
        return False
    if last_check is None:
        return True
    return current - last_check >= timedelta(minutes=60)


def weekday_bond_day(day: date) -> bool:
    return day.weekday() < 5


class FundEstimateService:
    def __init__(self, repository, store, sources, profiles, config=None, bond_trading_day=None, ai=None):
        self.repository = repository
        self.store = store
        self.sources = sources
        self.profiles = profiles
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self.bond_trading_day = bond_trading_day or is_trading_day
        self.ai = ai or EstimateAi(store)
        self._resumed = False
        self._worker = None
        self._worker_guard = threading.Lock()

    def jobs_enabled(self) -> bool:
        if self.config.get("enabled"):
            return True
        try:
            return bool(self.store.public_ai_settings().get("jobs_enabled"))
        except Exception:
            return False

    def tick(self, now: datetime) -> None:
        if not self.jobs_enabled():
            return
        stamp = iso_now(now)
        if not self._resumed:
            self.store.resume_running_jobs(stamp)
            self._resumed = True
        identities = discover_funds(self.repository, now)
        family = self.config.get('source_family', CFETS_REALTIME)
        factor_revision = self.store.get_watermark(f'factor-revision:{family}')
        self.store.cancel_queued("intraday_snapshot", {item.code for item in identities}, stamp)
        for identity in identities:
            self.store.enqueue_job(
                f"backfill:v2:{family}:{identity.code}:{identity.share_class}",
                "backfill",
                identity.product_id,
                identity.code,
                _identity_payload(identity),
                stamp,
            )
            if factor_revision:
                self.store.enqueue_job(f'train:{family}:{identity.code}:{identity.share_class}:factors:{factor_revision}',
                    'train', identity.product_id, identity.code, _identity_payload(identity), stamp)
            if intraday_session(now, self.bond_trading_day):
                slot = ensure_aware(now, TZ_CN).strftime("%Y-%m-%dT%H:%M")
                snapshot_key = f"snapshot:{identity.code}:{identity.share_class}:{slot}"
                self.store.cancel_older_queued("intraday_snapshot", identity.code, snapshot_key, stamp)
                self.store.enqueue_job(
                    snapshot_key,
                    "intraday_snapshot",
                    identity.product_id,
                    identity.code,
                    _identity_payload(identity),
                    stamp,
                )
            if nav_poll_session(now, self.bond_trading_day):
                poll_slot = ensure_aware(now, TZ_CN).strftime("%Y-%m-%dT%H:") + ("00" if now.minute < 30 else "30")
                self.store.enqueue_job(
                    f"nav:{identity.code}:{identity.share_class}:{poll_slot}",
                    "nav_poll",
                    identity.product_id,
                    identity.code,
                    _identity_payload(identity),
                    stamp,
                )
            if self._profile_due(identity, now):
                hour_slot = ensure_aware(now, TZ_CN).strftime("%Y-%m-%dT%H")
                self.store.enqueue_job(
                    f"profile:{identity.code}:{identity.share_class}:{hour_slot}",
                    "profile",
                    identity.product_id,
                    identity.code,
                    _identity_payload(identity),
                    stamp,
                )

    def run_pending(self, now: datetime, max_jobs: int = 5) -> int:
        stamp = iso_now(now)
        done = 0
        while done < max_jobs:
            jobs = self.store.claim_jobs(stamp, 1)
            if not jobs:
                break
            job = jobs[0]
            try:
                self._handle(job, now)
                self.store.finish_job(job["idempotency_key"], stamp)
            except Exception as exc:
                message = scrub_secret(str(exc), self.store.secret_ai_settings().get("api_key", ""))
                attempts = int(job.get("attempts") or 1)
                delay = min(6 * 60, 2 ** min(attempts, 8))
                retry_at = iso_now(ensure_aware(now, TZ_CN) + timedelta(minutes=delay))
                self.store.fail_job(job["idempotency_key"], message, retry_at, stamp)
                self.store.record_profile_failure(job.get("code") or "", "", message, stamp)
            done += 1
        return done

    def ensure_worker(self) -> None:
        """只启动一次后台执行线程。tick 本身仍然只负责入队。"""
        if not self.jobs_enabled():
            return
        with self._worker_guard:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="fund-estimate",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        while self.jobs_enabled():
            try:
                processed = self.run_pending(datetime.now(TZ_CN), max_jobs=1)
            except Exception:
                logger.warning("基金估值任务执行失败", exc_info=True)
                processed = 0
            if processed == 0:
                time_module.sleep(2)

    def read_snapshots(self, product_ids, now: datetime) -> dict[str, dict]:
        identities = discover_funds(self.repository, now)
        allowed = {item.product_id for item in identities}
        if product_ids is not None:
            allowed &= set(product_ids)
        stored = self.store.latest_snapshots(list(allowed))
        session = intraday_session(now, self.bond_trading_day) and _inside_quote_window(now)
        result = {}
        for identity in identities:
            if identity.product_id not in allowed:
                continue
            payload = stored.get(identity.product_id) or snapshot_pending(identity, now.date().isoformat(), iso_now(now), ['history_backfill_pending']).to_dict()
            payload = present_snapshot(payload, now, self.config, session)
            # 旧版本曾隐藏跨源警告，切换后不能继续显示旧的跨源数值。
            if 'basis_unverified' in (payload.get('reason_codes') or []):
                payload = dict(payload, status='reference', return_pct=None, lower_pct=None, upper_pct=None,
                               interval_kind='none', confidence_label='同类参考')
            payload = estimate_holding_amount(payload, identity, self.repository)
            product = self.repository.get_product(identity.product_id)
            payload['name'] = product.name if product is not None else identity.code
            result[identity.product_id] = payload
        return result

    def save_model_settings(self, body: dict, now: datetime) -> dict:
        base_url = chat_completions_url(str(body.get("base_url") or "")) if str(body.get("base_url") or "").strip() else ""
        if base_url:
            base_url = str(body.get("base_url") or "").strip()
            chat_completions_url(base_url)
        api_key = body.get("api_key")
        if api_key is not None:
            api_key = str(api_key)
        return self.store.save_ai_settings(
            enabled=bool(body.get("enabled")),
            jobs_enabled=bool(body.get("jobs_enabled")),
            base_url=str(body.get("base_url") or "").strip(),
            model=str(body.get("model") or "").strip(),
            api_key=api_key,
            clear_api_key=bool(body.get("clear_api_key")),
            updated_at=iso_now(now),
        )

    def test_model_connection(self, body: dict) -> dict:
        stored = self.store.secret_ai_settings()
        base_url = str(body.get("base_url") or stored.get("base_url") or "")
        model = str(body.get("model") or stored.get("model") or "")
        api_key = str(body.get("api_key") or "") or stored.get("api_key") or ""
        return self.ai.test_connection(base_url, api_key, model)

    def status(self) -> dict:
        jobs = self.store.list_jobs()
        failed = [item for item in jobs if item["status"] == "failed"]
        secret = self.store.secret_ai_settings().get("api_key", "")
        last_error = scrub_secret(failed[-1]["error"], secret) if failed else ""
        return {
            "jobs_enabled": self.jobs_enabled(),
            "queued": sum(item["status"] == "queued" for item in jobs),
            "running": sum(item["status"] == "running" for item in jobs),
            "failed": len(failed),
            "last_error": last_error,
            "active_funds": len(discover_funds(self.repository)),
        }

    def _handle(self, job: dict, now: datetime) -> None:
        identity = _identity_from_payload(job["payload"])
        if job["job_type"] in {"backfill", "nav_poll"}:
            self._backfill(identity, now, enqueue_train=True)
        elif job["job_type"] == "profile":
            profile = self.profiles.refresh(identity, now)
            if profile and profile.document_hash:
                self.store.enqueue_job(
                    f"train:{identity.code}:{identity.share_class}:profile:{profile.document_hash}",
                    "train",
                    identity.product_id,
                    identity.code,
                    _identity_payload(identity),
                    iso_now(now),
                )
        elif job["job_type"] == "train":
            train_models(identity, self.store, now, self.config, source_family=self.config.get('source_family', CFETS_REALTIME))
        elif job["job_type"] == "intraday_snapshot":
            self._snapshot(identity, now)
        else:
            raise RuntimeError("未知估值任务")

    def _backfill(self, identity: FundIdentity, now: datetime, enqueue_train: bool) -> None:
        today = ensure_aware(now, TZ_CN).date()
        navs = self.sources.fetch_nav_history(identity, default_history_start(today), today)
        previous = {item.nav_date: _nav_economic_content(item) for item in self.store.nav_as_of(identity.code, iso_now(now))}
        for item in navs:
            self.store.save_nav(item)
        factor_changes = []
        try:
            factor_changes = self._sync_daily_factors(today, iso_now(now))
        except Exception as exc:
            self.store.set_watermark(f"factors:{identity.code}", str(exc)[:200], iso_now(now))
        changed = [item for item in navs if previous.get(item.nav_date) != _nav_economic_content(item)]
        if enqueue_train and (changed or factor_changes):
            newest = max((item.nav_date for item in navs), default=today.isoformat())
            version = hashlib.sha256(json.dumps([[_nav_economic_content(item) for item in changed], factor_changes], sort_keys=True).encode()).hexdigest()[:16]
            self.store.enqueue_job(
                f"train:{identity.code}:{identity.share_class}:nav:{newest}:{version}",
                "train",
                identity.product_id,
                identity.code,
                _identity_payload(identity),
                iso_now(now),
            )
            # realized_return(D) 依赖 nav_{D-1} 与 nav_D：被改日期自身和它的
            # 后一净值日都要重算真值，否则修订 D-1 会留下陈旧的 D 日验证结果。
            nav_dates = sorted({item.nav_date for item in navs})
            affected = set()
            for item in changed:
                affected.add(item.nav_date)
                later = [day for day in nav_dates if day > item.nav_date]
                if later:
                    affected.add(min(later))
            for nav_date in sorted(affected):
                self._verify(identity, nav_date, now)

    def _sync_daily_factors(self, today: date, stamp: str) -> list[str]:
        """同源历史按小批次回补；所有基金共享公开曲线和新版本触发。

        批次游标只表达推进进度；临时网络故障的日期进入可重试水位，
        不随游标推进被永久漏掉，来源本就无数据的日期不进水位。
        """
        family = self.config.get('source_family', CFETS_REALTIME)
        changes = []
        retry_key = f'{family}_retry_days'
        try:
            pending = set(json.loads(self.store.get_watermark(retry_key) or '[]'))
        except ValueError:
            pending = set()  # 水位损坏时重建，不能让回补永久卡死

        def pull(start, finish):
            got = []
            for item in self.sources.fetch_daily_factors(start, finish, family):
                if self.store.save_factor(item):
                    got.append(item.source_version)
            return got, {d.isoformat() for d in self.sources.take_transient_failures()}

        try:
            for day_text in sorted(pending):
                if day_text >= today.isoformat():
                    continue  # 当日缺口由近14天窗口滚动重试
                got, transient = pull(date.fromisoformat(day_text), date.fromisoformat(day_text))
                changes.extend(got)
                pending |= transient
                if day_text in transient:
                    continue  # 仍临时失败，保留水位下次再试
                pending.discard(day_text)

            recent_start = today - timedelta(days=14)
            got, transient = pull(recent_start, today)
            changes.extend(got)
            pending |= transient

            history_start = today - timedelta(days=int(self.config.get('factor_history_days', 180)))
            cursor_key = f'{family}_before'
            cursor = self.store.get_watermark(cursor_key)
            end = today if not cursor else date.fromisoformat(cursor) - timedelta(days=1)
            if end >= history_start:
                batch_start = max(history_start, end - timedelta(days=30))
                got, transient = pull(batch_start, end)
                changes.extend(got)
                pending |= transient
                self.store.set_watermark(cursor_key, batch_start.isoformat(), stamp)
        finally:
            # 半途异常也要留下已收集的临时失败日，不能把洞丢在内存里。
            self.store.set_watermark(retry_key, json.dumps(sorted(pending)), stamp)
        if changes:
            old = self.store.get_watermark(f'factor-revision:{family}') or ''
            revision = hashlib.sha256(json.dumps([old, sorted(changes)]).encode()).hexdigest()[:20]
            self.store.set_watermark(f'factor-revision:{family}', revision, stamp)
        return changes

    def _verify(self, identity: FundIdentity, nav_date: str, now: datetime) -> None:
        navs = self.store.nav_as_of(identity.code, iso_now(now))
        current = next((item for item in navs if item.nav_date == nav_date), None)
        previous = None
        for item in navs:
            if item.nav_date < nav_date:
                previous = item
        if current is None or previous is None or current.cash_dividend_per_share is None:
            return
        from src.fund_estimate_sources import total_return_pct

        try:
            realized = total_return_pct(previous.unit_nav, current.unit_nav, current.cash_dividend_per_share)
        except Exception:
            return
        for snapshot in self.store.snapshots_for_trade_date(identity.product_id, nav_date):
            self.store.save_verification(
                product_id=identity.product_id,
                code=identity.code,
                trade_date=nav_date,
                snapshot_generated_at=snapshot.get("generated_at") or "",
                realized_return_pct=realized,
                recorded_at=iso_now(now),
            )

    def _snapshot(self, identity: FundIdentity, now: datetime) -> None:
        factors = self.sources.read_intraday_factors(now)
        if self.sources.clock is not None:
            now = self.sources.clock()
        navs = self.store.nav_as_of(identity.code, iso_now(now))
        base = navs[-1] if navs else None
        if base is not None:
            from src.fund_estimate_sources import align_southern_basis
            factors = align_southern_basis(factors, base.nav_date, self.bond_trading_day)
        reference = None
        if self.repository is not None:
            reference = _saved_market_reference(self.repository)
        snapshot = project_snapshot(identity, self.store, factors, base, now, self.config, reference)
        slot = ensure_aware(now, TZ_CN).strftime("%Y-%m-%dT%H:%M")
        self.store.save_snapshot(snapshot, slot=slot, source_version=factors.source_version or slot)

    def _profile_due(self, identity: FundIdentity, now: datetime) -> bool:
        raw = self.store.get_watermark(f"profile-checked:{identity.code}:{identity.share_class}")
        last = parse_time(raw) if raw else None
        return profile_check_due(now, last, self.bond_trading_day)


def create_fund_estimate_service(portfolio_runtime, config=None, *, path=None):
    """只构建对象和本地库，不访问网络。"""
    try:
        store = FundEstimateStore(path or _DEFAULT_PATH)
        store.initialize()
    except Exception:
        logger.warning("基金估值库不可用，正式功能保持原状", exc_info=True)
        return None
    repository = getattr(portfolio_runtime, "repository", None)
    settings = dict(DEFAULT_CONFIG)
    raw = getattr(config, "fund_estimate", None) or {}
    if isinstance(raw, dict):
        settings.update(raw)
    settings['source_family'] = CFETS_REALTIME
    curve_source = CfetsCurveSource(store)
    sources = EstimateSources(
        dividend_fetcher=live_dividend_coverage,
        factor_fetcher=curve_source.fetch_daily_factors,
        intraday_reader=curve_source.read_intraday_factors,
        clock=curve_source.clock,
        transient_reader=curve_source.take_transient_failures,
    )
    from src.fund_estimate_documents import PublicFundDocuments
    profiles = ProfileService(store, PublicFundDocuments(store), ai=EstimateAi(store))
    return FundEstimateService(repository, store, sources, profiles, settings, ai=EstimateAi(store))


def read_published_estimates(repository, now: datetime | None = None) -> dict:
    if not _DEFAULT_PATH.exists():
        return {}
    try:
        store = FundEstimateStore(_DEFAULT_PATH)
        service = FundEstimateService(repository, store, EstimateSources(), ProfileService(store, _EmptyDocuments()))
        return service.read_snapshots(None, now or datetime.now(TZ_CN))
    except Exception:
        logger.warning("读取基金估值快照失败", exc_info=True)
        return {}


class _EmptyDocuments:
    def list_documents(self, identity, since=None):
        return []


def _saved_intraday(repository, now: datetime) -> FactorObservation:
    payload = _barometer_payload(repository)
    if payload is None:
        return FactorObservation(
            factor_set_id="bond_factor_v1",
            source_family="southern_intraday",
            as_of=ensure_aware(now, TZ_CN).date().isoformat(),
            published_at=None,
            fetched_at=iso_now(now),
            market_as_of=None,
            values_bp={name: None for name in FACTOR_NAMES},
            calendar_days=None,
            basis_status="missing",
            missing_fields=list(FACTOR_NAMES),
            basis_date=None,
            source_version="missing",
        )
    return factors_from_southern_barometer(payload, fetched_at=iso_now(now))


def _saved_market_reference(repository):
    return category_market_reference(_barometer_payload(repository))


def _barometer_payload(repository):
    if repository is None:
        return None
    try:
        from src.bond_barometer import read_snapshot

        with repository.database.connection() as conn:
            snapshot = read_snapshot(conn)
    except Exception:
        return None
    if not snapshot:
        return None
    return snapshot.get("payload")


def _identity_payload(identity: FundIdentity) -> dict:
    return identity.to_dict()


def _identity_from_payload(payload: dict) -> FundIdentity:
    return FundIdentity(
        product_id=payload["product_id"],
        code=payload["code"],
        provider=payload.get("provider") or "",
        share_class=payload.get("share_class") or "",
        portfolio_group_id=payload.get("portfolio_group_id"),
        fund_type=payload.get("fund_type") or "",
        currency=payload.get("currency") or "CNY",
    )


def _metadata(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _nav_economic_content(item) -> tuple:
    return (item.nav_date, item.unit_nav, item.cash_dividend_per_share,
            item.dividend_coverage, item.event_status)


def estimate_holding_amount(payload, identity, repository) -> dict:
    """只给已确认份额计算参考金额，不写账本或把待确认资金算成持仓。"""
    item = dict(payload)
    shares = repository.get_position(identity.product_id).total_shares
    pending = any(tx.transaction_type in PURCHASE_TYPES and tx.status in PENDING_STATUSES
                  for tx in repository.list_transactions(identity.product_id))
    held = shares > 0
    # “买入待确认”标记只给纯新买（无已确认份额）；已持仓基金不再重复标记。
    item.update(estimated_profit=None, profit_currency=identity.currency, pending_purchase=bool(pending and not held),
                holding_status=('held_with_pending_purchase' if pending else 'held') if held else 'pending_purchase',
                profit_reason='estimate_unavailable', profit_basis='confirmed_shares_previous_nav')
    if shares <= 0:
        item['profit_reason'] = 'awaiting_confirmed_shares'
        return item
    if item.get('return_pct') in (None, '') or item.get('base_nav') in (None, ''):
        return item
    try:
        nav = Decimal(str(item['base_nav']))
        rate = Decimal(str(item['return_pct']))
        if not (shares.is_finite() and nav.is_finite() and rate.is_finite()) or nav <= 0:
            return item
        amount = (shares * nav * rate / Decimal(100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        item['estimated_profit'] = format(abs(amount) if amount == 0 else amount, '.2f')
        item['profit_reason'] = ''
    except (InvalidOperation, ValueError, TypeError):
        pass
    return item


def _inside_quote_window(now: datetime) -> bool:
    clock = ensure_aware(now, TZ_CN).time()
    return (time(9, 30) <= clock <= time(11, 30)) or (time(13, 0) <= clock <= time(17, 0))
