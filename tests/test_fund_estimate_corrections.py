from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.beijing_time import TZ_CN
from src.fund_estimate_profiles import MemoryDocuments, ProfileService
from src.fund_estimate_service import FundEstimateService, create_fund_estimate_service
from src.fund_estimate_sources import EstimateSources
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FundIdentity, NavObservation
from src.portfolio_models import TransactionType, TransactionStatus


NOW = datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN)
IDENTITY = FundIdentity('p-new', '918273', 'eastmoney_fund', 'A', None, '债券型')


def store_at(tmp_path):
    store = FundEstimateStore(tmp_path / 'estimates.db')
    store.initialize()
    return store


def _wait_for_analysis(service):
    import time
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        report = service.verification_report()
        if report['ai_status'] != 'generating':
            return report
        time.sleep(0.005)
    pytest.fail('background analysis did not finish')


def observation():
    return NavObservation('918273', '2026-10-22', '1.0000', None,
                          '2026-10-22T20:00:00+08:00', 'fixture', 'nav-1', 'unknown', 'missing')


def test_failed_training_job_retries_only_after_due_time(tmp_path):
    store = store_at(tmp_path)
    stamp = NOW.isoformat()
    store.enqueue_job('train:new-nav', 'train', 'p-new', '918273', {}, stamp)
    assert len(store.claim_jobs(stamp, 1)) == 1
    retry = NOW + timedelta(minutes=2)
    store.fail_job('train:new-nav', 'temporary network error', retry.isoformat(), stamp)
    assert store.claim_jobs((NOW + timedelta(minutes=1)).isoformat(), 1) == []
    assert len(store.claim_jobs(retry.isoformat(), 1)) == 1


def test_dividend_coverage_recovery_keeps_previous_version_visible(tmp_path):
    store = store_at(tmp_path)
    missing = observation()
    store.save_nav(missing)
    recovered = replace(missing, cash_dividend_per_share='0', event_status='ordinary',
                        dividend_coverage='verified_none', available_at=NOW.isoformat())
    store.save_nav(recovered)
    assert store.nav_as_of('918273', '2026-10-23T08:00:00+08:00')[0].cash_dividend_per_share is None
    assert store.nav_as_of('918273', NOW.isoformat())[0].cash_dividend_per_share == '0'


def test_same_date_nav_revision_enqueues_new_training(tmp_path):
    store = store_at(tmp_path)
    initial = replace(observation(), cash_dividend_per_share='0', event_status='ordinary', dividend_coverage='verified_none')
    store.save_nav(initial)
    newer = replace(initial, unit_nav='1.001', source_version='nav-2', available_at=NOW.isoformat())
    sources = EstimateSources(nav_fetcher=lambda *_: {'total': 0, 'rows': []}, factor_fetcher=lambda *_: [])
    sources.fetch_nav_history = lambda *_: [newer]
    service = FundEstimateService(None, store, sources, ProfileService(store, MemoryDocuments()))
    service._backfill(IDENTITY, NOW, enqueue_train=True)
    assert any(job['job_type'] == 'train' for job in store.list_jobs('918273'))


def _verifications(store):
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return {row['trade_date']: float(row['realized_return_pct'])
                for row in conn.execute('SELECT trade_date, realized_return_pct FROM verifications')}


def test_previous_day_nav_revision_updates_next_day_verification(tmp_path):
    from src.fund_estimate_types import EstimateSnapshot
    store = store_at(tmp_path)
    older = replace(observation(), nav_date='2026-10-21', unit_nav='1.0000', source_version='nav-p',
                    cash_dividend_per_share='0', event_status='ordinary', dividend_coverage='verified_none',
                    available_at='2026-10-21T17:05:00+08:00')
    day_d = replace(older, nav_date='2026-10-22', unit_nav='1.0010', source_version='nav-d',
                    available_at='2026-10-22T17:05:00+08:00')
    snapshot = EstimateSnapshot('p-new', '918273', '2026-10-22', 'mark_to_market_total_return', 'experimental',
                                [], '2026-10-21', '1.0000', '2026-10-22T15:00:00+08:00',
                                None, None, None, 'm1', '0.10', None, None, 'none', '试验',
                                None, '2026-10-22T15:00:00+08:00')
    store.save_snapshot(snapshot, slot='2026-10-22T15:00', source_version='m1')

    def run(rows):
        sources = EstimateSources(nav_fetcher=lambda *_: {'total': 0, 'rows': []}, factor_fetcher=lambda *_: [])
        sources.fetch_nav_history = lambda *_: rows
        service = FundEstimateService(None, store, sources, ProfileService(store, MemoryDocuments()))
        service._backfill(IDENTITY, NOW, enqueue_train=True)

    run([older, day_d])
    assert _verifications(store)['2026-10-22'] == pytest.approx(0.1, abs=1e-3)
    # D-1 净值修订后，D 日 realized_return 依赖它，必须跟着更新而不是留下旧真值。
    revised = replace(older, unit_nav='1.0020', source_version='nav-p2', available_at=NOW.isoformat())
    run([revised, day_d])
    assert _verifications(store)['2026-10-22'] == pytest.approx(-0.0998, abs=1e-3)


def _verification_fixture(tmp_path, *, available_at):
    from src.fund_estimate_types import EstimateSnapshot

    store = store_at(tmp_path)
    previous = replace(observation(), cash_dividend_per_share='0', event_status='ordinary',
                       dividend_coverage='verified_none')
    current = replace(previous, nav_date='2026-10-23', unit_nav='1.0010',
                      source_version='nav-today', available_at=available_at)
    generated = '2026-10-23T15:00:00+08:00'
    snapshot = EstimateSnapshot('p-new', '918273', '2026-10-23', 'mark_to_market_total_return',
                                'experimental', [], '2026-10-22', '1.0000', generated,
                                None, None, None, 'm1', '0.08', None, None, 'none', '试验',
                                None, generated)
    store.save_nav(previous)
    store.save_snapshot(snapshot, slot='2026-10-23T15:00', source_version='m1')
    sources = EstimateSources(factor_fetcher=lambda *_: [], clock=lambda: NOW + timedelta(seconds=25))
    sources.fetch_nav_history = lambda *_: [previous, current]
    service = FundEstimateService(None, store, sources, ProfileService(store, MemoryDocuments()))
    return service, current


def test_new_nav_fetched_after_job_start_is_verified_immediately(tmp_path):
    fetched_at = NOW + timedelta(seconds=24)
    service, _ = _verification_fixture(tmp_path, available_at=fetched_at.isoformat())

    service._backfill(IDENTITY, NOW, enqueue_train=True)

    rows = service.store.read_verifications()
    assert len(rows) == 1
    assert float(rows[0]['realized_return_pct']) == pytest.approx(0.1)
    assert rows[0]['predicted'] == '0.08'
    assert datetime.fromisoformat(rows[0]['recorded_at']) >= fetched_at
    # 历史可见时间仍保持真实抓取时间，不能通过提前 available_at 绕开问题。
    assert all(item.nav_date != '2026-10-23'
               for item in service.store.nav_as_of(IDENTITY.code, NOW.isoformat()))


@pytest.mark.parametrize('enqueue_train', [True, False])
def test_unchanged_nav_recovers_missing_verification_without_retraining(tmp_path, enqueue_train):
    service, current = _verification_fixture(tmp_path, available_at=NOW.isoformat())
    service.store.save_nav(current)

    service._backfill(IDENTITY, NOW, enqueue_train=enqueue_train)
    service._backfill(IDENTITY, NOW + timedelta(minutes=30), enqueue_train=enqueue_train)

    rows = service.store.read_verifications()
    assert len(rows) == 1
    assert float(rows[0]['realized_return_pct']) == pytest.approx(0.1)
    assert not service.store.list_jobs(IDENTITY.code)


def test_missing_dividend_coverage_remains_pending_until_recovered(tmp_path):
    service, current = _verification_fixture(tmp_path, available_at=NOW.isoformat())
    service.sources.fetch_nav_history = lambda *_: [replace(current, cash_dividend_per_share=None,
                                                            dividend_coverage='missing')]
    service._backfill(IDENTITY, NOW, enqueue_train=False)
    assert service.store.read_verifications() == []

    service.sources.fetch_nav_history = lambda *_: [current]
    service._backfill(IDENTITY, NOW, enqueue_train=False)
    assert len(service.store.read_verifications()) == 1


def test_read_snapshots_strips_unverified_cross_source_numbers(tmp_path):
    from src.fund_estimate_types import EstimateSnapshot
    from src.portfolio_models import Position, Product, ProductType
    store = store_at(tmp_path)
    snapshot = EstimateSnapshot('p-new', '918273', '2026-10-23', 'mark_to_market_total_return', 'experimental',
                                ['basis_unverified'], '2026-10-22', '1.0000', '2026-10-23T10:00:00+08:00',
                                None, None, None, 'm1', '0.5', None, None, 'none', '试验',
                                None, '2026-10-23T10:00:00+08:00')
    store.save_snapshot(snapshot, slot='2026-10-23T10:00', source_version='m1')
    product = Product('p-new', 'eastmoney_fund', '918273', '稳健纯债A', ProductType.PUBLIC_FUND,
                      metadata_json='{"fund_type":"债券型"}')
    repo = SimpleNamespace(
        list_products=lambda: [product],
        get_product=lambda _pid: product,
        get_position=lambda _pid: Position('p-new', Decimal('0'), Decimal('0'), Decimal('10000'), Decimal('0')),
        list_transactions=lambda _pid: [])
    sources = EstimateSources(nav_fetcher=lambda *_: {'total': 0, 'rows': []}, factor_fetcher=lambda *_: [])
    service = FundEstimateService(repo, store, sources, ProfileService(store, MemoryDocuments()))
    payload = service.read_snapshots(None, NOW)['p-new']
    # 跨源未核验的历史快照在读路径必须剥掉中心数值，只给同类参考。
    assert payload['status'] == 'reference'
    assert payload['return_pct'] is None
    assert payload['lower_pct'] is None and payload['upper_pct'] is None
    assert payload['confidence_label'] == '同类参考'


def test_read_snapshots_keeps_value_and_marks_market_stale(tmp_path):
    from src.fund_estimate_types import EstimateSnapshot
    from src.portfolio_models import Position, Product, ProductType
    store = store_at(tmp_path)
    snapshot = EstimateSnapshot('p-new', '918273', '2026-10-23', 'mark_to_market_total_return', 'experimental',
                                [], '2026-10-22', '1.0000', '2026-10-23T09:30:00+08:00',
                                None, None, None, 'm1', '0.5', None, None, 'none', '试验',
                                None, '2026-10-23T09:30:00+08:00')
    store.save_snapshot(snapshot, slot='2026-10-23T09:30', source_version='m1')
    product = Product('p-new', 'eastmoney_fund', '918273', '稳健纯债A', ProductType.PUBLIC_FUND,
                      metadata_json='{"fund_type":"债券型"}')
    repo = SimpleNamespace(
        list_products=lambda: [product],
        get_product=lambda _pid: product,
        get_position=lambda _pid: Position('p-new', Decimal('0'), Decimal('0'), Decimal('10000'), Decimal('0')),
        list_transactions=lambda _pid: [])
    sources = EstimateSources(nav_fetcher=lambda *_: {'total': 0, 'rows': []}, factor_fetcher=lambda *_: [])
    service = FundEstimateService(repo, store, sources, ProfileService(store, MemoryDocuments()))
    now = datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN)  # 距快照行情时点 30 分钟
    payload = service.read_snapshots(None, now)['p-new']
    # 行情时点落后但快照有值：保留预估并标注行情陈旧，不能退回"数据准备中"。
    assert payload['return_pct'] == '0.5'
    assert payload['status'] == 'stale'
    assert payload['confidence_label'] == '陈旧'
    assert 'market_stale' in payload['reason_codes']


def test_verification_report_pairs_predictions_with_realized_and_stats(tmp_path):
    from src.fund_estimate_types import EstimateSnapshot
    store = store_at(tmp_path)

    def snap(day, gen, ret):
        return EstimateSnapshot('p-new', '918273', day, 'mark_to_market_total_return', 'experimental',
                                [], day, '1', gen, None, None, None, 'm', ret, None, None,
                                'none', '试验', None, gen)

    rows = [('2026-10-20', '0.10', '0.20'), ('2026-10-21', '0.10', '-0.20'),
            ('2026-10-22', None, '0.30'), ('2026-10-23', '0', '0')]
    for day, pred, real in rows:
        gen = f'{day}T15:00:00+08:00'
        store.save_snapshot(snap(day, gen, pred), slot=f'{day}T15:00', source_version='m')
        store.save_verification(product_id='p-new', code='918273', trade_date=day,
                                snapshot_generated_at=gen, realized_return_pct=real,
                                recorded_at=f'{day}T20:00:00+08:00')
    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()))
    report = service.verification_report()
    stats = report['stats']
    assert stats['pairs'] == 4
    assert stats['predicted_count'] == 3
    assert stats['missing_predictions'] == 1
    # 方向一致：0.10/0.20 同向、0/0 一致；0.10/-0.20 相反；缺预测不计方向。
    assert stats['direction_hits'] == 2
    assert float(stats['direction_accuracy']) == pytest.approx(2 / 3, abs=1e-3)
    assert float(stats['mae_pct']) == pytest.approx((0.10 + 0.30 + 0) / 3, abs=1e-3)
    assert float(stats['bias_pct']) == pytest.approx((-0.10 + 0.30 + 0) / 3, abs=1e-3)
    assert [item['trade_date'] for item in report['items']] == [r[0] for r in rows]
    assert report['ai_status'] == 'disabled'
    assert len(report['ai_analysis'].splitlines()) == 4


def test_verification_analysis_uses_ai_without_changing_numbers(tmp_path):
    from src.fund_estimate_types import EstimateSnapshot
    store = store_at(tmp_path)
    gen = '2026-10-22T15:00:00+08:00'
    snapshot = EstimateSnapshot('p-new', '918273', '2026-10-22', 'mark_to_market_total_return', 'experimental',
                                [], '2026-10-21', '1', gen, None, None, None, 'm', '0.10', None, None,
                                'none', '试验', None, gen)
    store.save_snapshot(snapshot, slot='2026-10-22T15:00', source_version='m')
    store.save_verification(product_id='p-new', code='918273', trade_date='2026-10-22',
                            snapshot_generated_at=gen, realized_return_pct='0.20',
                            recorded_at='2026-10-22T20:00:00+08:00')

    class FakeAi:
        def is_enabled(self):
            return True

        def complete(self, messages):
            assert '0.10' in messages[-1]['content']  # 数据必须进提示词
            return ('方向判断与同期间简单方向基线持平，仅反映当日，尚不足以判断未来表现，需要新增记录继续比较。\n'
                    '预估整体偏低，平均误差小于实际涨跌幅，应核对同类偏差是否在后续净值日重复出现。\n'
                    '当前只有一只基金可比较，该净值日的偏差是主要观察对象，具体原因仍需资料核验。\n'
                    '下一净值日继续检查低估是否重现，并重算同观察期方向基线，确认误差是否逐渐收敛。')

    service = FundEstimateService(None, store, EstimateSources(), ProfileService(store, MemoryDocuments()), ai=FakeAi())
    report = service.verification_report()
    report = _wait_for_analysis(service)
    assert report['ai_status'] == 'ready'
    assert len(report['ai_analysis'].splitlines()) == 4
    assert report['stats']['pairs'] == 1
    # AI 点评不改动任何统计数字。
    assert float(report['stats']['bias_pct']) == pytest.approx(-0.10, abs=1e-3)


def test_verification_report_counts_only_final_valid_prediction_per_fund_day(tmp_path):
    from src.fund_estimate_types import EstimateSnapshot
    store = store_at(tmp_path)
    rows = [
        ('2026-10-22', '09:00', None, '0.10'),
        ('2026-10-22', '10:00', '-0.20', '0.10'),
        ('2026-10-22', '17:00', '0.08', '0.10'),
        ('2026-10-22', '17:01', None, '0.10'),
        ('2026-10-23', '10:00', '0.30', '-0.20'),
        ('2026-10-23', '17:00', '-0.18', '-0.20'),
    ]
    for day, clock, predicted, realized in rows:
        generated = f'{day}T{clock}:00+08:00'
        snapshot = EstimateSnapshot('p-new', '918273', day, 'mark_to_market_total_return',
                                    'experimental', [], day, '1', generated, None, None, None,
                                    'm', predicted, None, None, 'none', '试验', None, generated)
        store.save_snapshot(snapshot, slot=f'{day}T{clock}', source_version='m')
        store.save_verification(product_id='p-new', code='918273', trade_date=day,
                                snapshot_generated_at=generated, realized_return_pct=realized,
                                recorded_at=f'{day}T20:00:00+08:00')
    ai_inputs = []
    ai = SimpleNamespace(is_enabled=lambda: True,
                         complete=lambda messages: ai_inputs.append(messages[-1]['content']) or '按每日最终预估分析。')
    repo = SimpleNamespace(get_product=lambda _: SimpleNamespace(name='测试纯债A'))
    service = FundEstimateService(repo, store, EstimateSources(), None, ai=ai)

    report = service.verification_report()

    _wait_for_analysis(service)
    assert report['stats']['pairs'] == 2
    assert report['stats']['predicted_count'] == 2
    assert report['stats']['missing_predictions'] == 0
    assert report['stats']['direction_hits'] == 2
    assert float(report['stats']['mae_pct']) == pytest.approx(0.02)
    assert float(report['stats']['bias_pct']) == pytest.approx(0)
    assert [item['predicted_pct'] for item in report['items']] == ['0.08', '-0.18']
    assert all(item['name'] == '测试纯债A' for item in report['items'])
    assert all('T17:00:00' in item['snapshot_generated_at'] for item in report['items'])
    assert '-0.20' not in ai_inputs[0].split('"predicted_pct"')[1].split('}')[0]
    assert '"pairs": 2' in ai_inputs[0]
    assert len(report['snapshot_items']) == 6


def test_overall_analysis_covers_all_dates_beyond_latest_twenty_results(tmp_path):
    import json
    from src.fund_estimate_types import EstimateSnapshot

    store = store_at(tmp_path)
    start = date(2026, 9, 1)
    for offset in range(25):
        day = (start + timedelta(days=offset)).isoformat()
        generated = f'{day}T17:00:00+08:00'
        predicted = '-1.0' if offset == 0 else '0.10'
        snapshot = EstimateSnapshot('p-new', '918273', day, 'mark_to_market_total_return',
                                    'experimental', [], day, '1', generated, None, None, None,
                                    'm', predicted, None, None, 'none', '试验', None, generated)
        store.save_snapshot(snapshot, slot=f'{day}T17:00', source_version='m')
        store.save_verification(product_id='p-new', code='918273', trade_date=day,
                                snapshot_generated_at=generated, realized_return_pct='0.20',
                                recorded_at=f'{day}T20:00:00+08:00')
    prompts = []
    ai = SimpleNamespace(is_enabled=lambda: True,
                         complete=lambda messages: prompts.append(messages[-1]['content']) or '累计分析。')
    service = FundEstimateService(None, store, EstimateSources(), None, ai=ai)

    report = service.verification_report()

    _wait_for_analysis(service)
    overall = report['overall']
    assert overall['start_date'] == '2026-09-01'
    assert overall['end_date'] == '2026-09-25'
    assert overall['day_count'] == 25
    assert overall['fund_count'] == 1
    assert len(overall['by_day']) == 25
    assert overall['by_day'][0]['direction_hits'] == 0
    assert float(overall['by_day'][0]['mae_pct']) == pytest.approx(1.2)
    assert overall['by_fund'][0]['predicted_count'] == 25
    assert float(report['stats']['mae_pct']) == pytest.approx(0.144)
    payload = json.loads(prompts[0].split('数据：', 1)[1])
    assert payload['by_day'][0]['trade_date'] == '2026-09-01'
    assert payload['by_fund'][0]['predicted_count'] == 25
    assert payload['period']['day_count'] == 25
    diagnostics = report['overall']['diagnostics']
    assert float(diagnostics['mean_abs_realized_pct']) == pytest.approx(0.2)
    assert float(diagnostics['error_to_realized_movement_ratio']) == pytest.approx(0.72)
    assert diagnostics['negative_errors'] == 25
    assert float(diagnostics['majority_direction_accuracy']) == pytest.approx(1)
    assert float(diagnostics['direction_accuracy_above_baseline']) == pytest.approx(-0.04)
    assert payload['diagnostics'] == diagnostics
    assert '120' in prompts[0] and '180' in prompts[0]


def test_production_service_uses_trading_calendar(tmp_path):
    service = create_fund_estimate_service(None, path=tmp_path / 'fund.db')
    assert service.bond_trading_day(date(2026, 10, 1)) is False
    assert service.bond_trading_day(date(2026, 10, 8)) is True


def test_estimated_profit_uses_only_confirmed_shares(tmp_path):
    from src.fund_estimate_service import estimate_holding_amount
    payload = {'return_pct': '-0.024', 'base_nav': '1.4636', 'base_nav_date': '2026-10-22'}
    repo = SimpleNamespace(get_position=lambda _: SimpleNamespace(total_shares=Decimal('10000')),
                           list_transactions=lambda _: [SimpleNamespace(transaction_type=TransactionType.MANUAL_PURCHASE,
                                                                        status=TransactionStatus.PENDING_CONFIRMATION)])
    result = estimate_holding_amount(payload, IDENTITY, repo)
    assert result['estimated_profit'] == '-3.51'
    # 已持仓基金即使有买入待确认也不显示“买入待确认”标记，标记只给纯新买。
    assert result['pending_purchase'] is False
    assert result['holding_status'] == 'held_with_pending_purchase'


def test_pending_purchase_without_confirmed_shares_has_no_profit_amount():
    from src.fund_estimate_service import estimate_holding_amount
    repo = SimpleNamespace(get_position=lambda _: SimpleNamespace(total_shares=Decimal('0')),
                           list_transactions=lambda _: [])
    result = estimate_holding_amount({'return_pct': '0.1', 'base_nav': '1'}, IDENTITY, repo)
    assert result['estimated_profit'] is None
    assert result['profit_reason'] == 'awaiting_confirmed_shares'


def _curve(day, time):
    return {'head': {'rep_code': '200'}, 'data': {'date': day, 'time': time, 'data': [
        ['', '', '', str(term), 'true', '', '', ''] for term in (3, 5, 10)
    ] + [['', '', '', str(term), '', '', value, ''] for term, value in ((3, '1.2'), (5, '1.4'), (10, '1.6'))]}}


def _service_with_curve_fetcher(store, fetch):
    from src.fund_estimate_cfets import CfetsCurveSource
    clock = [datetime(2026, 10, 23, 19, 0, tzinfo=TZ_CN)]
    curve_source = CfetsCurveSource(store, fetcher=fetch, clock=lambda: clock[0],
                                    calendar=lambda d: d.weekday() < 5)
    sources = EstimateSources(nav_fetcher=lambda *_: {'total': 0, 'rows': []},
                              factor_fetcher=curve_source.fetch_daily_factors,
                              transient_reader=curve_source.take_transient_failures)
    service = FundEstimateService(None, store, sources, ProfileService(store, MemoryDocuments()))
    return service, clock


def test_transient_gap_keeps_retry_watermark_until_recovery(tmp_path):
    import json
    import requests as rq
    from src.fund_estimate_cfets import CFETS_REALTIME
    store = store_at(tmp_path)
    failing = {'down': True}

    def fetch(kind, day, time):
        if failing['down'] and day == date(2026, 10, 9):
            raise rq.ConnectionError('network down')
        return _curve(day.isoformat(), time)

    service, clock = _service_with_curve_fetcher(store, fetch)
    service._sync_daily_factors(date(2026, 10, 23), clock[0].isoformat())
    # 批次游标照常推进，但临时失败日及其基准日缺口必须留下可重试水位，不能永久漏掉。
    assert json.loads(store.get_watermark(f'{CFETS_REALTIME}_retry_days') or '[]') == ['2026-10-09', '2026-10-12']
    assert store.get_watermark(f'{CFETS_REALTIME}_before')
    failing['down'] = False
    clock[0] += timedelta(minutes=35)  # 越过30分钟失败退避窗口
    service._sync_daily_factors(date(2026, 10, 23), clock[0].isoformat())
    assert json.loads(store.get_watermark(f'{CFETS_REALTIME}_retry_days') or '[]') == []
    factors = {item.as_of for item in store.factors_as_of(clock[0].isoformat(), CFETS_REALTIME)}
    assert {'2026-10-09', '2026-10-12'} <= factors


def test_permanent_missing_day_is_not_kept_in_retry_watermark(tmp_path):
    import json
    from src.fund_estimate_cfets import CFETS_REALTIME
    store = store_at(tmp_path)

    def fetch(kind, day, time):
        payload = _curve(day.isoformat(), time)
        if day == date(2026, 10, 9):
            payload['data']['data'] = []
        return payload

    service, clock = _service_with_curve_fetcher(store, fetch)
    service._sync_daily_factors(date(2026, 10, 23), clock[0].isoformat())
    # 来源本就没有的数据不进水位；基准日缺失让依赖日也拿不到观测。
    assert json.loads(store.get_watermark(f'{CFETS_REALTIME}_retry_days') or '[]') == []
    factors = {item.as_of for item in store.factors_as_of(clock[0].isoformat(), CFETS_REALTIME)}
    assert '2026-10-09' not in factors
    assert '2026-10-12' not in factors


def test_corrupt_retry_watermark_rebuilds_instead_of_blocking_backfill(tmp_path):
    import json
    from src.fund_estimate_cfets import CFETS_REALTIME
    store = store_at(tmp_path)

    def fetch(kind, day, time):
        return _curve(day.isoformat(), time)

    service, clock = _service_with_curve_fetcher(store, fetch)
    store.set_watermark(f'{CFETS_REALTIME}_retry_days', 'not-json{', clock[0].isoformat())
    service._sync_daily_factors(date(2026, 10, 23), clock[0].isoformat())
    assert json.loads(store.get_watermark(f'{CFETS_REALTIME}_retry_days') or '[]') == []
    assert store.get_watermark(f'{CFETS_REALTIME}_before')


def test_legacy_factor_table_without_mapping_column_migrates(tmp_path):
    import sqlite3
    from src.fund_estimate_cfets import CFETS_REALTIME
    from src.fund_estimate_store import FundEstimateStore
    from src.fund_estimate_types import FACTOR_SET_ID, FactorObservation
    # 旧估值库：factor_observations 还没有 mapping_evidence_json 列。
    with sqlite3.connect(tmp_path / 'legacy.db') as conn:
        conn.execute("""CREATE TABLE factor_observations (
            id INTEGER PRIMARY KEY, factor_set_id TEXT NOT NULL, source_family TEXT NOT NULL,
            as_of TEXT NOT NULL, basis_date TEXT, published_at TEXT, fetched_at TEXT NOT NULL,
            market_as_of TEXT, values_bp_json TEXT NOT NULL, calendar_days INTEGER,
            basis_status TEXT NOT NULL, missing_fields_json TEXT NOT NULL, source_version TEXT NOT NULL,
            UNIQUE (factor_set_id, source_family, as_of, source_version))""")
    store = FundEstimateStore(tmp_path / 'legacy.db')
    store.initialize()
    observation = FactorObservation(FACTOR_SET_ID, CFETS_REALTIME, '2026-10-22',
                                    '2026-10-22T17:30:00+08:00', '2026-10-22T17:30:00+08:00',
                                    '2026-10-22T17:00:00+08:00', {'gov_3y_bp': '1'}, 1,
                                    'native', [], '2026-10-21', 'v1')
    assert store.save_factor(observation)
    loaded = store.factors_as_of('2026-10-23T10:00:00+08:00', CFETS_REALTIME)
    assert [item.as_of for item in loaded] == ['2026-10-22']
    assert loaded[0].mapping_evidence is None
