from datetime import datetime, timedelta
from types import SimpleNamespace


def _cfg():
    return SimpleNamespace(
        source=SimpleNamespace(TOK="old"),
        wechat=SimpleNamespace(to_user="user-1"),
    )


def test_scheduled_qr_success_refreshes_runtime(monkeypatch):
    import src.scheduler as scheduler

    cfg = _cfg()
    callback = {}
    refreshed = []
    updates = []
    messages = []

    monkeypatch.setattr(
        "src.qr_login_renewer.start_renew_cookies_by_qr",
        lambda *args, **kwargs: callback.setdefault("fn", kwargs["on_complete"]) or True,
    )
    monkeypatch.setattr(
        "src.config.refresh_source_config",
        lambda value, path: refreshed.append((value, path)) or value,
    )
    monkeypatch.setattr(scheduler, "update_runtime_config", updates.append)
    monkeypatch.setattr(
        "src.wechat_notifier.send_text",
        lambda _wechat, text, user=None: messages.append(text),
    )

    scheduler._start_qr_renew(cfg, "expired")
    callback["fn"](True, "扫码续期任务完成")

    assert refreshed and refreshed[0][0] is cfg
    assert updates == [cfg]
    assert any("运行时配置已同步" in text for text in messages)


def test_scheduled_qr_failure_does_not_refresh_runtime(monkeypatch):
    import src.scheduler as scheduler

    cfg = _cfg()
    callback = {}
    monkeypatch.setattr(
        "src.qr_login_renewer.start_renew_cookies_by_qr",
        lambda *args, **kwargs: callback.setdefault("fn", kwargs["on_complete"]) or True,
    )
    monkeypatch.setattr(
        "src.config.refresh_source_config",
        lambda *_args: (_ for _ in ()).throw(AssertionError("不应刷新")),
    )

    scheduler._start_qr_renew(cfg, "expired")
    callback["fn"](False, "扫码超时")


def test_scheduled_network_error_does_not_start_qr(monkeypatch):
    import src.cookies_checker as checker
    import src.scheduler as scheduler

    cfg = _cfg()
    notices = []
    monkeypatch.setattr(
        checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(checker.CookiesNetworkError("timeout")),
    )
    monkeypatch.setattr(
        "src.notifier.notify_cookies_network_error",
        lambda value, error: notices.append((value, error)),
    )
    monkeypatch.setattr(
        scheduler,
        "_start_qr_renew",
        lambda *_args: (_ for _ in ()).throw(AssertionError("网络异常不应生成二维码")),
    )

    scheduler._run_cookies_check(cfg)

    assert notices == [(cfg, "timeout")]


def test_scheduled_auth_error_still_starts_qr(monkeypatch):
    import src.cookies_checker as checker
    import src.scheduler as scheduler

    cfg = _cfg()
    renewals = []
    monkeypatch.setattr(
        checker,
        "check_cookies",
        lambda _cfg: (_ for _ in ()).throw(checker.CookiesError("login required")),
    )
    monkeypatch.setattr("src.notifier.notify_cookies_expired", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_start_qr_renew", lambda *args: renewals.append(args))

    scheduler._run_cookies_check(cfg)

    assert len(renewals) == 1
    assert renewals[0][0] is cfg


def test_portfolio_due_true_on_slot_change_false_within_slot():
    import src.scheduler as scheduler

    # 18:15 属于 18:00 slot，与 last_slot 相同 → 不触发
    assert scheduler._portfolio_due("2026-07-30T18:00", datetime(2026, 7, 30, 18, 15)) is False
    # 18:30 属于 18:30 slot，与 last_slot 18:00 不同 → 触发
    assert scheduler._portfolio_due("2026-07-30T18:00", datetime(2026, 7, 30, 18, 30)) is True
    # 18:45 属于 18:30 slot，与 last_slot 相同 → 不触发
    assert scheduler._portfolio_due("2026-07-30T18:30", datetime(2026, 7, 30, 18, 45)) is False


def test_prepare_portfolio_cycle_marks_slot_ready_after_success(monkeypatch):
    import src.scheduler as scheduler

    runtime = SimpleNamespace(write_enabled=True, repository=object())
    calls = []
    monkeypatch.setattr(
        scheduler,
        "_run_portfolio_cycle",
        lambda value, now: calls.append((value, now)) or True,
    )
    now = datetime(2026, 7, 30, 18, 30)

    slot, ready = scheduler._prepare_portfolio_cycle(
        runtime,
        "2026-07-30T18:00",
        now,
    )

    assert calls == [(runtime, now)]
    assert slot == "2026-07-30T18:30"
    assert ready is True


def test_prepare_portfolio_cycle_retries_failed_slot(monkeypatch):
    import src.scheduler as scheduler

    runtime = SimpleNamespace(write_enabled=True, repository=object())
    monkeypatch.setattr(scheduler, "_run_portfolio_cycle", lambda *_args: False)

    slot, ready = scheduler._prepare_portfolio_cycle(
        runtime,
        "2026-07-30T18:00",
        datetime(2026, 7, 30, 18, 30),
    )

    assert slot == "2026-07-30T18:00"
    assert ready is False


def test_prepare_portfolio_cycle_is_not_ready_without_repository():
    import src.scheduler as scheduler

    slot, ready = scheduler._prepare_portfolio_cycle(
        SimpleNamespace(write_enabled=False, repository=None),
        "2026-07-30T18:00",
        datetime(2026, 7, 30, 18, 30),
    )

    assert slot == "2026-07-30T18:00"
    assert ready is False


def test_scheduler_cycle_requests_strict_error_handling(monkeypatch):
    import src.scheduler as scheduler

    calls = []

    def fail_cycle(runtime, now, raise_on_error=False):
        calls.append((runtime, now, raise_on_error))
        raise RuntimeError("quote sync failed")

    monkeypatch.setattr("src.portfolio_jobs.run_portfolio_cycle", fail_cycle)
    runtime = SimpleNamespace(write_enabled=True, repository=object())
    now = datetime(2026, 7, 30, 18, 30)

    assert scheduler._run_portfolio_cycle(runtime, now) is False
    assert calls == [(runtime, now, True)]


def test_run_portfolio_cycle_noop_when_runtime_read_only(monkeypatch):
    import src.portfolio_jobs as jobs

    built = []
    monkeypatch.setattr(jobs, "build_portfolio_jobs", lambda _runtime: built.append(True) or None)

    from src.portfolio_jobs import run_portfolio_cycle

    result = run_portfolio_cycle(SimpleNamespace(write_enabled=False), datetime(2026, 7, 30, 18, 0))

    assert built == []
    assert result == jobs.PortfolioCycleResult(0, 0, 0, 0)
