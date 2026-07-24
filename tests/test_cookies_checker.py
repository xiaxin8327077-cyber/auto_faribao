import pytest


def test_network_timeout_retries_once_then_succeeds(monkeypatch):
    import src.cookies_checker as checker

    calls = []

    def check_once(cfg):
        calls.append(cfg)
        if len(calls) == 1:
            raise checker.CookiesNetworkError("timeout")
        return True

    monkeypatch.setattr(checker, "_check_cookies_once", check_once)
    cfg = object()

    assert checker.check_cookies(cfg) is True
    assert calls == [cfg, cfg]


def test_two_network_timeouts_raise_network_error(monkeypatch):
    import src.cookies_checker as checker

    calls = []

    def check_once(cfg):
        calls.append(cfg)
        raise checker.CookiesNetworkError("timeout")

    monkeypatch.setattr(checker, "_check_cookies_once", check_once)

    with pytest.raises(checker.CookiesNetworkError, match="timeout"):
        checker.check_cookies(object())

    assert len(calls) == 2


def test_auth_failure_does_not_retry(monkeypatch):
    import src.cookies_checker as checker

    calls = []

    def check_once(cfg):
        calls.append(cfg)
        raise checker.CookiesError("redirected to login page")

    monkeypatch.setattr(checker, "_check_cookies_once", check_once)

    with pytest.raises(checker.CookiesError, match="login"):
        checker.check_cookies(object())

    assert len(calls) == 1
