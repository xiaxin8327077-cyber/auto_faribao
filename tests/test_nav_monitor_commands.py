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
    assert parse_nav_command("看下净值的配置").action == "view_config"
    assert parse_nav_command("看看理财净值配置").action == "view_config"
    # 写指令已迁移至网页，企业微信只返回 None
    assert parse_nav_command("开启净值监控") is None
    assert parse_nav_command("关闭净值监控") is None
    assert parse_nav_command("设置净值推送时间 08:30") is None
    assert parse_nav_command("设置收益预估时间 17:30") is None
    assert parse_nav_command("把理财预估时间改到 18:05") is None


def test_parse_nav_holdings_and_estimate_commands():
    base = date(2026, 7, 9)
    # 预估/更新画像已迁移至网页
    assert parse_nav_command("收盘后预估一下理财涨跌", base) is None
    assert parse_nav_command("用昨天行情预估理财涨跌", base) is None
    assert parse_nav_command("更新持仓画像") is None
    view_all = parse_nav_command("查看持仓画像")
    view_one = parse_nav_command("查看 AF233276B 持仓画像")
    status = parse_nav_command("查看画像状态")

    assert view_all.action == "view_holdings"
    assert view_all.code == ""
    assert view_one.action == "view_holdings"
    assert view_one.code == "AF233276B"
    assert status.action == "view_holdings_status"


def test_prediction_language_takes_priority_over_latest_nav_query():
    base = date(2026, 7, 13)

    # 预估涨跌已迁移至网页，企业微信只返回 None
    assert parse_nav_command("今天我的理财能涨吗", base) is None
    assert parse_nav_command("我的理财会不会跌", base) is None
    assert parse_nav_command("帮我预测一下理财涨跌", base) is None
    explicit_nav_query = parse_nav_command("查询今天理财净值", base)
    assert explicit_nav_query.action == "query_latest"


def test_parse_nav_product_commands():
    # 添加/删除/确认添加/取消添加均已迁移至网页
    assert parse_nav_command("添加净值产品 信银 AF233276B") is None
    assert parse_nav_command("删除净值产品 AF233276B") is None
    assert parse_nav_command("确认添加净值产品 1") is None
    assert parse_nav_command("取消添加净值产品") is None


def test_parse_nav_product_add_alias_and_usage():
    # 添加产品（含别名与缺失参数）均已迁移至网页
    assert parse_nav_command("添加产品 信银 AF233276B") is None
    assert parse_nav_command("添加净值产品 信银理财AF233276B") is None
    assert parse_nav_command("添加产品") is None


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
    rolling_2y = parse_nav_command("查询近两年净值")
    rolling_2y_num = parse_nav_command("查询近2年净值")
    rolling_3y = parse_nav_command("查询近三年净值")
    rolling_3y_num = parse_nav_command("查询近3年净值")
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
    assert rolling_2y.period == "rolling_2y"
    assert rolling_2y_num.period == "rolling_2y"
    assert rolling_3y.period == "rolling_3y"
    assert rolling_3y_num.period == "rolling_3y"
    # 设置份额已迁移至网页
    assert shares is None


def test_parse_batch_nav_shares_command():
    # 批量设置份额已迁移至网页
    command = parse_nav_command(
        "批量设置净值份额\n"
        "AF233276B 10000.50\n"
        "AF233262B 20000"
    )

    assert command is None

def test_parse_nav_natural_language_query_commands():
    base = date(2026, 7, 8)

    latest = parse_nav_command("帮我看下今天净值", base)
    yesterday = parse_nav_command("查一下昨天理财收益", base)
    exact = parse_nav_command("查 20260505 净值", base)
    rolling = parse_nav_command("看看近三年收益", base)
    rolling_1m_num = parse_nav_command("帮我看一下最近1个月的理财净值", base)
    monthly = parse_nav_command("本月收益怎么样", base)
    weekly = parse_nav_command("上周理财表现", base)

    assert latest.action == "query_latest"
    assert yesterday.action == "query_date"
    assert yesterday.target_date == date(2026, 7, 7)
    assert exact.action == "query_date"
    assert exact.target_date == date(2026, 5, 5)
    assert rolling.action == "query_period"
    assert rolling.period == "rolling_3y"
    assert rolling_1m_num.action == "query_period"
    assert rolling_1m_num.period == "rolling_1m"
    assert monthly.period == "month"
    assert weekly.period == "week"


def test_parse_nav_natural_language_period_numbers_are_interchangeable():
    cases = {
        "最近7天理财收益": "rolling_7d",
        "最近七天理财收益": "rolling_7d",
        "最近一周理财收益": "rolling_7d",
        "最近1周理财收益": "rolling_7d",
        "最近一个星期理财收益": "rolling_7d",
        "最近一月理财收益": "rolling_1m",
        "最近一个月理财收益": "rolling_1m",
        "最近1个月理财收益": "rolling_1m",
        "最近三个月理财收益": "rolling_3m",
        "最近3个月理财收益": "rolling_3m",
        "最近半年理财收益": "rolling_6m",
        "最近六个月理财收益": "rolling_6m",
        "最近6个月理财收益": "rolling_6m",
        "最近一年理财收益": "rolling_1y",
        "最近1年理财收益": "rolling_1y",
        "最近两年理财收益": "rolling_2y",
        "最近2年理财收益": "rolling_2y",
        "最近三年理财收益": "rolling_3y",
        "最近3年理财收益": "rolling_3y",
    }

    for text, expected_period in cases.items():
        command = parse_nav_command(text)
        assert command is not None, text
        assert command.action == "query_period", text
        assert command.period == expected_period, text


def test_parse_nav_natural_language_product_and_shares_commands():
    # 自然语言添加/设置份额/删除产品均已迁移至网页
    assert parse_nav_command("加一下信银 AF233276B") is None
    assert parse_nav_command("把 AF233276B 份额改成 401133.95") is None
    assert parse_nav_command("删除一下 AF233276B 产品") is None


def test_daily_report_commands_are_not_nav_commands():
    assert parse_nav_command("今日日报") is None
    assert parse_nav_command("今日状态") is None
    assert parse_nav_command("设置日报提交时间 20:00") is None
    assert parse_nav_command("发送日报") is None


def test_parse_set_daily_profit_valid():
    base = date(2026, 7, 22)
    # 设置收益已迁移至网页
    assert parse_nav_command("设置收益 2026-07-21 150.5", base) is None
    assert parse_nav_command("设置收益 昨天 -80", base) is None
    assert parse_nav_command("设置收益 20260721 0", base) is None


def test_parse_set_daily_profit_invalid_date():
    base = date(2026, 7, 22)
    # 设置收益已迁移至网页（含非法日期分支）
    assert parse_nav_command("设置收益 2026-02-30 100", base) is None
    assert parse_nav_command("设置收益 abc 100", base) is None


def test_parse_set_daily_profit_invalid_amount_does_not_fallthrough():
    base = date(2026, 7, 22)
    # 设置收益已迁移至网页（含金额格式错误分支）
    assert parse_nav_command("设置收益 2026-07-21 abc", base) is None
    assert parse_nav_command("设置收益 2026-07-21 +100", base) is None
    assert parse_nav_command("设置收益 2026-07-21", base) is None
    assert parse_nav_command("设置收益", base) is None


def test_parse_natural_set_daily_profit():
    base = date(2026, 7, 22)
    # 自然语言设置收益已迁移至网页
    assert parse_nav_command("把最新收益改成200", base) is None
    assert parse_nav_command("把昨天的收益改成150块", base) is None
    assert parse_nav_command("把最新收益改成-50", base) is None
    assert parse_nav_command("把最新收益改为88.5元", base) is None


def test_natural_set_daily_profit_does_not_conflict_with_prediction():
    base = date(2026, 7, 22)
    # 预估类指令已迁移至网页
    assert parse_nav_command("收益预估改成200", base) is None
    assert parse_nav_command("预估理财涨跌", base) is None


def test_natural_set_daily_profit_requires_verb():
    base = date(2026, 7, 22)
    # 没有"改成/改为"等动词，不应匹配设置收益（已迁移至网页）
    assert parse_nav_command("最新收益200", base) is None
    assert parse_nav_command("看看收益", base) is None
