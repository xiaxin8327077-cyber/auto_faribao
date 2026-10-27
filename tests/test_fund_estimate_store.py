"""存储行为与发现测试共用同一组用例文件名约定；发现用例在 discovery 文件。"""
from tests.test_fund_estimate_discovery import (  # noqa: F401
    test_estimate_storage_does_not_mutate_ledger,
    test_newer_version_survives_overlapping_publish,
)
def test_verification_snapshot_lookup_stays_within_linear_query_budget(tmp_path):
    from contextlib import contextmanager
    from datetime import datetime, timedelta
    import json
    from src.fund_estimate_store import FundEstimateStore

    class BudgetStore(FundEstimateStore):
        @contextmanager
        def _connect(self):
            with super()._connect() as conn:
                operations = [0]
                def progress():
                    operations[0] += 1000
                    return int(operations[0] > 200000)
                conn.set_progress_handler(progress, 1000)
                yield conn

    store = BudgetStore(tmp_path / 'estimates.db')
    store.initialize()
    payload = json.dumps({'return_pct': '0.1', 'base_nav': '1'})
    rows = []
    for index in range(1000):
        stamp = (datetime(2026, 10, 27, 10) + timedelta(seconds=index)).isoformat()
        rows.append(('p', '000001', '2026-10-27', stamp, payload, stamp, 'v'))
    with store._connect() as conn:
        conn.executemany('INSERT INTO estimate_snapshots '
            '(product_id,code,trade_date,slot,payload_json,generated_at,source_version) VALUES(?,?,?,?,?,?,?)', rows)
        conn.executemany('INSERT INTO verifications '
            '(product_id,code,trade_date,snapshot_generated_at,realized_return_pct,recorded_at) VALUES(?,?,?,?,?,?)',
            [(row[0], row[1], row[2], row[5], '0.1', row[5]) for row in rows])
    assert len(store.read_verifications()) == 1000
