from src.config import Config, save_config


def test_nav_monitor_config_defaults_to_enabled_with_0800():
    cfg = Config({})

    assert cfg.nav_monitor.enabled is True
    assert cfg.nav_monitor.push_hour == 8
    assert cfg.nav_monitor.push_minute == 0
    assert cfg.nav_monitor.products == []


def test_save_config_preserves_nav_monitor(tmp_path):
    cfg = Config({
        "nav_monitor": {
            "enabled": True,
            "push_hour": 8,
            "push_minute": 30,
            "products": [
                {
                    "provider": "citic_wealth",
                    "code": "AF233276B",
                    "name": "P1",
                },
            ],
        }
    })
    path = tmp_path / "config.yaml"

    data = save_config(str(path), cfg, {"scheduler": {"report_submit_hour": 20}})

    assert data["scheduler"]["report_submit_hour"] == 20
    assert data["nav_monitor"]["enabled"] is True
    assert data["nav_monitor"]["push_hour"] == 8
    assert data["nav_monitor"]["push_minute"] == 30
    assert data["nav_monitor"]["products"][0]["provider"] == "citic_wealth"
    assert data["nav_monitor"]["products"][0]["code"] == "AF233276B"
