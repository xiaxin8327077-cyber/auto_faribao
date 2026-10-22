from datetime import datetime

from src.config import Config
from src.server import create_app


def _cfg() -> Config:
    return Config(
        {
            "wechat": {"token": "token", "aes_key": "a" * 43, "corpid": "corp"},
            "nav_monitor": {"products": []},
        }
    )


def test_dashboard_api_rejects_missing_or_wrong_private_key(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    client = create_app(_cfg()).test_client()

    assert client.get("/api/nav-dashboard").status_code == 403
    assert (
        client.get(
            "/api/nav-dashboard",
            headers={"X-Nav-Dashboard-Key": "wrong"},
        ).status_code
        == 403
    )


def test_dashboard_api_reads_cache_only(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    monkeypatch.setattr(
        "src.nav_dashboard.get_dashboard_payload",
        lambda _cfg: {"initialized": True, "products": []},
    )
    client = create_app(_cfg()).test_client()

    response = client.get(
        "/api/nav-dashboard",
        headers={"X-Nav-Dashboard-Key": "secret-key"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"initialized": True, "products": []}


def test_manual_refresh_api_returns_immediately(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    monkeypatch.setattr(
        "src.nav_dashboard.request_manual_refresh",
        lambda _cfg: {"accepted": True, "status": "refreshing"},
    )
    client = create_app(_cfg()).test_client()

    response = client.post(
        "/api/nav-dashboard/refresh",
        headers={"X-Nav-Dashboard-Key": "secret-key"},
    )

    assert response.status_code == 202
    assert response.get_json()["status"] == "refreshing"


def test_manual_refresh_api_returns_ok_while_refresh_is_already_running(monkeypatch):
    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    monkeypatch.setattr(
        "src.nav_dashboard.request_manual_refresh",
        lambda _cfg: {"accepted": False, "status": "refreshing"},
    )
    client = create_app(_cfg()).test_client()

    response = client.post(
        "/api/nav-dashboard/refresh",
        headers={"X-Nav-Dashboard-Key": "secret-key"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"accepted": False, "status": "refreshing"}


def test_scheduler_does_not_expose_hourly_dashboard_refresh_hook():
    from src import scheduler

    assert not hasattr(scheduler, "_run_nav_dashboard_refresh_check")


def test_dashboard_config_sync_failure_does_not_propagate(monkeypatch):
    from src import server

    monkeypatch.setattr(
        "src.nav_dashboard.sync_dashboard_portfolio",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("state unavailable")),
    )

    server._sync_dashboard_after_config_save(_cfg())


def test_nav_page_contains_dashboard_shell_and_refresh_control():
    html = create_app(_cfg()).test_client().get("/nav").get_data(as_text=True)

    assert "理财组合看板" in html
    assert 'id="refreshButton"' in html
    assert 'id="refreshState"' in html
    assert "/api/nav-dashboard" in html
    assert "累计收益" in html
    assert "产品持仓" in html


def test_nav_page_contains_daily_profit_bottom_sheet():
    html = create_app(_cfg()).test_client().get("/nav").get_data(as_text=True)

    assert 'id="cumulativeProfitTrigger"' in html
    assert 'aria-controls="dailyProfitSheet"' in html
    assert 'id="dailyProfitSheet"' in html
    assert 'id="dailyProfitList"' in html
    assert "收益明细" in html
    assert "每日收益明细" not in html
    assert "profit-page" in html
    assert 'aria-label="返回总览"' in html
    assert 'id="dailyProfitTab"' in html
    assert 'id="monthlyProfitTab"' in html
    assert 'id="yearlyProfitTab"' in html
    assert "每月" in html
    assert "每年" in html
    assert 'id="currentMonthProfit"' in html
    assert "本月累计" in html
    assert 'id="latestProfitLabel"' in html
    assert 'id="latestProfit"' in html
    assert 'id="latestProfitDate"' in html
    assert "最新收益" in html
    assert "最新收益（${" not in html
    assert "本月收益贡献排行" in html
    assert "本月无正贡献" in html
    assert "本月无拖累" in html
    assert "/nav-assets/cloud-sun.svg" in html
    assert "allPositive" in html
    assert "totalTone" in html
    assert ".hero.sunny" in html
    assert "data-profit-row-key" in html
    assert "daily-profit-products" in html


def test_nav_icon_assets_are_served_locally():
    response = create_app(_cfg()).test_client().get("/nav-assets/refresh-cw.svg")

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"


def test_selected_logo_png_is_served_locally_with_transparency():
    response = create_app(_cfg()).test_client().get("/nav-assets/private-wealth-logo.png")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data.startswith(b"\x89PNG\r\n\x1a\n")
    # PNG IHDR color type 6 is RGBA: the logo must not bake in a checkerboard.
    assert response.data[25] == 6
    assert len(response.data) < 400_000


def test_fangsong_webfont_is_served_locally():
    client = create_app(_cfg()).test_client()
    css = client.get("/nav-assets/fonts/fz-fangsong/font.css")
    woff = client.get("/nav-assets/fonts/fz-fangsong/L1_4e00_192.woff2")

    assert css.status_code == 200
    assert "FZFangSong-Z02S" in css.get_data(as_text=True)
    assert woff.status_code == 200
    assert woff.mimetype in {"font/woff2", "application/octet-stream", "application/font-woff2"}


def test_dashboard_api_gzips_large_payload_only(monkeypatch):
    """大 payload 按 Accept-Encoding 返回 gzip；小响应与未请求 gzip 时不压缩。"""
    import gzip as gzip_module
    import json

    monkeypatch.setattr("src.nav_dashboard.get_access_token", lambda: "secret-key")
    monkeypatch.setattr(
        "src.nav_dashboard.get_dashboard_payload",
        lambda _cfg: {"initialized": True, "blob": "x" * 40000},
    )
    client = create_app(_cfg()).test_client()

    compressed = client.get(
        "/api/nav-dashboard",
        headers={
            "X-Nav-Dashboard-Key": "secret-key",
            "Accept-Encoding": "gzip",
        },
    )
    assert compressed.status_code == 200
    assert compressed.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in compressed.headers.get("Vary", "")
    decompressed = json.loads(gzip_module.decompress(compressed.get_data()))
    assert decompressed["initialized"] is True
    assert len(decompressed["blob"]) == 40000

    plain = client.get(
        "/api/nav-dashboard",
        headers={"X-Nav-Dashboard-Key": "secret-key"},
    )
    assert plain.headers.get("Content-Encoding") is None
    assert plain.get_json()["initialized"] is True

    monkeypatch.setattr(
        "src.nav_dashboard.get_dashboard_payload",
        lambda _cfg: {"initialized": True},
    )
    small = client.get(
        "/api/nav-dashboard",
        headers={
            "X-Nav-Dashboard-Key": "secret-key",
            "Accept-Encoding": "gzip",
        },
    )
    assert small.headers.get("Content-Encoding") is None
    assert small.get_json() == {"initialized": True}


def test_dashboard_page_gzips_html(monkeypatch):
    import gzip as gzip_module

    monkeypatch.setattr(
        "src.nav_dashboard.render_dashboard_page",
        lambda: "<html>" + "x" * 200000 + "</html>",
    )
    client = create_app(_cfg()).test_client()

    response = client.get("/nav", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.headers.get("Content-Encoding") == "gzip"
    body = gzip_module.decompress(response.get_data())
    assert body.startswith(b"<html>")

    plain = client.get("/nav")
    assert plain.headers.get("Content-Encoding") is None
    assert plain.get_data().startswith(b"<html>")
