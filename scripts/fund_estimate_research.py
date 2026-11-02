"""在独立研究库回补公开曲线并输出新旧模型比较；不连接正式组合库。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.beijing_time import TZ_CN
from src.fund_estimate_cfets import CFETS_REALTIME
from src.fund_estimate_cfets_extended import ExtendedCfetsCurveSource
from src.fund_estimate_credit_eod import ChinaBondCreditSource
from src.fund_estimate_research import FundEstimateResearch, research_metrics
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FundIdentity, NavObservation, ProfileVersion


def import_public_data(store, payload):
    identities = [FundIdentity(r['product_id'], r['code'], 'eastmoney_fund', r['share_class'], None, '债券型')
                  for r in payload.get('identities', [])]
    by_code = {identity.code: identity for identity in identities}
    for row in payload.get('navs', []):
        store.save_nav(NavObservation(**row))
    for row in payload.get('profiles', []):
        identity = by_code.get(row['code'])
        if identity is None:
            continue
        store.publish_profile(ProfileVersion(
            row['profile_version'], identity, row['report_as_of'], row['published_at'], row['fetched_at'],
            row['source_url'], row['document_hash'], json.loads(row['fields_json']),
            json.loads(row['evidence_json']), row['status']))
    return identities


def refresh_curves(store, start, end, workers, source_class=ExtendedCfetsCurveSource):
    ranges = []
    begin = start
    while begin <= end:
        finish = min(end, begin+timedelta(days=20))
        ranges.append((begin, finish))
        begin = finish+timedelta(days=1)

    def fetch(bounds):
        source = source_class(store)
        try:
            observations = source.fetch_daily_factors(*bounds, getattr(source, 'source_family', CFETS_REALTIME))
            for factor in observations:
                store.save_factor(factor)
            return len(observations), [d.isoformat() for d in source.take_transient_failures()]
        finally:
            if source.session is not None:
                source.session.close()

    count, failures = 0, []
    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as pool:
        for future in as_completed([pool.submit(fetch, bounds) for bounds in ranges]):
            got, missing = future.result()
            count += got
            failures.extend(missing)
            print(f'{source_class.__name__}: {count} observations, {len(failures)} transient gaps', flush=True)
    family = getattr(source_class, 'source_family', CFETS_REALTIME)
    store.set_watermark('research:'+family+':retry', json.dumps(sorted(set(failures))), datetime.now(TZ_CN).isoformat())
    return dict(observations=count, transient_days=sorted(set(failures)))


def markdown_report(report):
    lines = ['# 债券基金模型并行研究', '',
             f"生成时间：{report['generated_at']}", '',
             '回补版本的结果为回顾探索，不等于真实盘中准确率。候选模型未激活。', '',
             '|基金代码/来源|历史样本|窗口|评估天数|基线Ridge MAE(bp)|选型候选|候选MAE(bp)|候选大波动日MAE(bp)|',
             '|---|---:|---|---:|---:|---|---:|---:|']
    def fmt(value):
        return f'{Decimal(value):.3f}' if value is not None else '—'
    for item in report['items']:
        mode = item['retrospective']
        selected = mode['selected_strategy']
        candidates = mode['candidates']
        legacy, chosen = candidates.get('legacy_ridge', {}), candidates.get(selected, {})
        lines.append(f"|{item['code']}/{item['source_family']}|{item['history_count']}|{item['training_windows']}|{len(mode['evaluation_dates'])}|"
                     f"{fmt(legacy.get('mae_bp'))}|{selected or '样本不足'}|{fmt(chosen.get('mae_bp'))}|"
                     f"{fmt(chosen.get('stress_mae_bp'))}|")
    lines += ['', '选型只使用较早的独立选择段，表中误差来自更晚的评估段。',
              '全体候选的逐项指标、日期、严格时点验证及真实盘中分时验证见同名JSON。', '',
              '数据范围：CFETS实时曲线为国债、国开债、AAA中票的不同期限；中债日终研究另含AA+、AA。'
              '各来源独立选型，不将日终数据冒充实时输入；未核实二永债及城投专属盘中曲线。',
              '公开报告的敏感度仅用作候选约束，不能代表当天完整持仓。']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='独立研究库路径')
    parser.add_argument('--import-json', type=Path, help='仅含净值、画像和身份的公开数据导出')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--refresh', action='store_true', help='显式联网回补公开曲线')
    parser.add_argument('--history-days', type=int, default=400)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--source', choices=('realtime', 'credit_eod', 'both'), default='realtime')
    args = parser.parse_args(argv)
    if args.db.name.lower() in {'portfolio.db', 'fund_estimation.db'}:
        parser.error('--db 请使用独立研究库名称，例如 fund_estimation_research.db')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    store = FundEstimateStore(args.db); store.initialize()
    payload = json.loads(args.import_json.read_text(encoding='utf-8-sig')) if args.import_json else {}
    identities = import_public_data(store, payload)
    if not identities:
        parser.error('需要 --import-json 提供研究基金及历史净值')
    refresh = {}
    classes = ([ExtendedCfetsCurveSource] if args.source == 'realtime' else
               [ChinaBondCreditSource] if args.source == 'credit_eod' else
               [ExtendedCfetsCurveSource, ChinaBondCreditSource])
    for source_class in classes:
        if args.refresh:
            today = datetime.now(TZ_CN).date()
            refresh[source_class.__name__] = refresh_curves(store, today-timedelta(days=args.history_days), today,
                                                          args.workers, source_class)
        now = datetime.now(TZ_CN)
        research = FundEstimateResearch(store, source_class(store), {'research_history_days': args.history_days})
        for identity in identities:
            research.train(identity, now)
            print(f"evaluated {identity.code}/{research.source_family}", flush=True)
    original = payload.get('original_final_predictions', [])
    pairs = [(r['trade_date'], Decimal(r['predicted_pct']), Decimal(r['realized_pct']))
             for r in original if r.get('error_pct') is not None]
    report = dict(generated_at=now.isoformat(), refresh=refresh, items=research.reports(now),
                  original_live_metrics=research_metrics(pairs),
                  sources=['https://www.chinamoney.com.cn/chinese/bkcurvrty/', 'https://yield.chinabond.com.cn/'],
                  limitation='历史回顾与真实盘中样本分别保存；不将分钟记录当成独立交易日。')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    args.output.with_suffix('.md').write_text(markdown_report(report), encoding='utf-8')
    print(f'report: {args.output}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
