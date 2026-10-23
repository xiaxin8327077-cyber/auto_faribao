from datetime import datetime

from src.scheduler import _barometer_due


def test_barometer_due_only_during_weekday_trading_window_slots():
    # 交易时段首个槽
    assert _barometer_due(None, datetime(2026, 10, 22, 9, 5))
    # 同一分钟不重复，下一分钟重新取盘中数据。
    assert not _barometer_due("2026-10-22T09:05", datetime(2026, 10, 22, 9, 5, 30))
    assert _barometer_due("2026-10-22T09:05", datetime(2026, 10, 22, 9, 6))
    # 交易时段外不触发
    assert not _barometer_due(None, datetime(2026, 10, 22, 8, 59))
    assert not _barometer_due(None, datetime(2026, 10, 22, 17, 31))
    # 周末不触发
    assert not _barometer_due(None, datetime(2026, 10, 24, 10, 0))


def test_scheduler_started_during_market_hours_reaches_end_of_first_cycle(monkeypatch):
    import pytest
    import src.scheduler as scheduler

    class EndCycle(Exception):
        pass

    runtime = object()
    warmed = []

    class InlineThread:
        def __init__(self, target, args=(), **_kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    def end_cycle(_seconds):
        raise EndCycle

    monkeypatch.setattr(scheduler, "_runtime_cfg", None)
    monkeypatch.setattr(scheduler, "_portfolio_runtime", runtime)
    monkeypatch.setattr(scheduler, "beijing_now", lambda: datetime(2026, 10, 23, 9, 19))
    monkeypatch.setattr(scheduler, "handle_pending_timeout_if_needed", lambda _cfg: None)
    monkeypatch.setattr(scheduler, "_get_times", lambda _cfg: (0,) * 8)
    monkeypatch.setattr(scheduler, "_is_workday", lambda _day: False)
    monkeypatch.setattr(scheduler, "_warm_bond_barometer", warmed.append)
    monkeypatch.setattr(scheduler.threading, "Thread", InlineThread)
    monkeypatch.setattr(scheduler.time, "sleep", end_cycle)
    with pytest.raises(EndCycle):
        scheduler._run(object())
    assert warmed == [runtime]
