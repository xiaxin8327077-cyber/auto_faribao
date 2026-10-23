from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from src.beijing_time import TZ_CN
from src.fund_estimate_models import (
    _metrics_from_pairs, build_training_set, fit_baseline, fit_ridge,
    predict, project_snapshot, train_models, walk_forward_validate,
)
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import (
    FACTOR_NAMES, FactorObservation, FundIdentity, NavObservation, ProfileVersion, TrainingSet,
)

NOW = datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN)
IDENTITY = FundIdentity('p-regression', '918273', 'eastmoney_fund', 'A', None, '债券型')


def training_rows(count=48):
    rows = []
    for i in range(count):
        day = (date(2026, 1, 1) + timedelta(days=i)).isoformat()
        gov = (i % 5) - 2
        slope = (i % 7) - 3
        rows.append({
            'nav_date': day, 'available_at': day + 'T20:00:00+08:00',
            'y': str(Decimal(slope) * Decimal('0.1') - Decimal(gov) * Decimal('0.02')),
            'features': dict(zip(FACTOR_NAMES, map(str, (gov, slope, 0, 0, 1)))),
            'source_version': 'factor-' + day,
        })
    return TrainingSet(IDENTITY, '2026-10-22T20:00:00+08:00', rows, FACTOR_NAMES,
                       [r['source_version'] for r in rows], None)


def store_of(tmp_path):
    store = FundEstimateStore(tmp_path / 'fund_estimation.db')
    store.initialize()
    return store


def live_factor(family='daily_close', status='native', at='2026-10-23T10:00:00+08:00'):
    return FactorObservation('bond_factor_v1', family, at[:10], at, at, at,
                             dict(zip(FACTOR_NAMES[:4], ('1', '0', '0', '0'))),
                             1, status, [], '2026-10-22', 'fixture')


def base_nav():
    return NavObservation('918273', '2026-10-22', '1', '0',
                          '2026-10-22T20:00:00+08:00', 'fixture', 'nav1',
                          'ordinary', 'verified_none')


def test_selected_window_is_the_final_fit_window(tmp_path, monkeypatch):
    # Full-history fitting would save 48 rows although the compared strategy uses 8.
    sample = training_rows()
    monkeypatch.setattr('src.fund_estimate_models.build_training_set', lambda *a, **k: sample)
    store = store_of(tmp_path)
    result = train_models(IDENTITY, store, NOW, {'min_fit_samples': 8, 'training_windows': (8,)})
    assert result['trained']
    for model in store.list_models(IDENTITY.code, IDENTITY.share_class):
        assert model.validation['sample_count'] == 8
        assert model.preprocessing['training_window'] == 8
        assert model.preprocessing['training_start'] == sample.rows[-8]['nav_date']
        assert model.source_versions == [r['source_version'] for r in sample.rows[-8:]]


def test_each_model_keeps_its_own_metrics_and_holdout_dates(tmp_path, monkeypatch):
    sample = training_rows()
    monkeypatch.setattr('src.fund_estimate_models.build_training_set', lambda *a, **k: sample)
    store = store_of(tmp_path)
    result = train_models(IDENTITY, store, NOW, {'min_fit_samples': 8, 'training_windows': (8, 16)})
    models = {m.model_kind: m for m in store.list_models(IDENTITY.code, IDENTITY.share_class)}
    base = models['duration_baseline'].validation
    ridge = models['weighted_ridge_net'].validation
    assert Decimal(base['mae_bp']) > Decimal(ridge['mae_bp']) * 2
    assert base['prediction_dates'] == ridge['prediction_dates']
    selection_dates = result['validation']['selection_prediction_dates']
    assert set(selection_dates).isdisjoint(ridge['prediction_dates'])
    assert max(selection_dates) < min(ridge['prediction_dates'])
    candidates = result['validation']['window_candidates']
    assert len({tuple(c['prediction_dates']) for c in candidates}) == 1
    assert result['selected'] == result['validation']['selected_model_kind']


def test_residual_direction_and_coverage_use_disjoint_chronological_samples():
    pairs = [(Decimal('0'), Decimal('0.2'))] * 10 + [(Decimal('0'), Decimal('0.8'))] * 10
    report = _metrics_from_pairs(pairs, [], [], 20, config={'min_oos_residuals': 5})
    assert report['residual_definition'] == 'actual_minus_predicted'
    assert report['residual_low_pct'] == '0.2'
    assert report['residual_high_pct'] == '0.2'
    assert report['bias_bp'] == '-50'
    assert report['interval_calibration_count'] == 10
    assert report['interval_evaluation_count'] == 10
    assert report['interval_coverage'] == '0'


def test_too_few_residuals_produces_no_empirical_interval():
    pairs = [(Decimal('0'), Decimal('0.2'))] * 6
    report = _metrics_from_pairs(pairs, [], [], 6, config={'min_oos_residuals': 5})
    assert report['residual_low_pct'] is None
    assert report['interval_coverage'] is None


@pytest.mark.parametrize('basis_status', ['native', 'verified_mapping'])
def test_cross_source_without_mapping_evidence_has_no_center(basis_status):
    model = fit_baseline(training_rows(), None)
    snapshot = predict(model, live_factor('southern_intraday', basis_status), base_nav(), None, NOW)
    assert snapshot.status == 'reference'
    assert snapshot.return_pct is None
    assert snapshot.interval_kind == 'none'
    assert 'basis_unverified' in snapshot.reason_codes


def test_daily_close_residuals_cannot_be_presented_as_intraday_coverage():
    model = fit_baseline(training_rows(), None)
    validation = _metrics_from_pairs([(Decimal('0'), Decimal('0.2'))] * 20, [], [], 20,
                                    config={'min_oos_residuals': 5})
    model = replace(model, validation=validation)
    snapshot = predict(model, live_factor(), base_nav(), None, NOW, {'min_oos_residuals': 5})
    assert snapshot.return_pct is not None
    assert snapshot.interval_kind == 'none'
    closed_at = '2026-10-23T20:00:00+08:00'
    closed = predict(model, live_factor(at=closed_at), base_nav(), None,
                     datetime(2026, 10, 23, 21, tzinfo=TZ_CN), {'min_oos_residuals': 5})
    assert closed.interval_kind == 'empirical'
    assert '日终' in closed.confidence_label
    assert Decimal(closed.lower_pct) - Decimal(closed.return_pct) == Decimal('0.2')


def test_close_reference_curve_cannot_borrow_daily_close_interval():
    model = fit_baseline(training_rows(), None)
    validation = _metrics_from_pairs([(Decimal('0'), Decimal('0.2'))] * 20, [], [], 20,
                                    config={'min_oos_residuals': 5})
    model = replace(model, validation=validation)
    # 收盘参考不是 17:00 日终定格输入，即使时点晚于 17:00 也不能借用日终残差区间。
    reference = live_factor(family='daily_close', status='close_reference', at='2026-10-23T17:26:00+08:00')
    snapshot = predict(model, reference, base_nav(), None,
                       datetime(2026, 10, 23, 17, 30, tzinfo=TZ_CN), {'min_oos_residuals': 5})
    assert snapshot.return_pct is not None
    assert snapshot.interval_kind == 'none'


def test_retraining_same_data_same_day_reuses_model_version(tmp_path, monkeypatch):
    sample = training_rows()
    monkeypatch.setattr('src.fund_estimate_models.build_training_set', lambda *a, **k: sample)
    store = store_of(tmp_path)
    config = {'min_fit_samples': 8, 'training_windows': (8,)}
    train_models(IDENTITY, store, NOW, config)
    before = {model.model_version for model in store.list_models(IDENTITY.code, IDENTITY.share_class)}
    # 同数据同日的重复训练只是时刻不同，不应产生新版本把模型表灌大。
    train_models(IDENTITY, store, NOW + timedelta(minutes=5), config)
    after = {model.model_version for model in store.list_models(IDENTITY.code, IDENTITY.share_class)}
    assert after == before


def test_future_model_cannot_predict_and_future_profile_cannot_supply_duration(tmp_path):
    model = fit_baseline(training_rows(), None)
    future = replace(model, available_at='2026-10-24T00:00:00+08:00')
    blocked = predict(future, live_factor(), base_nav(), None, NOW)
    assert blocked.return_pct is None
    assert blocked.status == 'reference'
    profile = ProfileVersion('future', IDENTITY, '2026-06-30',
                             '2026-10-24T00:00:00+08:00', '2026-10-24T00:00:00+08:00',
                             'https://example.test', 'h', {'duration_nav': '9'}, [], 'ok')
    shown = predict(model, live_factor(), base_nav(), profile, NOW)
    assert shown.interval_kind == 'none'
    assert shown.profile_as_of is None
    store = store_of(tmp_path)
    store.save_model(future, active=True)
    store.publish_profile(profile)
    store.set_watermark('training:918273:A', '{"rows": 48}', NOW.isoformat())
    projected = project_snapshot(IDENTITY, store, live_factor(), base_nav(), NOW, {'min_fit_samples': 8})
    assert projected.return_pct is None
    assert projected.profile_as_of is None


def test_same_coefficients_for_different_fund_or_training_window_do_not_collide():
    sample = training_rows(8)
    model = fit_baseline(sample, None)
    other_fund = replace(sample, identity=replace(IDENTITY, code='111111', product_id='other'))
    assert fit_baseline(other_fund, None).model_version != model.model_version
    copied_window = replace(sample, rows=sample.rows + sample.rows, source_versions=sample.source_versions)
    assert fit_baseline(copied_window, None).model_version != model.model_version

def test_models_preserve_their_own_signed_residual_series(tmp_path, monkeypatch):
    sample = training_rows()
    monkeypatch.setattr('src.fund_estimate_models.build_training_set', lambda *a, **k: sample)
    store = store_of(tmp_path)
    train_models(IDENTITY, store, NOW, {'min_fit_samples': 8, 'training_windows': (8,)})
    models = {m.model_kind: m for m in store.list_models(IDENTITY.code, IDENTITY.share_class)}
    base = models['duration_baseline'].validation
    ridge = models['weighted_ridge_net'].validation
    assert len(base['residuals_pct']) == base['oos_count']
    assert base['residuals_pct'] != ridge['residuals_pct']
    assert base['residual_definition'] == 'actual_minus_predicted'


def test_future_market_input_does_not_generate_a_point_estimate():
    model = fit_baseline(training_rows(), None)
    future = live_factor(at='2026-10-23T17:00:00+08:00')
    snapshot = predict(model, future, base_nav(), None, NOW)
    assert snapshot.status == 'reference'
    assert snapshot.return_pct is None


def test_short_history_keeps_honest_unvalidated_fit_window(tmp_path, monkeypatch):
    sample = training_rows(10)
    monkeypatch.setattr('src.fund_estimate_models.build_training_set', lambda *a, **k: sample)
    store = store_of(tmp_path)
    result = train_models(IDENTITY, store, NOW, {'min_fit_samples': 8, 'training_windows': (60,)})
    assert result['trained']
    assert result['validation']['oos_count'] == 0
    model = store.active_model(IDENTITY.code, IDENTITY.share_class)
    assert model.preprocessing['training_window'] == model.preprocessing['training_sample_count'] == 10

def mapping_evidence():
    return {
        'mapping_id': 'audited-mapping-1', 'from_source_family': 'southern_intraday',
        'to_source_family': 'daily_close', 'calibrated_at': '2026-10-22T20:00:00+08:00',
        'training_sample_count': 20, 'validation_sample_count': 10,
        'per_feature': {name: {'slope': '1', 'intercept_bp': '0', 'unit': 'bp'} for name in FACTOR_NAMES[:4]},
    }


@pytest.mark.parametrize('overrides', [
    {'per_feature': None}, {'per_feature': []},
    {'per_feature': {FACTOR_NAMES[0]: []}},
    {'calibrated_at': '2026-10-24T20:00:00+08:00'},
    {'training_sample_count': 19}, {'validation_sample_count': 9},
    {'to_source_family': 'other'}, {'from_source_family': 'other'},
])
def test_unverifiable_mapping_evidence_is_rejected(overrides):
    model = fit_baseline(training_rows(), None)
    live = live_factor('southern_intraday', 'verified_mapping')
    object.__setattr__(live, 'mapping_evidence', dict(mapping_evidence(), **overrides))
    snapshot = predict(model, live, base_nav(), None, NOW)
    assert snapshot.return_pct is None
    assert snapshot.status == 'reference'
    assert 'basis_unverified' in snapshot.reason_codes


def test_audited_mapping_allows_center_but_cannot_reuse_daily_close_interval():
    model = fit_baseline(training_rows(), None)
    validation = _metrics_from_pairs([(Decimal('0'), Decimal('0.2'))] * 20, [], [], 20,
                                    config={'min_oos_residuals': 5})
    model = replace(model, validation=validation)
    live = live_factor('southern_intraday', 'verified_mapping')
    object.__setattr__(live, 'mapping_evidence', mapping_evidence())
    snapshot = predict(model, live, base_nav(), None, NOW, {'min_oos_residuals': 5})
    assert snapshot.return_pct is not None
    assert snapshot.status == 'experimental'
    assert snapshot.interval_kind == 'none'


def test_legacy_residual_metadata_cannot_claim_ready():
    model = fit_baseline(training_rows(), None)
    validation = {
        'source_family': 'daily_close', 'validation_scope': 'intraday',
        'mae_bp': '0', 'direction_accuracy': '1', 'interval_coverage': '1',
        'oos_count': 100, 'intraday_sample_count': 100,
        'residual_low_pct': '-0.01', 'residual_high_pct': '0.01',
    }
    model = replace(model, validation=validation)
    snapshot = predict(model, live_factor(), base_nav(), None, NOW, {
        'enable_ready': True, 'max_mae_bp': 1, 'min_direction_accuracy': '0.5',
        'min_interval_coverage': '0.5',
    })
    assert snapshot.return_pct is not None
    assert snapshot.interval_kind == 'none'
    assert snapshot.status == 'experimental'

def test_training_label_waits_for_revised_base_nav_availability(tmp_path):
    store = store_of(tmp_path)
    previous = replace(base_nav(), nav_date='2026-01-01', unit_nav='1.1',
                       available_at='2026-10-22T20:00:00+08:00', source_version='revised')
    current = replace(base_nav(), nav_date='2026-01-02', unit_nav='1.2',
                      available_at='2026-01-02T20:00:00+08:00')
    store.save_nav(previous)
    store.save_nav(current)
    observed = live_factor(at='2026-01-02T17:00:00+08:00')
    store.save_factor(replace(observed, basis_date='2026-01-01'))
    sample = build_training_set(IDENTITY, NOW.isoformat(), store)
    assert sample.rows[0]['available_at'] == '2026-10-22T20:00:00+08:00'

@pytest.mark.parametrize('kind,want', [('duration_baseline', '-0.36'), ('weighted_ridge_net', '12')])
def test_mapping_coefficients_transform_the_actual_prediction_input(kind, want):
    sample = training_rows()
    if kind == 'duration_baseline':
        model = replace(fit_baseline(sample, None), coefficients={'duration_nav': '3', 'carry_per_day': '0'})
    else:
        coefficients = {name: '0' for name in FACTOR_NAMES}
        coefficients.update(intercept='0', gov_3y_bp='1')
        preprocessing = dict(fit_ridge(sample).preprocessing,
                             mean={name: '0' for name in FACTOR_NAMES},
                             scale={name: '1' for name in FACTOR_NAMES})
        model = replace(fit_ridge(sample), coefficients=coefficients, preprocessing=preprocessing)
    evidence = mapping_evidence()
    evidence['per_feature']['gov_3y_bp'].update(slope='2', intercept_bp='10')
    live = replace(live_factor('southern_intraday', 'verified_mapping'), mapping_evidence=evidence)
    snapshot = predict(model, live, base_nav(), None, NOW)
    assert snapshot.return_pct == want
    assert snapshot.status == 'experimental'


def test_training_row_includes_market_version_availability(tmp_path):
    store = store_of(tmp_path)
    store.save_nav(replace(base_nav(), nav_date='2026-01-01', available_at='2026-01-01T20:00:00+08:00'))
    store.save_nav(replace(base_nav(), nav_date='2026-01-02', available_at='2026-01-02T20:00:00+08:00'))
    observed = replace(live_factor(at='2026-01-02T17:00:00+08:00'),
                       basis_date='2026-01-01', fetched_at='2026-10-22T20:00:00+08:00')
    store.save_factor(observed)
    sample = build_training_set(IDENTITY, NOW.isoformat(), store)
    row = sample.rows[0]
    assert row['available_at'] == row['market_available_at'] == '2026-10-22T20:00:00+08:00'
    assert row['economic_at'] == '2026-01-02T17:00:00+08:00'
    assert row['availability_verified'] is False


def test_strict_validation_excludes_future_market_revisions():
    from src.fund_estimate_models import _rolling_predictions
    sample = training_rows(16)
    for row in sample.rows:
        row.update(availability_verified=True, prediction_at=row['available_at'],
                   market_available_at=row['available_at'], economic_at=row['available_at'])
    sample.rows[0].update(available_at='2026-10-22T20:00:00+08:00',
                          market_available_at='2026-10-22T20:00:00+08:00', y='999')
    predictions = _rolling_predictions(sample, sample.rows, 8, {}, availability_mode='point_in_time')
    assert '2026-01-09' not in predictions  # Only seven prior rows were then visible.
    assert '2026-01-10' in predictions
    changed = replace(sample, rows=[dict(r) for r in sample.rows])
    changed.rows[0]['y'] = '-999'
    changed_predictions = _rolling_predictions(changed, changed.rows, 8, {}, availability_mode='point_in_time')
    assert predictions == changed_predictions


def test_backfilled_history_trains_with_explicit_retrospective_limits(tmp_path, monkeypatch):
    sample = training_rows(80)
    for row in sample.rows:
        row.update(available_at='2026-10-22T20:00:00+08:00',
                   market_available_at='2026-10-22T20:00:00+08:00',
                   prediction_at=row['nav_date'] + 'T17:00:00+08:00',
                   economic_at=row['nav_date'] + 'T17:00:00+08:00', availability_verified=False)
    monkeypatch.setattr('src.fund_estimate_models.build_training_set', lambda *a, **k: sample)
    store = store_of(tmp_path)
    result = train_models(IDENTITY, store, NOW, {'min_fit_samples': 60, 'training_windows': (60,)})
    assert result['trained'] is True
    report = result['validation']
    assert report['validation_availability'] == 'retrospective_current_versions'
    assert report['availability_verified'] is False
    assert report['point_in_time_oos_count'] == 0
    assert report['retrospective_oos_count'] == 10
    assert report['mae_bp'] is not None
    model = store.active_model(IDENTITY.code, IDENTITY.share_class)
    assert model.validation['validation_availability'] == 'retrospective_current_versions'
    snapshot = predict(model, live_factor(), base_nav(), None, NOW)
    assert snapshot.return_pct is not None
    assert snapshot.status == 'experimental'
    assert snapshot.interval_kind == 'none'


def test_retrospective_residuals_cannot_be_used_as_empirical_intervals():
    model = fit_baseline(training_rows(), None)
    validation = _metrics_from_pairs([(Decimal('0'), Decimal('0.2'))] * 20, [], [], 20,
                                    config={'min_oos_residuals': 5})
    validation.update(validation_availability='retrospective_current_versions', availability_verified=False)
    model = replace(model, validation=validation)
    closed_at = '2026-10-23T20:00:00+08:00'
    snapshot = predict(model, live_factor(at=closed_at), base_nav(), None,
                       datetime(2026, 10, 23, 21, tzinfo=TZ_CN), {'min_oos_residuals': 5})
    assert snapshot.interval_kind == 'none'
    assert snapshot.status == 'experimental'

def test_known_historical_versions_retain_strict_chronological_validation():
    sample = training_rows(80)
    for row in sample.rows:
        row.update(availability_verified=True, prediction_at=row['nav_date'] + 'T17:00:00+08:00',
                   market_available_at=row['nav_date'] + 'T17:00:00+08:00',
                   economic_at=row['nav_date'] + 'T17:00:00+08:00')
    report = walk_forward_validate(sample, {'min_fit_samples': 60, 'training_windows': (60,)})
    assert report['validation_availability'] == 'point_in_time'
    assert report['availability_verified'] is True
    assert report['point_in_time_oos_count'] == 10
    assert report['retrospective_oos_count'] == 10
    assert max(report['selection_prediction_dates']) < min(report['prediction_dates'])
