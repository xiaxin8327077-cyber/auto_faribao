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
_verification_service = None


def notify_official_nav_update() -> None:
    """正式净值提交后启动本地补齐；抓取与交易流程不等待验证。"""
    if _verification_service is not None:
        _verification_service.start_official_verification_sync(force=True)


def discover_funds(repository, now: datetime | None = None, *, errors=None) -> list[FundIdentity]:
    """持有份额大于 0，或存在有效待确认买入的公募基金。不按代码白名单过滤。"""
    del now
    if repository is None:
        return []
    found = []
    for product in repository.list_products():
        if product.product_type is not ProductType.PUBLIC_FUND:
            continue
        try:
            position = repository.get_position(product.id)
            transactions = repository.list_transactions(product.id)
        except Exception as exc:
            logger.warning('基金发现失败，继续处理其他基金: %s', product.code, exc_info=True)
            if errors is not None:
                errors.append({'product_id': product.id, 'code': str(product.code), 'error': str(exc)[:300]})
            continue
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
        self._official_sync_lock = threading.Lock()
        self._official_worker_guard = threading.Lock()
        self._official_worker = None
        self._official_dirty = False
        self._official_last_started = 0.0
        self._last_verification_report = None
        from src.fund_estimate_analysis import VerificationAnalysis
        self._analysis = VerificationAnalysis(store, self.ai)

    def sync_official_verifications(self, now: datetime | None = None) -> dict:
        from src.fund_estimate_verification import sync_confirmed_verifications
        current = now or datetime.now(TZ_CN)
        with self._official_sync_lock:
            generation = str(time_module.time_ns())
            self.store.set_watermark('official-nav-verification-generation:v1',
                                     'running:' + generation, iso_now(current))
            identities = discover_funds(self.repository, current)
            active_ids = {item.product_id for item in identities}
            historical_ids = {row['product_id'] for row in
                              self.store.read_daily_verifications() + self.store.read_verifications()}
            for product_id in sorted(historical_ids - active_ids):
                product = self.repository.get_product(product_id) if self.repository is not None else None
                if product is not None and product.product_type is ProductType.PUBLIC_FUND:
                    identities.append(FundIdentity(product.id, product.code, product.provider,
                        discover_share_class(product.name, _metadata(product.metadata_json)),
                        None, product.name, product.currency or 'CNY'))
            metadata = sync_confirmed_verifications(
                self.repository, self.store, identities, current, active_product_ids=active_ids)
            self.store.set_watermark('official-nav-verification-meta:v1',
                                     json.dumps(metadata, ensure_ascii=False), iso_now(current))
            self.store.set_watermark('official-nav-verification-generation:v1',
                                     'ready:' + generation, iso_now(current))
            return metadata

    def start_official_verification_sync(self, now: datetime | None = None, *, force=False) -> None:
        if self.repository is None or not hasattr(self.repository, 'list_quotes'):
            return
        with self._official_worker_guard:
            if self._official_worker is not None and self._official_worker.is_alive():
                self._official_dirty = True
                return
            clock = time_module.monotonic()
            if not force and clock - self._official_last_started < 5:
                return
            self._official_last_started = clock

            def work():
                current = now
                while True:
                    try:
                        self.sync_official_verifications(current)
                    except Exception:
                        logger.warning('正式净值验证补齐失败', exc_info=True)
                    with self._official_worker_guard:
                        if not self._official_dirty:
                            self._official_worker = None
                            return
                        self._official_dirty = False
                    current = None

            self._official_worker = threading.Thread(target=work, daemon=True,
                                                     name='official-nav-verification')
            self._official_worker.start()

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

    def verification_report(self) -> dict:
        """失败保留持久化的最后成功报告；明细不等待 AI。"""
        cache_key = 'verification-report:latest:v1'
        try:
            previous = self._last_verification_report
            if previous is None:
                cached = self.store.get_watermark(cache_key)
                previous = json.loads(cached) if cached else None
                self._last_verification_report = previous
            report = self._build_verification_report()
            if not report['items'] and previous and previous.get('items'):
                self._last_verification_report = previous
                return dict(previous, status='stale', message='正在恢复验证数据，保留上次内容',
                            refresh_complete=False, next_refresh_at=None)
            serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
            if self.store.get_watermark(cache_key) != serialized:
                self.store.set_watermark(cache_key, serialized, report['generated_at'])
            self._last_verification_report = report
            return report
        except Exception:
            logger.warning('验证报告读取失败，尝试恢复最后成功内容', exc_info=True)
            previous = self._last_verification_report
            if previous is None:
                try:
                    cached = self.store.get_watermark(cache_key)
                    previous = json.loads(cached) if cached else None
                except Exception:
                    previous = None
            if previous and previous.get('items'):
                return dict(previous, status='stale', message='更新失败，已保留上次数据',
                            refresh_complete=False, next_refresh_at=None)
            raise

    def _build_verification_report(self) -> dict:
        """预估与真实涨跌对比：确定性统计为准，AI只做点评且不改动任何数字。"""
        from src.fund_estimate_types import decimal_text

        generation_key = 'official-nav-verification-generation:v1'
        generation_before = self.store.get_watermark(generation_key)
        meta_before = self.store.get_watermark('official-nav-verification-meta:v1')
        rows = self.store.read_verifications()
        snapshot_items = []
        names = {}
        for row in rows:
            realized = row.get('realized_return_pct')
            if realized in (None, ''):
                continue
            predicted = row.get('predicted')
            product_id = row['product_id']
            if product_id not in names:
                product = self.repository.get_product(product_id) if self.repository is not None else None
                names[product_id] = product.name if product is not None else row['code']
            entry = {'product_id': product_id, 'name': names[product_id],
                     'code': row['code'], 'trade_date': row['trade_date'],
                     'snapshot_generated_at': row['snapshot_generated_at'],
                     'predicted_pct': predicted, 'realized_pct': realized,
                     'error_pct': None, 'direction_ok': None}
            if predicted not in (None, ''):
                error = Decimal(str(predicted)) - Decimal(str(realized))
                entry['error_pct'] = decimal_text(error)
                if (Decimal(str(predicted)) > 0) - (Decimal(str(predicted)) < 0) == \
                        (Decimal(str(realized)) > 0) - (Decimal(str(realized)) < 0):
                    entry['direction_ok'] = True
                else:
                    entry['direction_ok'] = False
            snapshot_items.append(entry)
        # 每只基金每天只验证最后一次有效预估；分钟采样频率不应改变统计权重。
        daily = {}
        for entry in snapshot_items:
            key = (entry['product_id'], entry['trade_date'])
            current = daily.get(key)
            rank = (entry['predicted_pct'] not in (None, ''), entry['snapshot_generated_at'])
            current_rank = ((current['predicted_pct'] not in (None, ''), current['snapshot_generated_at'])
                            if current is not None else None)
            if current_rank is None or rank > current_rank:
                daily[key] = entry
        for entry in self.store.read_daily_verifications():
            daily[(entry['product_id'], entry['trade_date'])] = entry
        items = sorted(daily.values(), key=lambda item: (item['trade_date'], item['code'], item['product_id']))
        stats = _verification_statistics(items)
        overall = _verification_overall(items, stats)
        # AI 仅接收涨跌幅和误差；持仓份额及收益金额不进入模型请求或缓存键。
        now = datetime.now(TZ_CN)
        analysis_items = items
        try:
            enriched = self._verification_amounts(items, items + rows)
        except Exception:
            enriched = []
            for item in items:
                try:
                    enriched.extend(self._verification_amounts([item], items + rows))
                except Exception:
                    logger.warning('基金验证金额补充失败: %s', item.get('code'), exc_info=True)
                    enriched.append(dict(item, predicted_profit=None, realized_profit=None,
                                         profit_error=None, profit_status='error'))
        items = enriched
        latest = max((item['trade_date'] for item in items), default=None)
        today = now.date().isoformat()
        version = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
        raw_meta = self.store.get_watermark('official-nav-verification-meta:v1')
        generation_after = self.store.get_watermark(generation_key)
        publication_pending = (generation_before != generation_after or meta_before != raw_meta
                               or bool(generation_after and generation_after.startswith('running:')))
        metadata = json.loads(raw_meta) if raw_meta else {}
        current_state = {}
        active_identities = []
        if self.repository is not None and hasattr(self.repository, 'list_quotes') and hasattr(self.repository, 'list_products'):
            from src.fund_estimate_verification import read_official_nav_state
            discovery_errors = []
            active_identities = discover_funds(self.repository, now, errors=discovery_errors)
            current_state = read_official_nav_state(self.repository, active_identities, now)
            current_state['errors'].extend(discovery_errors)
        shown_today = sum(item['trade_date'] == today for item in items)
        confirmed_today = max(shown_today, current_state.get('today_confirmed_count', 0))
        errors = list(metadata.get('errors') or []) + list(current_state.get('errors') or [])
        errors.extend({'product_id': item['product_id'], 'error': 'profit_enrichment_failed'}
                      for item in items if item.get('profit_status') == 'error')
        version_pending = bool(current_state.get('confirmed_nav_version') and
            current_state['confirmed_nav_version'] != metadata.get('processed_active_nav_version'))
        status = 'stale' if items and errors else 'updating' if errors or version_pending or publication_pending or confirmed_today > shown_today else 'ready' if items else 'empty'
        if version_pending or errors or not items:
            self.start_official_verification_sync()
        if not items and self._official_worker is not None and self._official_worker.is_alive():
            status = 'updating'
        from src.fund_estimate_verification import verification_refresh_state
        # Each data version already has a deterministic summary even if the
        # model failed. Reuse it until the input data changes; resuming NAV
        # polling must not restart analysis of the same version.
        analysis = self._analysis.read(analysis_items, stats, overall, retry_failed=False)
        refresh_state = verification_refresh_state(
            active_identities, current_state, items, now,
            is_trading_day=self.bond_trading_day, analysis_status=analysis.get('ai_status'),
            status=status, version_pending=version_pending)
        return {'items': items, 'stats': stats, 'snapshot_items': snapshot_items,
                'overall': overall,
                **analysis,
                **refresh_state,
                'status': status,
                'message': '部分基金正在重新核验，已保留现有内容' if errors else '',
                'data_version': version,
                'confirmed_nav_version': current_state.get('confirmed_nav_version', metadata.get('confirmed_nav_version', version)),
                'latest_available_date': latest, 'display_date': latest,
                'today_date': today,
                'today_confirmed_count': confirmed_today,
                'generated_at': iso_now(now)}

    def _verification_amounts(self, items, snapshots):
        """按对应披露日的收益份额还原金额，沿用正式收益账本口径。"""
        from src.portfolio_profit import _nav_earning_shares, calculate_latest_profit

        snapshot_by_key = {(row['product_id'], row['snapshot_generated_at']): row for row in snapshots}
        holdings = {}
        result = []
        for item in items:
            entry = dict(item, predicted_profit=None, realized_profit=None, profit_error=None)
            result.append(entry)
            if self.repository is None or not all(hasattr(self.repository, name) for name in
                                                  ('get_product', 'list_transactions', 'list_quotes')):
                continue
            product_id = item['product_id']
            if product_id not in holdings:
                product = self.repository.get_product(product_id)
                if product is None or product.currency != 'CNY':
                    holdings[product_id] = None
                else:
                    holdings[product_id] = (product, self.repository.list_transactions(product_id=product_id),
                                           self.repository.list_quotes(product_id))
            if holdings[product_id] is None:
                continue
            product, transactions, quotes = holdings[product_id]
            day = date.fromisoformat(item['trade_date'])
            current = next((quote for quote in reversed(quotes) if quote.quote_date == day and quote.unit_nav is not None), None)
            previous = next((quote for quote in reversed(quotes) if quote.quote_date < day and quote.unit_nav is not None), None)
            if current is None or previous is None:
                continue
            profit_date, realized = calculate_latest_profit(self.repository, product, day,
                bundle_non_trading_days=False, transactions=transactions, quotes=quotes)
            dividend_ready = _dividend_ledger_matches(self.repository, item, transactions)
            if not dividend_ready:
                entry['profit_status'] = 'pending_dividend_ledger'
            if profit_date == day and realized is not None and item['realized_pct'] is not None and dividend_ready:
                entry['realized_profit'] = format(realized.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
            snapshot = snapshot_by_key.get((product_id, item['snapshot_generated_at']), {})
            prediction_base = snapshot.get('snapshot_base_nav', snapshot.get('base_nav'))
            if item['predicted_pct'] in (None, '') or prediction_base in (None, ''):
                continue
            shares = _nav_earning_shares(self.repository, product_id, previous.quote_date, day,
                product=product, transactions=transactions, current_nav=current.unit_nav)
            predicted = shares * Decimal(str(prediction_base)) * Decimal(str(item['predicted_pct'])) / 100
            entry['predicted_profit'] = format(predicted.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f')
            if entry['realized_profit'] is not None:
                entry['profit_error'] = format(abs(Decimal(entry['predicted_profit']) - Decimal(entry['realized_profit'])), '.2f')
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
        # 验证独立于训练：净值没变化也要补齐历史漏配和新出现的快照。
        # realized_return(D) 依赖 nav_{D-1} 与 nav_D，前一净值修订也要重算 D。
        nav_dates = sorted({item.nav_date for item in navs})
        affected = set(self.store.pending_verification_dates(identity.product_id))
        for item in changed:
            affected.add(item.nav_date)
            later = [day for day in nav_dates if day > item.nav_date]
            if later:
                affected.add(min(later))
        # available_at 是抓取完成时间；任务启动时的 now 看不到刚保存的新净值。
        completed_at = self.sources.clock() if self.sources.clock is not None else datetime.now(TZ_CN)
        verification_now = max(ensure_aware(now, TZ_CN), ensure_aware(completed_at, TZ_CN))
        for nav_date in sorted(affected):
            self._verify(identity, nav_date, verification_now)

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
    global _verification_service
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
    curve_source = CfetsCurveSource(store, intraday_stale_minutes=settings.get('intraday_stale_minutes', 15))
    sources = EstimateSources(
        dividend_fetcher=live_dividend_coverage,
        factor_fetcher=curve_source.fetch_daily_factors,
        intraday_reader=curve_source.read_intraday_factors,
        clock=curve_source.clock,
        transient_reader=curve_source.take_transient_failures,
    )
    from src.fund_estimate_documents import PublicFundDocuments
    profiles = ProfileService(store, PublicFundDocuments(store), ai=EstimateAi(store))
    service = FundEstimateService(repository, store, sources, profiles, settings, ai=EstimateAi(store))
    _verification_service = service
    service.start_official_verification_sync(force=True)
    return service


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


def _dividend_ledger_matches(repository, item: dict, transactions) -> bool:
    """有明确分红时，正式金额必须等同日分红流水确认后才能作比较。"""
    declared = Decimal(str(item.get('cash_dividend_per_share') or '0'))
    if declared <= 0:
        return True
    database = getattr(repository, 'database', None)
    if database is None:
        return False
    with database.connection() as conn:
        events = conn.execute(
            'SELECT per_share, error, dividend_transaction_id FROM fund_dividend_events '
            'WHERE product_id=? AND ex_dividend_date=?',
            (item['product_id'], item['trade_date'])).fetchall()
    confirmed = {tx.id for tx in transactions if tx.status is TransactionStatus.CONFIRMED
                 and tx.transaction_type is TransactionType.CASH_DIVIDEND}
    return any(not row['error'] and Decimal(row['per_share']) == declared
               and row['dividend_transaction_id'] in confirmed for row in events)


def _verification_statistics(items: list[dict]) -> dict:
    from src.fund_estimate_types import decimal_text

    errors = [Decimal(item['error_pct']) for item in items if item['error_pct'] is not None]
    count = len(errors)
    hits = sum(item['direction_ok'] is True for item in items)
    return {
        'pairs': len(items),
        'predicted_count': count,
        'missing_predictions': sum(item.get('predicted_pct') in (None, '') for item in items),
        'pending_returns': sum(item.get('realized_pct') in (None, '') for item in items),
        'direction_hits': hits,
        'direction_accuracy': decimal_text(Decimal(hits) / Decimal(count)) if count else None,
        'mae_pct': decimal_text(sum((abs(error) for error in errors), Decimal(0)) / Decimal(count)) if count else None,
        'bias_pct': decimal_text(sum(errors, Decimal(0)) / Decimal(count)) if count else None,
    }


def _verification_overall(items: list[dict], stats: dict) -> dict:
    by_day, by_fund = {}, {}
    for item in items:
        by_day.setdefault(item['trade_date'], []).append(item)
        by_fund.setdefault(item['product_id'], []).append(item)
    dates = sorted(by_day)
    summary = f"已覆盖{len(dates)}个净值日、{len(by_fund)}只基金，累计完成{stats['predicted_count']}条最终预估对比。"
    if stats['bias_pct'] is not None:
        bias = Decimal(stats['bias_pct'])
        summary += '整体预估偏高。' if bias > 0 else '整体预估偏低。' if bias < 0 else '平均偏差为零。'
    if len(dates) == 1:
        summary += '目前仅有一天数据，长期表现仍需继续观察。'
    return {
        'start_date': dates[0] if dates else None,
        'end_date': dates[-1] if dates else None,
        'day_count': len(dates),
        'fund_count': len(by_fund),
        'summary': summary if dates else None,
        'diagnostics': _verification_diagnostics(items, stats),
        'by_day': [dict(trade_date=day, **_verification_statistics(by_day[day])) for day in dates],
        'by_fund': [dict(product_id=pid, code=rows[0]['code'], name=rows[0]['name'],
                         **_verification_statistics(rows)) for pid, rows in sorted(by_fund.items())],
    }


def _verification_diagnostics(items: list[dict], stats: dict) -> dict:
    from src.fund_estimate_types import decimal_text

    valid = [item for item in items if item['error_pct'] is not None]
    count = len(valid)
    if not count:
        return {}
    denominator = Decimal(count)
    errors = [Decimal(item['error_pct']) for item in valid]
    realized = [Decimal(str(item['realized_pct'])) for item in valid]
    predicted = [Decimal(str(item['predicted_pct'])) for item in valid]
    actual_movement = sum((abs(value) for value in realized), Decimal(0)) / denominator
    up = Decimal(sum(value > 0 for value in realized)) / denominator
    down = Decimal(sum(value < 0 for value in realized)) / denominator
    flat = Decimal(sum(value == 0 for value in realized)) / denominator
    baseline = max(up, down, flat)
    mae = Decimal(stats['mae_pct'])
    return {
        'positive_errors': sum(value > 0 for value in errors),
        'negative_errors': sum(value < 0 for value in errors),
        'zero_errors': sum(value == 0 for value in errors),
        'mean_abs_realized_pct': decimal_text(actual_movement),
        'mean_abs_predicted_pct': decimal_text(sum((abs(value) for value in predicted), Decimal(0)) / denominator),
        'mae_bp': decimal_text(mae * Decimal(100)),
        'bias_bp': decimal_text(Decimal(stats['bias_pct']) * Decimal(100)),
        'mean_abs_realized_bp': decimal_text(actual_movement * Decimal(100)),
        'error_to_realized_movement_ratio': decimal_text(mae / actual_movement) if actual_movement else None,
        'always_up_accuracy': decimal_text(up),
        'always_down_accuracy': decimal_text(down),
        'majority_direction_accuracy': decimal_text(baseline),
        'direction_accuracy_above_baseline': decimal_text(Decimal(stats['direction_accuracy']) - baseline),
    }


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
