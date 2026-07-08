from datetime import date

from src.nav_monitor import parse_nav_command


def test_parse_nav_query_commands():
    base = date(2026, 7, 8)

    assert parse_nav_command("立即查询净值", base).action == "query_latest"
    yesterday = parse_nav_command("查询昨天净值", base)
    exact = parse_nav_command("查询净值 20260707", base)

    assert yesterday.action == "query_date"
    assert yesterday.target_date == date(2026, 7, 7)
    assert exact.action == "query_date"
    assert exact.target_date == date(2026, 7, 7)


def test_parse_nav_config_commands():
    assert parse_nav_command("查看净值配置").action == "view_config"
    assert parse_nav_command("开启净值监控").action == "enable"
    assert parse_nav_command("关闭净值监控").action == "disable"

    set_time = parse_nav_command("设置净值推送时间 08:30")
    assert set_time.action == "set_time"
    assert set_time.hour == 8
    assert set_time.minute == 30


def test_parse_nav_product_commands():
    add = parse_nav_command("添加净值产品 信银 AF233276B")
    delete = parse_nav_command("删除净值产品 AF233276B")
    confirm = parse_nav_command("确认添加净值产品 1")
    cancel = parse_nav_command("取消添加净值产品")

    assert add.action == "add_product"
    assert add.provider == "citic_wealth"
    assert add.query == "AF233276B"
    assert delete.action == "delete_product"
    assert delete.code == "AF233276B"
    assert confirm.action == "confirm_add"
    assert confirm.index == 1
    assert cancel.action == "cancel_add"


def test_daily_report_commands_are_not_nav_commands():
    assert parse_nav_command("今日日报") is None
    assert parse_nav_command("今日状态") is None
    assert parse_nav_command("设置日报提交时间 20:00") is None
    assert parse_nav_command("发送日报") is None
