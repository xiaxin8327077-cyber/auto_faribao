"""扩展债券因子和候选模型的并行验证，不激活主模型、不修改正式预估。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json

from src.beijing_time import TZ_CN
from src.fund_estimate_models import (
    _predict_number, _training_slice, _visible_profile, build_training_set, fit_baseline, fit_ridge,
)
from src.fund_estimate_sources import total_return_pct
from src.fund_estimate_store import iso_now, parse_time
from src.fund_estimate_types import FACTOR_NAMES, ModelVersion, decimal_text, ensure_aware, pct_text


def research_metrics(pairs, *, stress_return_pct='0.10') -> dict:
    """pairs 为 (日期, 预估百分数, 实际百分数)，零收益也计入方向分母。"""
    count = len(pairs)
    if not count:
        return dict(count=0, prediction_dates=[], mae_bp=None, bias_bp=None,
                    direction_accuracy=None, stress_count=0, stress_mae_bp=None, max_error_bp=None)
    errors = [hat-actual for _, hat, actual in pairs]
    stress = [abs(hat-actual) for _, hat, actual in pairs if abs(actual) >= Decimal(stress_return_pct)]
    hits = sum((hat > 0)-(hat < 0) == (actual > 0)-(actual < 0) for _, hat, actual in pairs)
    denominator = Decimal(count)
    return dict(count=count, prediction_dates=[day for day, _, _ in pairs],
                mae_bp=decimal_text(sum(map(abs, errors))/denominator*100),
                bias_bp=decimal_text(sum(errors)/denominator*100),
                direction_accuracy=decimal_text(Decimal(hits)/denominator),
                stress_count=len(stress), stress_mae_bp=decimal_text(sum(stress)/len(stress)*100) if stress else None,
                max_error_bp=decimal_text(max(map(abs, errors))*100))


def _windows(config):
    values = sorted(set(max(2, int(w)) for w in config.get('research_windows', (20, 60, 120))))
    if len(values) != 3:
        raise ValueError('research_windows 需要三个不同的短、中、长窗口')
    return values


def _bundle(parts):
    return {'components': [{'weight': str(weight), 'model': model.to_dict()} for weight, model in parts]}


def candidate_prediction(bundle, features) -> Decimal:
    result = Decimal(0)
    for part in bundle['components']:
        model = ModelVersion(**part['model'])
        result += Decimal(part['weight']) * _predict_number(model, features)
    return result


def fit_research_models(training, config=None, profile=None):
    config = config or {}
    short, medium, long = _windows(config)
    if len(training.rows) < long:
        return {}
    fits = {}
    for size in (short, medium, long):
        subset = _training_slice(training, training.rows[-size:])
        fits[size] = fit_ridge(subset, dict(config, half_life_days=max(5, size/2)))
    middle = _training_slice(training, training.rows[-medium:])
    baseline = fit_baseline(middle, None, config)
    legacy = fit_ridge(replace(middle, feature_names=FACTOR_NAMES), config)
    stress = fit_ridge(middle, dict(config, stress_weight=3, stress_threshold_bp=2))
    visible_profile = _visible_profile(profile, training.cutoff_at)
    prior = fit_baseline(middle, visible_profile, config)
    return {
        'duration_baseline': _bundle([(1, baseline)]),
        'legacy_ridge': _bundle([(1, legacy)]),
        'extended_short': _bundle([(1, fits[short])]),
        'extended_medium': _bundle([(1, fits[medium])]),
        'extended_long': _bundle([(1, fits[long])]),
        'stress_weighted': _bundle([(1, stress)]),
        'blend_short_long': _bundle([(Decimal('0.5'), fits[short]), (Decimal('0.5'), fits[long])]),
        'profile_constrained': _bundle([(Decimal('0.8'), fits[medium]), (Decimal('0.2'), prior)]),
    }


def replay_research(training, config=None, *, mode='point_in_time', profile=None) -> dict:
    """每个预测只在此前已可见标签上拟合。回顾版本必须显式选择独立模式。"""
    config = config or {}
    if mode not in ('point_in_time', 'retrospective_current_versions'):
        raise ValueError('未知验证模式')
    rows = sorted(training.rows, key=lambda row: row['nav_date'])
    minimum = max(_windows(config))
    result = {}
    for end in range(minimum, len(rows)):
        row = rows[end]
        prediction_at = row.get('prediction_at') or row['available_at']
        if mode == 'point_in_time':
            if not row.get('availability_verified'):
                continue
            if parse_time(row.get('market_available_at') or prediction_at) > parse_time(prediction_at):
                continue
            visible = [r for r in rows[:end] if parse_time(r['available_at']) <= parse_time(prediction_at)]
        else:
            visible = rows[:end]
            prediction_at = row.get('economic_at') or prediction_at
        if len(visible) < minimum:
            continue
        fitted = _training_slice(training, visible[-minimum:], prediction_at)
        models = fit_research_models(fitted, config, profile)
        actual = Decimal(row['y'])
        result[row['nav_date']] = {name: (candidate_prediction(bundle, row['features']), actual)
                                   for name, bundle in models.items()}
    return result


def _evaluate_mode(predictions, config, *, verified):
    days = sorted(predictions)
    minimum = max(1, int(config.get('research_min_holdout', 20)))
    split = len(days)//2
    if split < minimum or len(days)-split < minimum:
        return dict(availability_verified=verified, selection_dates=[], evaluation_dates=[],
                    available_prediction_count=len(days), selected_strategy=None, candidates={},
                    reason='insufficient_independent_holdout')
    selection, evaluation = days[:split], days[split:]
    strategies = sorted(set.intersection(*(set(predictions[d]) for d in days)))
    selection_metrics = {name: research_metrics([(day, *predictions[day][name]) for day in selection])
                         for name in strategies}
    # 选型只用前半段；固定规则同时惩罚总体误差与大幅波动日误差。
    def score(name):
        metric = selection_metrics[name]
        return Decimal(metric['mae_bp']) + Decimal('0.25')*Decimal(metric['stress_mae_bp'] or metric['mae_bp']), name
    selected = min(strategies, key=score)
    return dict(availability_verified=verified, selection_dates=selection, evaluation_dates=evaluation,
                available_prediction_count=len(days), selected_strategy=selected,
                selection_metrics=selection_metrics,
                candidates={name: research_metrics([(day, *predictions[day][name]) for day in evaluation])
                            for name in strategies})


def evaluate_research(training, config=None, *, profile=None):
    config = config or {}
    strict = replay_research(training, config, mode='point_in_time', profile=profile)
    retrospective = replay_research(training, config, mode='retrospective_current_versions', profile=profile)
    return dict(code=training.identity.code, product_id=training.identity.product_id,
                source_family=training.source_family, history_count=len(training.rows),
                first_date=training.rows[0]['nav_date'] if training.rows else None,
                last_date=training.rows[-1]['nav_date'] if training.rows else None,
                factor_names=list(training.feature_names), gaps=training.gaps, training_windows=_windows(config),
                point_in_time=_evaluate_mode(strict, config, verified=True),
                retrospective=_evaluate_mode(retrospective, config, verified=False),
                promotion_allowed=False,
                limitation='回补历史只作回顾探索；真实盘中预测按时点独立累计，不自动替换主模型。')


class FundEstimateResearch:
    def __init__(self, store, source, config=None):
        self.store, self.source = store, source
        self.config = config or {}
        self.source_family = getattr(source, 'source_family', 'cfets_realtime_mean')
        self.comparisons = []

    def sync_history(self, now, *, full=False):
        """后台每次至多回补一个月；独立命令可显式补齐一年。缺口有单独重试水位。"""
        today = ensure_aware(now, TZ_CN).date()
        stamp = iso_now(now)
        key = 'research:'+self.source_family+':before'
        retry_key = 'research:'+self.source_family+':retry'
        pending = set(json.loads(self.store.get_watermark(retry_key) or '[]'))
        floor = today-timedelta(days=int(self.config.get('research_history_days', 400)))
        cursor = self.store.get_watermark(key)
        finish = today if full or not cursor else date.fromisoformat(cursor)-timedelta(days=1)
        start = floor if full else max(floor, finish-timedelta(days=30))
        ranges = [(today-timedelta(days=14), today)]
        if finish >= floor:
            ranges.append((start, finish))
        ranges += [(date.fromisoformat(day), date.fromisoformat(day)) for day in sorted(pending)
                   if floor <= date.fromisoformat(day) < today]
        changed = 0
        versions = []
        try:
            for begin, end in ranges:
                observations = self.source.fetch_daily_factors(begin, end, self.source_family)
                transient = {day.isoformat() for day in self.source.take_transient_failures()}
                pending = {day for day in pending if not begin <= date.fromisoformat(day) <= end} | transient
                for factor in observations:
                    if self.store.save_factor(factor):
                        changed += 1
                        versions.append(factor.source_version)
            if finish >= floor:
                self.store.set_watermark(key, start.isoformat(), stamp)
        finally:
            self.store.set_watermark(retry_key, json.dumps(sorted(pending)), stamp)
        if versions:
            revision_key = 'research:'+self.source_family+':revision'
            old = self.store.get_watermark(revision_key) or ''
            revision = hashlib.sha256(json.dumps([old, sorted(versions)]).encode()).hexdigest()[:24]
            self.store.set_watermark(revision_key, revision, stamp)
        return changed

    def train(self, identity, now):
        from src.fund_estimate_cfets_extended import EXTENDED_FACTOR_NAMES
        cutoff = iso_now(now)
        training = build_training_set(identity, cutoff, self.store, self.source_family,
            feature_names=getattr(self.source, 'factor_names', EXTENDED_FACTOR_NAMES),
            factor_set_id=getattr(self.source, 'factor_set_id', 'bond_factor_v2'))
        effective_config = dict(self.config)
        short_history = self.source_family == 'cfets_realtime_mean' and len(training.rows) < max(_windows(effective_config))
        if short_history:
            # 实时接口可用深度不足时单列短历史研究；不假装已经完成120日模型的评估。
            effective_config['research_windows'] = self.config.get('research_intraday_windows', (10, 20, 40))
            effective_config['research_min_holdout'] = self.config.get('research_intraday_min_holdout', 8)
        # 同一经济版本不重复回测；可见版本变化会改变行哈希，仍会重算。
        fingerprint = hashlib.sha256(json.dumps(training.rows, sort_keys=True).encode()).hexdigest()
        config_fingerprint = hashlib.sha256(json.dumps(effective_config, sort_keys=True, default=str).encode()).hexdigest()
        previous = next((r for r in self.store.read_research_reports() if r['product_id'] == identity.product_id
                         and r.get('source_family', 'cfets_realtime_mean') == self.source_family), None)
        profile = self.store.profile_as_of(identity.code, identity.share_class, cutoff)
        profile_version = profile.profile_version if profile else None
        if (previous and previous.get('training_fingerprint') == fingerprint
                and previous.get('profile_version') == profile_version
                and previous.get('config_fingerprint') == config_fingerprint):
            return previous
        report = evaluate_research(training, effective_config, profile=profile)
        report.update(training_fingerprint=fingerprint, profile_version=profile_version, config_fingerprint=config_fingerprint,
                      generated_at=cutoff, short_history=short_history,
                      models=fit_research_models(training, effective_config, profile))
        storage_id = identity.product_id + ':' + self.source_family
        self.store.save_research_report(storage_id, report, cutoff)
        return report

    def snapshot(self, identity, now, base_nav):
        now = ensure_aware(now, TZ_CN)
        slots = self.config.get('shadow_times', ('09:45', '11:30', '14:30', '16:30', '17:00'))
        slot = next((s for s in reversed(sorted(slots))
                     if 0 <= (now-parse_time(now.date().isoformat()+'T'+s+':00+08:00')).total_seconds() < 300), None)
        if slot is None or base_nav is None:
            return
        report = next((r for r in self.store.read_research_reports() if r['product_id'] == identity.product_id
                       and r.get('source_family', 'cfets_realtime_mean') == self.source_family), None)
        if not report or parse_time(report['generated_at']) > now or not report.get('models'):
            return
        factor = self.source.read_intraday_factors(now)
        if self.source.clock is not None:
            now = max(now, ensure_aware(self.source.clock(), TZ_CN))
        if (now-parse_time(now.date().isoformat()+'T'+slot+':00+08:00')).total_seconds() >= 300:
            return
        observed = (factor.fetched_at, factor.published_at, factor.market_as_of, base_nav.available_at)
        if (factor.basis_status in ('missing', 'unverified') or factor.basis_date != base_nav.nav_date
                or not factor.market_as_of or factor.as_of != now.date().isoformat()
                or any(parse_time(v) > now for v in observed if v)):
            return
        for name, bundle in report['models'].items():
            required = {f for part in bundle['components'] for f in part['model']['feature_names']}
            if any(factor.values_bp.get(f) in (None, '') for f in required if f != 'calendar_days'):
                continue
            features = dict(factor.values_bp, calendar_days=str(factor.calendar_days))
            if factor.calendar_days is None:
                continue
            self.store.save_research_prediction(dict(
                product_id=identity.product_id, code=identity.code, trade_date=factor.as_of,
                slot=slot, strategy=name, predicted_pct=pct_text(candidate_prediction(bundle, features)),
                generated_at=iso_now(now), market_as_of=factor.market_as_of,
                basis_status=factor.basis_status, source_version=factor.source_version,
                base_nav_date=base_nav.nav_date, base_nav=base_nav.unit_nav,
                training_fingerprint=report['training_fingerprint'],
            ))

    def reports(self, now=None):
        cutoff = iso_now(now or datetime.now(TZ_CN))
        reports = []
        for saved in self.store.read_research_reports():
            report = {key: value for key, value in saved.items() if key != 'models'}
            navs = self.store.nav_as_of(report['code'], cutoff)
            returns = {}
            for previous, current in zip(navs, navs[1:]):
                if (current.event_status == 'ordinary' and current.dividend_coverage not in ('missing', 'special_event')
                        and current.cash_dividend_per_share is not None):
                    returns[current.nav_date] = (previous, current, Decimal(total_return_pct(
                        previous.unit_nav, current.unit_nav, current.cash_dividend_per_share)))
            groups = {}
            live = (self.store.read_research_predictions(report['product_id'])
                    if report.get('source_family', 'cfets_realtime_mean') == 'cfets_realtime_mean' else [])
            for item in live:
                realized = returns.get(item['trade_date'])
                if realized is None:
                    continue
                previous, current, actual = realized
                # 预测必须在正式净值公布前保存，收益基准日必须与真实前净值一致。
                if (item['base_nav_date'] != previous.nav_date
                        or parse_time(item['generated_at']) >= parse_time(current.available_at)):
                    continue
                groups.setdefault(item['slot'], {}).setdefault(item['strategy'], []).append(
                    (item['trade_date'], Decimal(item['predicted_pct']), actual))
            report['live_by_slot'] = {slot: {name: research_metrics(pairs) for name, pairs in strategies.items()}
                                      for slot, strategies in groups.items()}
            reports.append(report)
        return reports
