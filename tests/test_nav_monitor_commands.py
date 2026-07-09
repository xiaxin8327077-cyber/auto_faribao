from datetime import date
from decimal import Decimal

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


def test_parse_nav_product_add_alias_and_usage():
    alias = parse_nav_command("添加产品 信银 AF233276B")
    stuck = parse_nav_command("添加净值产品 信银理财AF233276B")
    usage = parse_nav_command("添加产品")

    assert alias.action == "add_product"
    assert alias.provider == "citic_wealth"
    assert alias.query == "AF233276B"
    assert stuck.action == "add_product"
    assert stuck.provider == "citic_wealth"
    assert stuck.query == "AF233276B"
    assert usage.action == "add_product_usage"


def test_parse_nav_period_and_shares_commands():
    weekly = parse_nav_command("查询周度净值")
    monthly = parse_nav_command("查询月度净值")
    quarterly = parse_nav_command("查询季度净值")
    half_year = parse_nav_command("查询半年度净值")
    yearly = parse_nav_command("查询年度净值")
    rolling_7d = parse_nav_command("查询近7天净值")
    rolling_1m = parse_nav_command("查询近一月净值")
    rolling_3m = parse_nav_command("查询近三月净值")
    rolling_6m = parse_nav_command("查询近半年净值")
    rolling_1y = parse_nav_command("查询近一年净值")
    shares = parse_nav_command("设置净值份额 AF233276B 10000.50")

    assert weekly.action == "query_period"
    assert weekly.period == "week"
    assert monthly.period == "month"
    assert quarterly.period == "quarter"
    assert half_year.period == "half_year"
    assert yearly.period == "year"
    assert rolling_7d.period == "rolling_7d"
    assert rolling_1m.period == "rolling_1m"
    assert rolling_3m.period == "rolling_3m"
    assert rolling_6m.period == "rolling_6m"
    assert rolling_1y.period == "rolling_1y"
    assert shares.action == "set_shares"
    assert shares.code == "AF233276B"
    assert str(shares.shares) == "10000.50"


def test_parse_batch_nav_shares_command():
    command = parse_nav_command(
        "批量设置净值份额\n"
        "AF233276B 10000.50\n"
        "AF233262B 20000"
    )

    assert command.action == "set_shares_batch"
    assert command.share_updates == (
        ("AF233276B", Decimal("10000.50")),
        ("AF233262B", Decimal("20000")),
    )
    assert command.share_errors == ()


def test_daily_report_commands_are_not_nav_commands():
    assert parse_nav_command("今日日报") is None
    assert parse_nav_command("今日状态") is None
    assert parse_nav_command("设置日报提交时间 20:00") is None
    assert parse_nav_command("发送日报") is None
