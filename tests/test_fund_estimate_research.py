"""研究模型必须在真实时点之外保留回顾标识，不能反写主预测。"""
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.beijing_time import TZ_CN
from src.fund_estimate_models import build_training_set, fit_ridge
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FACTOR_NAMES, FundIdentity, NavObservation, FactorObservation, TrainingSet


IDENTITY = FundIdentity('research-test', '918273', 'eastmoney_fund', 'A', None, '债券型')
FEATURES = FACTOR_NAMES + ('gov_1y_bp', 'gov_2y_bp', 'gov_5y_bp', 'credit_aaa_spread_5y_bp')
NOW = datetime(2026, 11, 2, 18, tzinfo=TZ_CN)


def sample(count=100, retrospective=False):
    rows = []
    for i in range(count):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        gov, credit = (i % 7) - 3, (i % 5) - 2
        features = {name: '0' for name in FEATURES}
        features.update(gov_3y_bp=str(gov), gov_1y_bp=str(gov / 3),
                        gov_2y_bp=str(gov / 2), gov_5y_bp=str(gov),
                        credit_aaa_spread_5y_bp=str(credit), calendar_days='1')
        rows.append(dict(nav_date=day, available_at=day+'T20:00:00+08:00',
                         market_available_at=day+'T17:00:00+08:00',
                         prediction_at=day+'T17:00:00+08:00', economic_at=day+'T17:00:00+08:00',
                         availability_verified=not retrospective, features=features,
                         y=str(Decimal('0.01')-Decimal(gov)*Decimal('0.03')-Decimal(credit)*Decimal('0.05')),
                         source_version='factor-'+day))
    return TrainingSet(IDENTITY, NOW.isoformat(), rows, FEATURES,
                       [r['source_version'] for r in rows], None, source_family='cfets_realtime_mean')


def config():
    return {'research_windows': (8, 16, 24), 'research_min_holdout': 8}


def test_training_set_requires_every_requested_extended_factor(tmp_path):
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    for day, nav in [('2026-10-29', '1'), ('2026-10-30', '0.999')]:
        store.save_nav(NavObservation(IDENTITY.code, day, nav, '0', day+'T20:00:00+08:00',
                                      'fixture', day, 'ordinary', 'verified_none'))
    values = {name: '0' for name in FACTOR_NAMES}
    store.save_factor(FactorObservation('bond_factor_v2', 'cfets_realtime_mean', '2026-10-30',
                                       NOW.isoformat(), NOW.isoformat(), '2026-10-30T17:00:00+08:00',
                                       values, 1, 'native', [], '2026-10-29', 'missing-extended'))
    training = build_training_set(IDENTITY, NOW.isoformat(), store,
                                  'cfets_realtime_mean', feature_names=FEATURES)
    assert training.rows == []
    assert training.gaps[-1]['reason'] == 'factor_missing'


def test_ridge_uses_the_declared_training_features():
    model = fit_ridge(sample())
    assert 'credit_aaa_spread_5y_bp' in model.coefficients
    from src.fund_estimate_models import _predict_number
    row = sample().rows[-1]
    assert abs(_predict_number(model, row['features'])-Decimal(row['y'])) < Decimal('0.02')


def test_extended_archive_cannot_replace_primary_training_observations(tmp_path):
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    for day, value in [('2026-10-29', '1'), ('2026-10-30', '0.999')]:
        store.save_nav(NavObservation(IDENTITY.code, day, value, '0', day+'T20:00:00+08:00',
                                     'fixture', day, 'ordinary', 'verified_none'))
    values = dict.fromkeys(FACTOR_NAMES, '0')
    original = FactorObservation('bond_factor_v1', 'cfets_realtime_mean', '2026-10-30',
        '2026-10-30T17:00:00+08:00', '2026-10-30T17:00:00+08:00',
        '2026-10-30T17:00:00+08:00', values, 1, 'native', [], '2026-10-29', 'v1')
    store.save_factor(original)
    store.save_factor(replace(original, factor_set_id='bond_factor_v2', fetched_at=NOW.isoformat(),
                              published_at=NOW.isoformat(), source_version='v2',
                              values_bp=dict(values, gov_3y_bp='99')))
    training = build_training_set(IDENTITY, NOW.isoformat(), store, 'cfets_realtime_mean')
    assert training.rows[0]['features']['gov_3y_bp'] == '0'
    assert training.rows[0]['availability_verified'] is True


def test_research_selection_and_holdout_are_disjoint_and_future_labels_do_not_choose_strategy():
    from src.fund_estimate_research import evaluate_research
    training = sample()
    report = evaluate_research(training, config())
    strict = report['point_in_time']
    assert strict['availability_verified'] is True
    assert strict['selection_dates'] and strict['evaluation_dates']
    assert max(strict['selection_dates']) < min(strict['evaluation_dates'])
    dates = {tuple(row['prediction_dates']) for row in strict['candidates'].values()}
    assert dates == {tuple(strict['evaluation_dates'])}
    mutated = replace(training, rows=[dict(r, y='9') if r['nav_date'] in strict['evaluation_dates'] else r
                                      for r in training.rows])
    second = evaluate_research(mutated, config())
    assert second['point_in_time']['selected_strategy'] == strict['selected_strategy']
    assert report['promotion_allowed'] is False
    assert 'blend_short_long' in strict['candidates']
    assert 'stress_weighted' in strict['candidates']


def test_backfill_is_never_labelled_point_in_time():
    from src.fund_estimate_research import evaluate_research
    report = evaluate_research(sample(retrospective=True), config())
    assert report['point_in_time']['evaluation_dates'] == []
    assert report['point_in_time']['selected_strategy'] is None
    assert report['retrospective']['evaluation_dates']
    assert report['retrospective']['availability_verified'] is False
    assert report['promotion_allowed'] is False


def test_future_revision_cannot_enter_an_earlier_strict_fit():
    from src.fund_estimate_research import replay_research
    original = sample()
    rows = [dict(r) for r in original.rows]
    rows[10] = dict(rows[10], available_at='2026-10-31T20:00:00+08:00', y='900')
    replay = replay_research(replace(original, rows=rows), config(), mode='point_in_time')
    day = rows[40]['nav_date']
    clean = replace(original, rows=[r for i, r in enumerate(original.rows) if i != 10])
    expected = replay_research(clean, config(), mode='point_in_time')
    assert replay[day]['legacy_ridge'] == expected[day]['legacy_ridge']


def test_research_report_and_predictions_never_replace_the_primary_model(tmp_path):
    from src.fund_estimate_research import FundEstimateResearch
    from src.fund_estimate_models import fit_baseline
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    model = fit_baseline(sample(), None); store.save_model(model, active=True)
    store.save_research_report(IDENTITY.product_id, {'code': IDENTITY.code, 'status': 'research'}, NOW.isoformat())
    row = dict(product_id=IDENTITY.product_id, code=IDENTITY.code, trade_date='2026-11-02',
               slot='14:30', strategy='legacy_ridge', predicted_pct='0.01', generated_at=NOW.isoformat())
    assert store.save_research_prediction(row)
    assert not store.save_research_prediction(dict(row, predicted_pct='99'))
    assert store.read_research_predictions(IDENTITY.product_id)[0]['predicted_pct'] == '0.01'
    assert store.active_model(IDENTITY.code, 'A').model_version == model.model_version
    assert store.latest_snapshots() == {}


def test_metrics_keep_stress_days_and_flat_returns():
    from src.fund_estimate_research import research_metrics
    result = research_metrics([('2026-10-29', Decimal('0.02'), Decimal('0')),
                               ('2026-10-30', Decimal('-0.03'), Decimal('-0.28'))])
    assert Decimal(result['mae_bp']) == Decimal('13.5')
    assert Decimal(result['stress_mae_bp']) == Decimal('25')
    assert result['stress_count'] == 1
    assert Decimal(result['direction_accuracy']) == Decimal('0.5')


def test_live_samples_keep_slots_separate_and_reject_predictions_after_nav(tmp_path):
    from src.fund_estimate_research import FundEstimateResearch
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    store.save_research_report(IDENTITY.product_id,
        {'product_id': IDENTITY.product_id, 'code': IDENTITY.code, 'models': {}}, NOW.isoformat())
    for day, value in [('2026-10-29', '1'), ('2026-10-30', '0.999')]:
        store.save_nav(NavObservation(IDENTITY.code, day, value, '0', day+'T20:00:00+08:00',
                                     'fixture', day, 'ordinary', 'verified_none'))
    for slot, value, at in [('09:45', '-0.01', '09:45'), ('16:30', '-0.09', '16:30'),
                            ('17:00', '9', '20:01')]:
        store.save_research_prediction(dict(product_id=IDENTITY.product_id, code=IDENTITY.code,
            trade_date='2026-10-30', slot=slot, strategy='legacy_ridge', predicted_pct=value,
            generated_at='2026-10-30T'+at+':00+08:00', base_nav_date='2026-10-29'))
    report = FundEstimateResearch(store, None).reports(NOW)[0]
    assert set(report['live_by_slot']) == {'09:45', '16:30'}
    assert Decimal(report['live_by_slot']['09:45']['legacy_ridge']['mae_bp']) == Decimal('9')
    assert Decimal(report['live_by_slot']['16:30']['legacy_ridge']['mae_bp']) == Decimal('1')


def test_training_job_also_builds_shadow_research_without_activating_it(tmp_path, monkeypatch):
    from src.fund_estimate_research import FundEstimateResearch
    from src.fund_estimate_service import FundEstimateService
    from src.fund_estimate_profiles import ProfileService, MemoryDocuments
    from src.fund_estimate_sources import EstimateSources
    from src.fund_estimate_models import fit_baseline
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    primary = fit_baseline(sample(), None); store.save_model(primary, active=True)
    monkeypatch.setattr('src.fund_estimate_research.build_training_set', lambda *a, **k: sample())
    research = FundEstimateResearch(store, None, config())
    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()),
                                   research=research)
    monkeypatch.setattr('src.fund_estimate_service.train_models', lambda *a, **k: None)
    service._handle({'job_type': 'train', 'payload': IDENTITY.to_dict()}, NOW)
    service._research_executor.submit(lambda: None).result(timeout=15)
    assert store.read_research_reports()[0]['history_count'] == 100
    assert store.active_model(IDENTITY.code, 'A').model_version == primary.model_version


def test_real_curve_fetch_completion_time_does_not_discard_live_shadow_samples(tmp_path):
    from src.fund_estimate_cfets_extended import ExtendedCfetsCurveSource
    from src.fund_estimate_research import FundEstimateResearch, fit_research_models
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    entry = datetime(2026, 11, 2, 14, 30, tzinfo=TZ_CN)
    completed = entry+timedelta(seconds=1)
    training = replace(sample(), cutoff_at='2026-11-02T14:29:00+08:00')
    store.save_research_report(IDENTITY.product_id, dict(product_id=IDENTITY.product_id,
        code=IDENTITY.code, generated_at=training.cutoff_at, training_fingerprint='f',
        models=fit_research_models(training, config())), training.cutoff_at)
    def fetch(kind, day, clock):
        return {'data': {'date': day.isoformat(), 'time': clock, 'data':
            [['', '', '', str(term), '', '', '2.0', ''] for term in (1, 2, 3, 5, 10)]}}
    source = ExtendedCfetsCurveSource(store, fetcher=fetch, clock=lambda: completed)
    base = NavObservation(IDENTITY.code, '2026-10-30', '1', '0', '2026-10-30T20:00:00+08:00',
                          'fixture', 'nav', 'ordinary', 'verified_none')
    FundEstimateResearch(store, source).snapshot(IDENTITY, entry, base)
    rows = store.read_research_predictions(IDENTITY.product_id)
    assert len(rows) == 8
    assert rows[0]['generated_at'] == completed.isoformat()
    assert rows[0]['market_as_of'] == entry.isoformat()


def test_slow_research_history_does_not_block_primary_nav_job(tmp_path):
    import threading
    from src.fund_estimate_service import FundEstimateService
    from src.fund_estimate_profiles import ProfileService, MemoryDocuments
    from src.fund_estimate_sources import EstimateSources
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    class SlowResearch:
        source = None
        def sync_history(self, now):
            entered.set(); release.wait(10)
            return 0
        def train(self, *args):
            return None
    sources = EstimateSources(nav_fetcher=lambda *a: {'rows': [], 'total': 0},
                              factor_fetcher=lambda *a: [], clock=lambda: NOW)
    service = FundEstimateService(None, store, sources, ProfileService(store, MemoryDocuments()),
                                  research=SlowResearch())
    def run():
        service._backfill(IDENTITY, NOW, enqueue_train=False)
        returned.set()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        assert returned.wait(0.2), 'slow research blocked the primary NAV task'
    finally:
        release.set(); worker.join(timeout=5)


def test_same_fund_keeps_realtime_and_multirating_eod_research_separate(tmp_path, monkeypatch):
    from src.fund_estimate_research import FundEstimateResearch
    from src.fund_estimate_credit_eod import ChinaBondCreditSource
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    def training(identity, cutoff, store, family, **kwargs):
        return replace(sample(), source_family=family)
    monkeypatch.setattr('src.fund_estimate_research.build_training_set', training)
    FundEstimateResearch(store, None, config()).train(IDENTITY, NOW)
    FundEstimateResearch(store, ChinaBondCreditSource(store), config()).train(IDENTITY, NOW)
    reports = store.read_research_reports()
    assert {r['source_family'] for r in reports} == {'cfets_realtime_mean', 'chinabond_eod'}
    assert len(reports) == 2


def test_short_realtime_history_can_collect_shadow_samples_with_explicit_short_windows(tmp_path, monkeypatch):
    from src.fund_estimate_research import FundEstimateResearch
    store = FundEstimateStore(tmp_path/'e.db'); store.initialize()
    monkeypatch.setattr('src.fund_estimate_research.build_training_set', lambda *a, **k: sample(58))
    report = FundEstimateResearch(store, None).train(IDENTITY, NOW)
    assert report['models']
    assert report['training_windows'] == [10, 20, 40]
    assert report['short_history'] is True
    assert report['promotion_allowed'] is False
