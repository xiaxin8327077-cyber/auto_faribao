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
