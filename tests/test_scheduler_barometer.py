from datetime import datetime

from src.scheduler import _barometer_due


def test_barometer_due_only_during_weekday_trading_window_slots():
    # 交易时段首个槽
    assert _barometer_due(None, datetime(2026, 10, 22, 9, 5))
    # 同一 30 分钟槽不重复触发
    assert not _barometer_due("2026-10-22T09:00", datetime(2026, 10, 22, 9, 29))
    # 进入下一个槽
    assert _barometer_due("2026-10-22T09:00", datetime(2026, 10, 22, 9, 30))
    # 交易时段外不触发
    assert not _barometer_due(None, datetime(2026, 10, 22, 8, 59))
    assert not _barometer_due(None, datetime(2026, 10, 22, 17, 31))
    # 周末不触发
    assert not _barometer_due(None, datetime(2026, 10, 24, 10, 0))
