from datetime import date

from src.config import Config
from src.server import (
    _build_help_messages,
    _ai_command_in_progress,
    _format_schedule_config,
    _is_existing_command,
    _is_nav_command_message,
    _normalize_daily_report_command_text,
    create_app,
)


def test_normalize_daily_report_natural_language_commands():
    assert _normalize_daily_report_command_text("帮我发一下日报") == "发送日报"
    assert _normalize_daily_report_command_text("今天日报提交一下") == "发送日报"
    assert _normalize_daily_report_command_text("日报重新发一下") == "重新发送今日日报"
    assert _normalize_daily_report_command_text("把今天日报撤回") == "撤回今日日报"
    assert _normalize_daily_report_command_text("查一下今天日报状态") == "今日状态"
    assert _normalize_daily_report_command_text("看看最近日报记录") == "最近记录"
    assert _normalize_daily_report_command_text("本月日报提交情况") == "本月统计"
    assert _normalize_daily_report_command_text("这周日报情况") == "本周统计"
    assert _normalize_daily_report_command_text("把日报提交时间改到 20:00") == "设置日报提交时间 20:00"
    assert _normalize_daily_report_command_text("沿用昨天日报提交") == "根据前一天内容发送"
    assert _normalize_daily_report_command_text("看下昨天日报内容") == "获取前一天日报"
    assert _normalize_daily_report_command_text("读取一下日报内容") == "读取日报"


def test_daily_report_normalizer_does_not_steal_nav_language():
    assert _normalize_daily_report_command_text("本月收益怎么样") == "本月收益怎么样"
    assert _normalize_daily_report_command_text("帮我看下今天净值") == "帮我看下今天净值"


def test_normalizes_natural_language_daily_report_date_queries():
    reference_date = date(2026, 7, 10)

    assert _normalize_daily_report_command_text(
        "查询2026-07-08日报", reference_date=reference_date
    ) == "查询日报 2026-07-08"
    assert _normalize_daily_report_command_text(
        "查20260708日报", reference_date=reference_date
    ) == "查询日报 2026-07-08"
    assert _normalize_daily_report_command_text(
        "帮我看 7月8日的日报", reference_date=reference_date
    ) == "查询日报 2026-07-08"
    assert _normalize_daily_report_command_text(
        "查昨天日报", reference_date=reference_date
    ) == "查询日报 2026-07-09"
    assert _normalize_daily_report_command_text(
        "看前天日报", reference_date=reference_date
    ) == "查询日报 2026-07-08"


def test_daily_report_date_query_preserves_non_query_commands():
    reference_date = date(2026, 7, 10)

    assert _normalize_daily_report_command_text("发送日报", reference_date=reference_date) == "发送日报"
    assert _normalize_daily_report_command_text("撤回今日日报", reference_date=reference_date) == "撤回今日日报"


def test_wechat_callback_does_not_shadow_module_regex_import():
    app = create_app(Config({}))
    callback = app.view_functions["wechat_callback"]

    assert "re" not in callback.__code__.co_varnames


def test_nav_message_gate_accepts_natural_nav_and_rejects_daily_report():
    assert _is_nav_command_message("本月收益怎么样") is True
    assert _is_nav_command_message("帮我看下今天净值") is True
    assert _is_nav_command_message("加一下信银 AF233276B") is True
    assert _is_nav_command_message("把 AF233276B 份额改成 401133.95") is True
    assert _is_nav_command_message("帮我发一下日报") is False
    assert _is_nav_command_message("本月日报提交情况") is False


def test_help_default_is_navigation_index_only():
    messages = _build_help_messages("帮助")

    assert len(messages) == 1
    assert "系统指令导航" in messages[0]
    assert "<font color=\"info\">直接说自然语言</font>" in messages[0]
    assert "**帮我发一下日报**" in messages[0]
    assert "<font color=\"comment\">日报、净值都支持口语表达</font>" in messages[0]
    assert "回复数字查看" in messages[0]
    assert "**1** 日报指令" in messages[0]
    assert "**0** 系统指令大全" in messages[0]
    assert "**5** AI助手" in messages[0]
    assert "日报指令" in messages[0]
    assert "净值指令" in messages[0]
    assert "系统指令大全" in messages[0]
    assert "启动Xray" not in messages[0]


def test_help_all_is_split_by_groups():
    messages = _build_help_messages("系统指令大全")

    assert len(messages) == 7
    assert messages[0].startswith("## 📝 日报指令")
    assert messages[1].startswith("## 💹 理财净值指令 · 查询统计")
    assert messages[2].startswith("## 📈 理财收益预估指令")
    assert messages[3].startswith("## 💹 理财净值指令 · 产品设置")
    assert messages[4].startswith("## ⚙️ 系统配置指令")
    assert messages[5].startswith("## 🖥️ 运维指令")
    assert messages[6].startswith("## 🤖 AI助手")
    assert all(len(message) < 1800 for message in messages)


def test_help_group_commands_return_single_detail_message():
    assert _build_help_messages("日报指令")[0].startswith("## 📝 日报指令")
    assert "帮我发一下日报" in _build_help_messages("日报指令")[0]
    assert "<font color=\"info\">提交</font>" in _build_help_messages("日报指令")[0]
    assert "**发送日报**" in _build_help_messages("日报指令")[0]
    assert "**查询2026-07-08日报**" in _build_help_messages("日报指令")[0]
    assert "<font color=\"comment\">读取智能文档并提交</font>" in _build_help_messages("日报指令")[0]
    nav_messages = _build_help_messages("净值指令")
    assert len(nav_messages) == 3
    assert nav_messages[0].startswith("## 💹 理财净值指令 · 查询统计")
    assert "最近1个月理财净值" in nav_messages[0]
    assert "<font color=\"info\">滚动周期统计</font>" in nav_messages[0]
    assert nav_messages[1].startswith("## 📈 理财收益预估指令")
    assert "用昨天行情预估理财涨跌" in nav_messages[1]
    assert "更新持仓画像" in nav_messages[1]
    assert "<font" not in nav_messages[1]
    assert nav_messages[2].startswith("## 💹 理财净值指令 · 产品设置")
    assert "**删除净值产品 AF233276B**" in nav_messages[2]
    assert "**设置净值推送时间 08:00**" not in nav_messages[2]
    assert "**开启净值监控**" in nav_messages[2]
    assert "**关闭净值监控**" in nav_messages[2]
    system_message = _build_help_messages("系统指令")[0]
    assert system_message.startswith("## ⚙️ 系统配置指令")
    assert "**设置净值推送时间 08:00**" in system_message
    assert "**设置收益预估时间 17:30**" in system_message
    assert _build_help_messages("运维指令")[0].startswith("## 🖥️ 运维指令")

    wealth_messages = _build_help_messages("理财指令")
    assert len(wealth_messages) == 3
    assert wealth_messages[0].startswith("## 💹 理财净值指令 · 查询统计")


def test_schedule_config_includes_nav_estimate_time():
    cfg = Config(
        {
            "scheduler": {
                "cookie_check_hour": 9,
                "cookie_check_minute": 45,
                "report_submit_hour": 20,
                "report_submit_minute": 0,
                "stats_push_hour": 21,
                "stats_push_minute": 0,
                "cache_cleanup_hour": 4,
                "cache_cleanup_minute": 0,
            },
            "nav_monitor": {
                "enabled": True,
                "push_hour": 8,
                "push_minute": 0,
                "evening_push_enabled": True,
                "evening_push_hour": 23,
                "evening_push_minute": 30,
                "estimate_enabled": True,
                "estimate_hour": 17,
                "estimate_minute": 30,
            },
        }
    )

    message = _format_schedule_config(cfg)

    assert "6️⃣ 净值晚间补发：23:30（工作日，有当日净值才发，开启）" in message
    assert "7️⃣ 理财收益预估：17:30（工作日，开启）" in message
    assert "设置收益预估时间 17:30" in message


def test_help_navigation_accepts_number_replies():
    assert _build_help_messages("1")[0].startswith("## 📝 日报指令")
    nav_messages = _build_help_messages("2")
    assert len(nav_messages) == 3
    assert nav_messages[0].startswith("## 💹 理财净值指令 · 查询统计")
    assert nav_messages[1].startswith("## 📈 理财收益预估指令")
    assert nav_messages[2].startswith("## 💹 理财净值指令 · 产品设置")
    assert _build_help_messages("3")[0].startswith("## ⚙️ 系统配置指令")
    assert _build_help_messages("4")[0].startswith("## 🖥️ 运维指令")
    ai_help = _build_help_messages("5")[0]
    assert ai_help.startswith("## 🤖 AI助手")
    assert "查看AI模型" in ai_help
    assert "切换AI模型 百炼" in ai_help
    assert "切换AI模型 LongCat" in ai_help
    assert len(_build_help_messages("0")) == 7


def test_existing_command_detection_keeps_deterministic_routes_out_of_ai():
    assert _is_existing_command("立即查询净值") is True
    assert _is_existing_command("发送日报") is True
    assert _is_existing_command("查看定时配置") is True
    assert _is_existing_command("重启服务") is True
    assert _is_existing_command("清理缓存") is True
    assert _is_existing_command("停止Xray") is True
    assert _is_existing_command("帮助") is True
    assert _is_existing_command("帮我瞅瞅那个最近表现咋样") is False


def test_ai_command_state_is_detected_for_global_callback_gate(monkeypatch):
    monkeypatch.setattr("src.server._cmd_busy", True)
    monkeypatch.setattr("src.server._cmd_name", "AI线上诊断")
    assert _ai_command_in_progress() is True

    monkeypatch.setattr("src.server._cmd_name", "净值查询")
    assert _ai_command_in_progress() is False


# ---- Task 3: 日报编辑指令解析测试 ----

def _srv():
    import src.server as s
    return s


def test_parse_mixed_colon_then_newline():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("修改今日日报：一、修复问题\n二、完成测试")
    assert parsed.action == "overwrite_today"
    assert parsed.body == "一、修复问题\n二、完成测试"


def test_parse_newline_first():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("设置日报\n一、完成联调\n二、测试")
    assert parsed.action == "set_today" and parsed.body == "一、完成联调\n二、测试"


def test_parse_colon_only():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("追加日报：完成生产验证")
    assert parsed.action == "append" and parsed.body == "完成生产验证"


def test_body_leading_space_preserved():
    s = _srv()
    parsed = s.parse_daily_report_edit_command("追加日报： 完成验证")  # 冒号后一个空格
    assert parsed.body == " 完成验证"  # 原样保留，不 strip


def test_set_report_time_not_captured():
    s = _srv()
    assert s.parse_daily_report_edit_command("设置日报提交时间 09:00") is None
    assert s.parse_daily_report_edit_command("设置日报提交时间：09:00") is None


def test_extract_body_uses_earliest_sep():
    s = _srv()
    assert s._extract_body_from_raw("往日报后面加一条：完成生产验证") == "完成生产验证"
    assert s._extract_body_from_raw("帮我设置日报\n一、完成联调") == "一、完成联调"
    assert s._extract_body_from_raw("修改今日日报：一、修复\n二、测试") == "一、修复\n二、测试"
    assert s._extract_body_from_raw("无分隔符") == ""
