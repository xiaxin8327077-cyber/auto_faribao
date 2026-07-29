import os
import re
import subprocess
import threading
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from flask import Flask, request, jsonify, send_from_directory
from src.config import Config
from src.notifier import notify_report_success, notify_report_failure
from src.target import submit_daily_report, get_previous_report_content, get_report_status, delete_daily_report, get_recent_reports, get_monthly_statistics, get_weekly_statistics
from src.wechat_callback import WechatCallbackCrypto, parse_message, extract_text_content, is_cookies_update_message
from src.auto_cookies_updater import update_cookies_from_wechat
from src.extractor import extract_tasks, ExtractError
from src.wechat_notifier import send_text as _send_wechat_text, send_markdown as _send_wechat_markdown
from src.qr_login_renewer import start_renew_cookies_by_qr
from src.pending_confirmation import (
    save_pending, load_pending, clear_pending,
    is_pending_confirmation_reply, is_yes, is_no,
)

logger = logging.getLogger(__name__)

_cmd_lock = threading.Lock()
_cmd_busy = False
_cmd_name = ""


def _try_start_cmd(name: str) -> bool:
    """Try to acquire the command lock. Returns True if started, False if busy."""
    global _cmd_busy, _cmd_name
    with _cmd_lock:
        if _cmd_busy:
            return False
        _cmd_busy = True
        _cmd_name = name
        return True


def _end_cmd():
    """Release the command lock."""
    global _cmd_busy, _cmd_name
    with _cmd_lock:
        _cmd_busy = False
        _cmd_name = ""


def _rename_cmd(name: str) -> None:
    """Rename the currently running command without changing lock ownership."""
    global _cmd_name
    with _cmd_lock:
        if _cmd_busy:
            _cmd_name = name


def _finish_manual_qr_command(success: bool, message: str) -> None:
    try:
        logger.info("Manual QR command finished: success=%s, message=%s", success, message)
    finally:
        _end_cmd()


def _refresh_runtime_source(cfg: Config, config_path: str) -> Config:
    from src.config import refresh_source_config
    from src.scheduler import update_runtime_config

    refresh_source_config(cfg, config_path)
    update_runtime_config(cfg)
    return cfg


def _finish_manual_qr_with_refresh(
    cfg: Config,
    config_path: str,
    to_user: str,
    success: bool,
    message: str,
) -> None:
    final_success = success
    final_message = message
    try:
        if success:
            _refresh_runtime_source(cfg, config_path)
            _send_wechat_text(
                cfg.wechat,
                "✅ Cookies 运行时配置已同步，无需重启服务。",
                to_user,
            )
    except Exception as exc:
        final_success = False
        final_message = f"运行时配置同步失败: {exc}"
        logger.error(final_message, exc_info=True)
        _send_wechat_text(
            cfg.wechat,
            f"⚠️ Cookies 已验证并写入配置，但运行时同步失败，需要重启服务。\n{exc}",
            to_user,
        )
    finally:
        _finish_manual_qr_command(final_success, final_message)


def _process_cookies_update(cfg, config_path: str, content: str, from_user_id: str) -> None:
    """处理企微发送的Cookie文本更新，验证后同步运行时。"""
    try:
        success, updated_fields, error_msg = update_cookies_from_wechat(config_path, content, cfg)
        if success and updated_fields:
            try:
                _refresh_runtime_source(cfg, config_path)
                _send_wechat_text(
                    cfg.wechat,
                    "✅ Cookies 运行时配置已同步，无需重启服务。",
                    from_user_id,
                )
            except Exception as exc:
                _send_wechat_text(
                    cfg.wechat,
                    f"⚠️ Cookies 已写入配置，但运行时同步失败，需要重启服务。\n{exc}",
                    from_user_id,
                )
        elif success and not updated_fields:
            try:
                _refresh_runtime_source(cfg, config_path)
                _send_wechat_text(
                    cfg.wechat,
                    "✅ Cookies 运行时配置已同步，无需重启服务。",
                    from_user_id,
                )
            except Exception as exc:
                _send_wechat_text(
                    cfg.wechat,
                    f"⚠️ Cookies 运行时同步失败，需要重启服务。\n{exc}",
                    from_user_id,
                )
        elif not success:
            _send_wechat_text(
                cfg.wechat,
                f"❌ Cookies 更新失败\n{error_msg}",
                from_user_id,
            )
    except Exception as e:
        logger.error(f"Process cookies error: {e}")
        _send_wechat_text(cfg.wechat, f"❌ 处理异常\n{e}", from_user_id)
    finally:
        _end_cmd()


def _current_cmd() -> str:
    with _cmd_lock:
        return _cmd_name


def _ai_command_in_progress() -> bool:
    with _cmd_lock:
        return _cmd_busy and _cmd_name.startswith("AI")


def _busy_reply() -> str:
    return f"""⏳ 系统繁忙，请稍等

当前正在执行：{_current_cmd()}

为避免资源冲突，同一时间只能执行一个操作。
请等当前任务完成后再试～"""


def _normalize_message_text(text: str) -> str:
    content = (text or "").replace("\u3000", " ").replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", content)


# ---- \u65e5\u62a5\u7f16\u8f91\u6307\u4ee4\u89e3\u6790\uff08\u6700\u65e9\u5206\u9694\u7b26\u3001\u6b63\u6587\u4e0d strip\uff09----

@dataclass
class ParsedReportCommand:
    action: str  # set_today | append | append_today | overwrite_today | view_draft | clear_draft
    body: str


_REPORT_EDIT_ACTIONS = {
    "\u8bbe\u7f6e\u65e5\u62a5": "set_today",
    "\u8ffd\u52a0\u65e5\u62a5": "append",
    "\u8ffd\u52a0\u4eca\u65e5\u65e5\u62a5": "append_today",
    "\u4fee\u6539\u4eca\u65e5\u65e5\u62a5": "overwrite_today",
    "\u67e5\u770b\u8349\u7a3f": "view_draft",
    "\u6e05\u9664\u8349\u7a3f": "clear_draft",
}


def _first_sep_index(raw: str) -> int:
    """\u8fd4\u56de\u6362\u884c/\u4e2d\u6587\u5192\u53f7/\u82f1\u6587\u5192\u53f7\u4e2d\u6700\u65e9\u51fa\u73b0\u7684\u4f4d\u7f6e\uff0c\u6ca1\u6709\u8fd4\u56de -1\u3002"""
    candidates = [i for i in (raw.find("\n"), raw.find("\uff1a"), raw.find(":")) if i != -1]
    return min(candidates) if candidates else -1


def _split_action_and_body(raw: str) -> tuple:
    idx = _first_sep_index(raw)
    if idx == -1:
        return raw.strip(), ""
    return raw[:idx].strip(), raw[idx + 1:]  # \u6b63\u6587\u539f\u6837\u4fdd\u7559\uff0c\u4e0d strip


def _extract_body_from_raw(raw: str) -> str:
    """\u4ece\u539f\u59cb\u6d88\u606f\u6309\u6700\u65e9\u5206\u9694\u7b26\u63d0\u53d6\u6b63\u6587\uff0c\u5ffd\u7565\u52a8\u4f5c\u524d\u7f00\uff0c\u539f\u6837\u4fdd\u7559\u3002"""
    if not raw:
        return ""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    idx = _first_sep_index(raw)
    if idx == -1:
        return ""
    return raw[idx + 1:]


def parse_daily_report_edit_command(raw_content):
    if not raw_content:
        return None
    raw = raw_content.replace("\r\n", "\n").replace("\r", "\n")
    if _normalize_message_text(raw).startswith("\u8bbe\u7f6e\u65e5\u62a5\u63d0\u4ea4\u65f6\u95f4"):
        return None
    action_text, body = _split_action_and_body(raw)
    if not action_text:
        return None
    action = _REPORT_EDIT_ACTIONS.get(action_text)
    if action is None:
        return None
    if action in ("view_draft", "clear_draft"):
        return ParsedReportCommand(action, "")
    return ParsedReportCommand(action, body)


def _contains_any(content: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in content for keyword in keywords)


def _is_daily_report_query(content: str) -> bool:
    return (
        "日报" in content
        and not _contains_any(content, ("状态", "提交了吗", "交了吗"))
        and _contains_any(content, ("查询", "查", "看", "查看", "看看", "看下", "读取"))
    )


def _parse_daily_report_query_date(content: str, reference_date: date) -> date | None:
    if not _is_daily_report_query(content):
        return None

    match = re.search(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)", content)
    if match:
        year, month, day = (int(value) for value in match.groups())
    else:
        match = re.search(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", content)
        if match:
            year, month, day = (int(value) for value in match.groups())
        else:
            match = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日?", content)
            if match:
                year = reference_date.year
                month, day = (int(value) for value in match.groups())
            elif "前天" in content:
                return reference_date - timedelta(days=2)
            elif "昨天" in content or "前一天" in content or "上一天" in content:
                return reference_date - timedelta(days=1)
            elif "今天" in content or "今日" in content:
                return reference_date
            else:
                return None

    try:
        return date(year, month, day)
    except ValueError:
        return None


def _has_daily_report_date_hint(content: str) -> bool:
    return bool(
        re.search(r"20\d{2}[-/]?\d{2}[-/]?\d{2}|\d{1,2}月\d{1,2}日?", content)
        or _contains_any(content, ("今天", "今日", "昨天", "前天", "前一天", "上一天"))
    )


def _normalize_daily_report_command_text(text: str, reference_date: date | None = None) -> str:
    content = _normalize_message_text(text)
    if not content:
        return content
    if "净值" in content or ("收益" in content and "日报" not in content):
        return content

    daily_related = "日报" in content or _contains_any(content, ("今天提交了吗", "今日状态", "最近记录", "提交统计"))
    if not daily_related:
        return content

    time_match = re.search(r"(\d{1,2})[:：](\d{2})", content)
    if "日报" in content and "时间" in content and time_match:
        return f"设置日报提交时间 {int(time_match.group(1)):02d}:{time_match.group(2)}"

    if "日报" in content and _contains_any(content, ("重发", "重新", "再发", "重提", "删掉重发", "删除重发", "撤回重发")):
        return "重新发送今日日报"
    if "日报" in content and _contains_any(content, ("撤回", "撤销", "删除", "删掉", "取消")):
        return "撤回今日日报"

    if _contains_any(content, ("沿用", "用昨天", "按昨天", "照昨天", "根据前一天")) and _contains_any(content, ("日报", "提交")):
        return "根据前一天内容发送"

    if "日报" in content and _contains_any(content, ("昨天", "前一天", "上次", "上一天")) and _contains_any(content, ("内容", "读取", "查看", "看看", "看下")):
        return "获取前一天日报"

    if reference_date is None:
        from src.beijing_time import today as beijing_today

        reference_date = beijing_today()
    query_date = _parse_daily_report_query_date(content, reference_date)
    if query_date:
        return f"查询日报 {query_date.isoformat()}"
    if _is_daily_report_query(content) and _has_daily_report_date_hint(content):
        return "查询日报 日期格式错误"

    if "日报" in content and _contains_any(content, ("最近", "历史", "记录")):
        return "最近记录"
    if "日报" in content and _contains_any(content, ("本月", "这个月", "月度")) and _contains_any(content, ("统计", "情况", "提交", "交")):
        return "本月统计"
    if "日报" in content and _contains_any(content, ("本周", "这周", "这个周", "周度")) and _contains_any(content, ("统计", "情况", "提交", "交")):
        return "本周统计"

    if _contains_any(content, ("今天提交了吗", "今日状态")):
        return "今日状态"
    if "日报" in content and _contains_any(content, ("状态", "提交了吗", "交了吗")):
        return "今日状态"
    if "日报" in content and _contains_any(content, ("今天", "今日")) and _contains_any(content, ("查", "看", "看看")):
        return "今日状态"

    if "日报" in content and _contains_any(content, ("昨天", "前一天", "上次", "上一天")) and _contains_any(content, ("内容", "读取", "查看", "看看", "看下")):
        return "获取前一天日报"
    if "日报" in content and _contains_any(content, ("读取", "查看", "看看", "看下", "内容")):
        return "读取日报"

    if "日报" in content and _contains_any(content, ("发送", "提交", "发一下", "帮我发", "交一下", "补交", "发了", "提交一下")):
        return "发送日报"

    return content


def _extract_command_product_code(content: str) -> str:
    match = re.search(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{3,})(?![A-Za-z0-9])", content or "")
    return match.group(1).upper() if match else ""


def _is_nav_command_message(text: str) -> bool:
    content = _normalize_message_text(text)
    if not content:
        return False
    if "日报" in content and "净值" not in content and "理财" not in content:
        return False
    if _contains_any(content, ("净值", "理财", "收益", "份额", "持仓", "画像", "预估", "涨跌")):
        return True
    if content.startswith("添加产品") or content.startswith("确认添加净值产品") or content == "取消添加净值产品":
        return True

    code = _extract_command_product_code(content)
    if not code:
        return False
    if _contains_any(content, ("添加", "新增", "加一下", "加上", "加入", "删除", "删掉", "移除", "去掉", "份额")):
        return True

    try:
        from src.nav_monitor import PROVIDER_ALIASES

        lower_content = content.lower()
        return any(alias and (alias in content or alias in lower_content) for alias in PROVIDER_ALIASES)
    except Exception:
        return False


def _help_index_text() -> str:
    return """# 📌 系统指令导航

> <font color="info">直接说自然语言</font>
> <font color="comment">日报、净值都支持口语表达</font>

**常用示例**
• **帮我发一下日报** <font color="comment">立即提交日报</font>
• **今天日报提交了吗** <font color="comment">查询日报状态</font>
• **本月日报提交情况** <font color="comment">查看月度统计</font>
• **帮我看一下最近1个月的理财净值** <font color="comment">查询近一月净值统计</font>
• **把 AF233276B 份额改成 401133.95** <font color="comment">修改持仓份额</font>
• **问助手 最近债券市场怎么样** <font color="comment">调用当前AI模型</font>

**回复数字查看**
• **1** 日报指令
• **2** 净值指令
• **3** 系统指令
• **4** 运维指令
• **5** AI助手
• **0** 系统指令大全"""


def _daily_help_text() -> str:
    return """## 📝 日报指令

> <font color="info">提交</font>
• **发送日报** <font color="comment">读取智能文档并提交</font>
• **帮我发一下日报** <font color="comment">自然语言提交</font>
• **今天日报提交一下** <font color="comment">自然语言提交</font>
• **根据前一天内容发送** <font color="comment">沿用上一条日报</font>
• **沿用昨天日报提交** <font color="comment">自然语言沿用</font>
• **重新发送今日日报** <font color="comment">删除后重新提交</font>
• **日报重新发一下** <font color="comment">自然语言重发</font>

> <font color="info">查询</font>
• **今日状态** <font color="comment">查看今天是否已提交</font>
• **今天日报提交了吗** <font color="comment">自然语言查询</font>
• **最近记录** <font color="comment">最近5条记录</font>
• **看看最近日报记录** <font color="comment">自然语言查询</font>
• **本周统计** <font color="comment">本周提交情况</font>
• **这周日报情况** <font color="comment">自然语言查询</font>
• **本月统计** <font color="comment">本月提交情况</font>
• **本月日报提交情况** <font color="comment">自然语言查询</font>

> <font color="info">内容</font>
• **读取日报** <font color="comment">读取智能文档内容</font>
• **读取一下日报内容** <font color="comment">自然语言读取</font>
• **查询2026-07-08日报** <font color="comment">查询指定日期OA日报正文</font>
• **查昨天日报** <font color="comment">支持自然语言日期</font>
• **获取前一天日报** <font color="comment">读取OA上一条</font>
• **看下昨天日报内容** <font color="comment">自然语言读取</font>

> <font color="info">管理</font>
• **撤回今日日报** <font color="comment">删除已提交日报</font>
• **把今天日报撤回** <font color="comment">自然语言撤回</font>
• **设置日报提交时间 20:00** <font color="comment">修改定时提交时间</font>
• **把日报提交时间改到 20:00** <font color="comment">自然语言修改</font>"""


def _nav_query_help_text() -> str:
    return """## 💹 理财净值指令 · 查询统计

> <font color="info">查询</font>
• **立即查询净值** <font color="comment">查询最新披露净值</font>
• **帮我看下今天净值** <font color="comment">自然语言查询最新</font>
• **查询昨天净值** <font color="comment">查询指定相对日期</font>
• **查一下昨天理财收益** <font color="comment">自然语言查询</font>
• **查询前天净值** <font color="comment">查询指定相对日期</font>
• **查询净值 20260707** <font color="comment">YYYYMMDD 指定日期</font>

> <font color="info">自然周期统计</font>
• **查询周度净值** <font color="comment">自然周起点</font>
• **上周理财表现** <font color="comment">自然语言周度</font>
• **查询月度净值** <font color="comment">自然月起点</font>
• **本月收益怎么样** <font color="comment">自然语言月度</font>
• **查询季度净值** <font color="comment">自然季度起点</font>
• **查询半年度净值** <font color="comment">自然半年起点</font>
• **查询年度净值** <font color="comment">自然年起点</font>

> <font color="info">滚动周期统计</font>
• **查询近7天净值** <font color="comment">滚动7天</font>
• **最近七天理财收益** <font color="comment">数字/汉字互通</font>
• **查询近一月净值** <font color="comment">滚动30天</font>
• **最近1个月理财净值** <font color="comment">自然语言近一月</font>
• **查询近三月净值** <font color="comment">滚动90天</font>
• **查询近半年净值** <font color="comment">滚动180天</font>
• **查询近一年净值** <font color="comment">滚动365天</font>
• **查询近两年净值** <font color="comment">滚动730天</font>
• **查询近三年净值** <font color="comment">滚动1095天</font>"""


def _nav_holdings_help_text() -> str:
    return """## 📈 理财收益预估指令

> 持仓画像默认后台使用，不主动推送；需要时可手动查看。
• **收盘后预估一下理财涨跌**  按画像和行情预估方向
• **用昨天行情预估理财涨跌**  指定使用昨日行情
• **更新持仓画像**  手动抓取最新定期报告
• **查看持仓画像**  查看全部产品画像摘要
• **查看 AF233276B 持仓画像**  查看指定产品画像
• **查看画像状态**  查看报告期和更新时间"""


def _nav_settings_help_text() -> str:
    return """## 💹 理财净值指令 · 产品设置

> <font color="info">产品管理</font>
• **查看净值配置** <font color="comment">查看监控产品与份额</font>
• **看下净值的配置** <font color="comment">自然语言查看配置</font>
• **添加净值产品 信银 AF233276B** <font color="comment">添加前会候选确认</font>
• **加一下信银 AF233276B** <font color="comment">自然语言添加</font>
• **删除净值产品 AF233276B** <font color="comment">移除监控产品</font>
• **删除一下 AF233276B 产品** <font color="comment">自然语言删除</font>
• **确认添加净值产品 1** <font color="comment">确认候选序号</font>
• **取消添加净值产品** <font color="comment">取消候选</font>

> <font color="info">份额管理</font>
• **设置净值份额 AF233276B 10000** <font color="comment">设置持仓份额</font>
• **把 AF233276B 份额改成 401133.95** <font color="comment">自然语言设置份额</font>
• **批量设置净值份额** <font color="comment">多行批量更新</font>

> <font color="info">收益修正</font>
• **设置收益 2026-07-21 150.5** <font color="comment">仅支持修改看板当前最新收益日期</font>
• **把最新收益改成200** <font color="comment">自然语言修正最新收益</font>

> <font color="info">监控开关</font>
• **开启净值监控** <font color="comment">启用工作日定时推送</font>
• **关闭净值监控** <font color="comment">停用工作日定时推送</font>"""


def _nav_help_messages() -> list[str]:
    return [_nav_query_help_text(), _nav_holdings_help_text(), _nav_settings_help_text()]


def _nav_help_text() -> str:
    return "\n\n".join(_nav_help_messages())


def _system_help_text() -> str:
    return """## ⚙️ 系统配置指令

> <font color="info">配置查看</font>
• **查看配置** <font color="comment">查看当前系统配置</font>
• **当前配置** <font color="comment">同上</font>
• **查看定时配置** <font color="comment">查看定时任务时间</font>
• **定时任务** <font color="comment">同上</font>

> <font color="info">时间设置</font>
• **设置日报提交时间 20:00** <font color="comment">日报工作日提交时间</font>
• **设置统计推送时间 21:00** <font color="comment">日报统计推送时间</font>
• **设置Cookies检查时间 09:45** <font color="comment">Cookies 自动检查时间</font>
• **设置缓存清理时间 04:00** <font color="comment">缓存清理时间</font>
• **设置净值推送时间 08:00** <font color="comment">净值工作日推送时间</font>
• **设置收益预估时间 17:30** <font color="comment">理财收益预估时间</font>

> <font color="info">登录与数据</font>
• **生成二维码** <font color="comment">扫码续期 Cookies</font>
• **重新登录** <font color="comment">重新生成登录二维码</font>
• **扫码登录** <font color="comment">同上</font>
• **检查Cookies** <font color="comment">验证智能文档读取状态</font>
• **Cookies状态** <font color="comment">同上</font>
• **更新工作日历** <font color="comment">更新节假日数据</font>"""


def _ops_help_text() -> str:
    return """## 🖥️ 运维指令

> <font color="info">服务状态</font>
• **服务器状态** <font color="comment">查看内存、磁盘、运行时间</font>
• **系统状态** <font color="comment">同上</font>
• **运行服务** <font color="comment">查看服务和资源占用</font>
• **查看日志** <font color="comment">最近日志</font>

> <font color="info">维护操作</font>
• **清理缓存** <font color="comment">清理系统缓存</font>
• **重启服务** <font color="comment">重启 daily-report</font>

> <font color="info">代理服务</font>
• **启动Xray** <font color="comment">启动代理服务</font>
• **停止Xray** <font color="comment">停止代理服务</font>
• **重启Xray** <font color="comment">重启代理服务</font>
• **启动Hy2** <font color="comment">启动 Hy2</font>
• **停止Hy2** <font color="comment">停止 Hy2</font>
• **重启Hy2** <font color="comment">重启 Hy2</font>"""


def _ai_help_text() -> str:
    return """## 🤖 AI助手

> <font color="info">模型配置</font>
• **查看AI模型** <font color="comment">查看当前主模型和备用模型</font>
• **切换AI模型 百炼** <font color="comment">检测成功后立即切换</font>
• **切换AI模型 LongCat** <font color="comment">一键切回备用模型</font>

> <font color="info">聊天</font>
• **问助手 最近债券市场怎么样** <font color="comment">单次聊天</font>
• **进入助手模式** <font color="comment">开启连续对话，保留最近10轮</font>
• **退出助手模式** <font color="comment">退出并清除本次上下文</font>

> <font color="info">线上诊断</font>
• **为什么今天没自动发送理财净值日报** <font color="comment">只读检查任务、日志和代码</font>
• **昨天日报为什么提交失败** <font color="comment">根据真实证据分析</font>

> AI不会修改代码、配置、数据库或服务。模型识别出的写操作需要回复「确认执行」。"""


def _format_schedule_config(cfg) -> str:
    s = cfg.scheduler
    nav = cfg.nav_monitor
    return f"""⏰ 定时任务配置

1️⃣ Cookies 检查：{s.cookie_check_hour:02d}:{s.cookie_check_minute:02d}（工作日）
2️⃣ 日报自动提交：{s.report_submit_hour:02d}:{s.report_submit_minute:02d}（工作日）
3️⃣ 统计自动推送：{s.stats_push_hour:02d}:{s.stats_push_minute:02d}（周日/月末）
4️⃣ 缓存自动清理：{s.cache_cleanup_hour:02d}:{s.cache_cleanup_minute:02d}（每月1号）
5️⃣ 净值自动推送：{nav.push_hour:02d}:{nav.push_minute:02d}（工作日，{'开启' if nav.enabled else '关闭'}）
6️⃣ 净值晚间补发：{nav.evening_push_hour:02d}:{nav.evening_push_minute:02d}（工作日，有当日净值才发，{'开启' if nav.evening_push_enabled else '关闭'}）
7️⃣ 理财收益预估：{nav.estimate_hour:02d}:{nav.estimate_minute:02d}（工作日，{'开启' if nav.estimate_enabled else '关闭'}）

📝 修改指令：
• 设置Cookies检查时间 09:45
• 设置日报提交时间 20:00
• 设置统计推送时间 21:00
  • 设置缓存清理时间 04:00
  • 设置净值推送时间 08:00
  • 设置收益预估时间 17:30"""


def _build_help_messages(text: str) -> list[str]:
    content = _normalize_message_text(text)
    if content in ("系统指令大全", "0"):
        return [_daily_help_text(), *_nav_help_messages(), _system_help_text(), _ops_help_text(), _ai_help_text()]
    if content in ("日报指令", "1"):
        return [_daily_help_text()]
    if content in ("净值指令", "理财指令", "2"):
        return _nav_help_messages()
    if content in ("系统指令", "3"):
        return [_system_help_text()]
    if content in ("运维指令", "4"):
        return [_ops_help_text()]
    if content in ("AI指令", "助手指令", "AI助手", "5"):
        return [_ai_help_text()]
    if content in ("指令", "帮助", "help", "菜单"):
        return [_help_index_text()]
    return []


def _is_existing_command(text: str) -> bool:
    content = _normalize_message_text(text)
    if not content:
        return False
    if is_cookies_update_message(content) or is_pending_confirmation_reply(content):
        return True
    if _build_help_messages(content):
        return True

    try:
        from src.ai_command_router import classify_canonical_command

        if classify_canonical_command(content):
            return True
    except Exception:
        logger.warning("Failed to classify deterministic command", exc_info=True)

    if _is_nav_command_message(content):
        try:
            from src.nav_monitor import parse_nav_command

            if parse_nav_command(content):
                return True
        except Exception:
            logger.warning("Failed to parse deterministic NAV command", exc_info=True)

    if _match_proxy_cmd(content, "xray") or _match_proxy_cmd(content, "hysteria", "hy2"):
        return True
    return _contains_any(
        content,
        (
            "重新登录", "扫码登录", "Cookies状态", "cookies状态", "检查cookies",
            "历史记录", "最近日报", "提交统计", "周报统计", "当前配置",
            "定时配置", "定时任务", "修改Cookies检查时间", "修改统计推送时间",
            "修改缓存清理时间", "修改日报提交时间", "修改提交时间",
            "设置提交时间", "修改日报时间", "设置日报时间", "系统状态",
            "运行进程", "清理缓存", "清除缓存", "清理临时", "最近日志", "重启服务",
            "读取智能文档内容", "读取上次日报", "上一天日报", "用昨天内容提交",
            "沿用昨日日报", "查询日报 日期格式错误", "今日日报",
        ),
    )


def create_app(cfg: Config, ai_assistant=None) -> Flask:
    app = Flask(__name__)

    if ai_assistant is None:
        try:
            from src.ai_assistant import AiAssistant

            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ai_assistant = AiAssistant.from_env(cfg, project_root)
        except Exception:
            logger.error("Failed to initialize AI assistant; AI is disabled", exc_info=True)
            ai_assistant = None

    from src.ai_assistant import AiMessageBridge

    ai_bridge = AiMessageBridge(
        ai_assistant,
        send_text=lambda text, user: _send_wechat_text(cfg.wechat, text, user),
        send_markdown=lambda text, user: _send_wechat_markdown(cfg.wechat, text, user),
        try_start_cmd=_try_start_cmd,
        end_cmd=_end_cmd,
        rename_cmd=_rename_cmd,
        busy_reply=_busy_reply,
        is_known_command=_is_existing_command,
    )

    def _check_restart_flag():
        """Check if service was restarted via command, send confirmation if so."""
        flag_path = "/tmp/daily_report_restart.flag"
        try:
            if os.path.exists(flag_path):
                with open(flag_path, "r") as f:
                    user_id = f.read().strip()
                os.remove(flag_path)
                if user_id:
                    import time
                    time.sleep(2)
                    _send_wechat_text(cfg.wechat, "✅ 服务已重启完成，运行正常！", user_id)
                    logger.info(f"Restart confirmation sent to {user_id}")
        except Exception as e:
            logger.warning(f"Failed to check restart flag: {e}")

    threading.Thread(target=_check_restart_flag, daemon=True).start()

    @app.route("/", methods=["GET"])
    def index():
        return _render_web_ui()

    @app.route("/nav", methods=["GET"])
    def nav_dashboard_page():
        from src.nav_dashboard import render_dashboard_page

        return render_dashboard_page()

    @app.route("/nav-assets/<path:filename>", methods=["GET"])
    def nav_dashboard_asset(filename):
        asset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nav_assets")
        return send_from_directory(asset_dir, filename)

    @app.route("/api/nav-dashboard", methods=["GET"])
    def nav_dashboard_data():
        from src.nav_dashboard import get_dashboard_payload, request_is_authorized

        if not request_is_authorized(request):
            return jsonify({"error": "private_link_invalid"}), 403
        return jsonify(get_dashboard_payload(cfg))

    @app.route("/api/nav-dashboard/refresh", methods=["POST"])
    def nav_dashboard_refresh():
        from src.nav_dashboard import request_is_authorized, request_manual_refresh

        if not request_is_authorized(request):
            return jsonify({"error": "private_link_invalid"}), 403
        result = request_manual_refresh(cfg)
        if result.get("accepted"):
            status_code = 202
        elif result.get("status") == "cooldown":
            status_code = 429
        else:
            status_code = 200
        return jsonify(result), status_code

    @app.route("/api/submit", methods=["POST"])
    def api_submit():
        from flask import jsonify
        data = request.get_json(silent=True) or {}
        content = data.get("message", "")
        report_date = data.get("date", "").strip() or None

        if not content.strip():
            return jsonify({"success": False, "msg": "日报内容不能为空"}), 400

        thread = threading.Thread(
            target=_handle_manual_submit,
            args=(content, report_date, cfg),
            daemon=True,
        )
        thread.start()
        return jsonify({"success": True, "msg": "请求已接收，将在后台处理"})

    @app.route("/api/wechat/callback", methods=["GET", "POST"])
    def wechat_callback():
        msg_signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")

        crypto = WechatCallbackCrypto(cfg.wechat.token, cfg.wechat.aes_key, cfg.wechat.corpid)

        if request.method == "GET":
            try:
                result = crypto.verify_url(msg_signature, timestamp, nonce, echostr)
                return result
            except Exception as e:
                logger.error(f"Wechat callback verify error: {e}")
                return "Invalid signature", 403

        try:
            encrypt_data = request.data.decode("utf-8")
            import xml.etree.ElementTree as ET
            root = ET.fromstring(encrypt_data)
            encrypt_msg = root.find("Encrypt").text if root.find("Encrypt") is not None else ""

            decrypted_xml = crypto.decrypt_message(msg_signature, timestamp, nonce, encrypt_msg)
            msg = parse_message(decrypted_xml)
            msg_type = msg.get("MsgType", "")

            if msg_type == "text":
                content = extract_text_content(msg)
                content = _normalize_daily_report_command_text(content)
                from_user = msg.get("FromUserName", "")

                if _ai_command_in_progress():
                    _send_wechat_text(cfg.wechat, _busy_reply(), from_user)
                    return "", 200

                from src.beijing_time import today as beijing_today

                prepared = ai_bridge.prepare(content, from_user, beijing_today())
                if prepared.handled:
                    return "", 200
                content = prepared.content

                if is_cookies_update_message(content):
                    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                    from_user_id = msg.get("FromUserName", "")
                    
                    if not _try_start_cmd("Cookies更新"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    def process_cookies_update():
                        _process_cookies_update(cfg, config_path, content, from_user_id)

                    _send_wechat_text(cfg.wechat, "⏳ 正在检查 Cookies 是否可用，请稍等...", from_user_id)

                    thread = threading.Thread(target=process_cookies_update, daemon=True)
                    thread.start()

                    return "", 200

                elif is_pending_confirmation_reply(content):
                    from_user_id = msg.get("FromUserName", "")
                    if is_yes(content):
                        _handle_pending_yes(cfg, from_user_id)
                    elif is_no(content):
                        _handle_pending_no(cfg, from_user_id)
                    return "", 200

                elif help_messages := _build_help_messages(content):
                    from_user_id = msg.get("FromUserName", "")
                    for help_text in help_messages:
                        _send_wechat_markdown(cfg.wechat, help_text, from_user_id)
                    return "", 200

                elif _is_nav_command_message(content):
                    from src.nav_monitor import parse_nav_command
                    nav_command = parse_nav_command(content)
                    if nav_command:
                        from_user_id = msg.get("FromUserName", "")
                        _handle_nav_command(cfg, nav_command, from_user_id)
                        return "", 200
                    from_user_id = msg.get("FromUserName", "")
                    _send_wechat_text(
                        cfg.wechat,
                        "❌ 无法识别净值指令\n\n示例：立即查询净值\n查询昨天净值\n查询净值 20260707\n查询月度净值\n查询近7天净值\n添加净值产品 信银 AF233276B\n批量设置净值份额",
                        from_user_id,
                    )
                    return "", 200

                elif content.strip() in ("指令", "帮助", "help", "菜单"):
                    from_user_id = msg.get("FromUserName", "")
                    help_text = """# 系统指令大全

## 📝 日报提交
**发送日报** — 立即提交（智能文档提取）
**根据前一天内容发送** — 用昨日内容提交
**重新发送今日日报** — 删除后重新提交

## 🔍 状态查询
**今日状态** — 查询提交状态
**最近记录** — 最近5条记录
**本周统计** / **本月统计**

## 💹 理财净值
**立即查询净值** — 查询最新净值涨跌
**查询昨天净值** / **查询前天净值**
**查询净值 20260707** — 查询指定日期
**查询周度净值** / **查询月度净值**
**查询季度净值** / **查询半年度净值** / **查询年度净值**
**查询近7天净值** / **查询近一月净值**
**查询近三月净值** / **查询近半年净值** / **查询近一年净值**
**查看净值配置**
**设置净值推送时间 08:00**
**设置净值份额 AF233276B 10000**
**批量设置净值份额**
AF233276B 10000
AF233262B 20000
**添加净值产品 信银 AF233276B**
**添加净值产品 南银 NYZY000022**
**确认添加净值产品 1** / **取消添加净值产品**
**删除净值产品 AF233276B**

## 📄 内容查询
**读取日报** — 读取智能文档
**查询2026-07-08日报** — 查询指定日期OA日报正文
**获取前一天日报** — 读取OA上一条

## 🗑️ 操作管理
**撤回今日日报** — 删除已提交日报

## ⚙️ 系统管理
**生成二维码** — 续期Cookies
**更新工作日历** — 更新下一年节假日
**检查Cookies** — 验证有效性
**查看配置** / **查看定时配置**
**设置Cookies检查时间 09:45**
**设置日报提交时间 20:00**
**设置统计推送时间 21:00**
**设置缓存清理时间 04:00**

## 🖥️ 服务器运维
**服务器状态** — 内存/磁盘/运行时间
**运行服务** — 查看所有服务及资源占用
**清理缓存** — 系统级缓存清理
**查看日志** — 最近30条日志
**重启服务** — 重启daily-report
**启动Xray** / **停止Xray** / **重启Xray**
**启动Hy2** / **停止Hy2** / **重启Hy2**

> 💡 手动触发仅发企业微信通知。"""
                    _send_wechat_markdown(cfg.wechat, help_text, from_user_id)
                    return "", 200

                elif "生成二维码" in content or "重新登录" in content or "扫码登录" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("生成二维码"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                    _send_wechat_text(cfg.wechat, "⏳ 已收到请求，正在准备企业微信扫码登录二维码...", from_user_id)
                    started = start_renew_cookies_by_qr(
                        config_path,
                        from_user_id,
                        "收到手动扫码登录指令",
                        on_complete=lambda success, message: _finish_manual_qr_with_refresh(
                            cfg,
                            config_path,
                            from_user_id,
                            success,
                            message,
                        ),
                    )
                    if not started:
                        _send_wechat_text(cfg.wechat, "ℹ️ 已有二维码登录续期任务正在进行中，请先完成当前扫码。", from_user_id)
                        _end_cmd()
                    return "", 200

                elif "更新工作日历" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("更新工作日历"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200
                    _send_wechat_text(cfg.wechat, "⏳ 正在从中国政府网获取下一年节假日安排...", from_user_id)

                    def process_calendar_update():
                        try:
                            from src.calendar_updater import update_calendar
                            ok, msg = update_calendar()
                            _send_wechat_text(cfg.wechat, msg, from_user_id)
                            from src.workday_calendar import reset_calendar
                            reset_calendar()
                        except Exception as e:
                            logger.error(f"Calendar update failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 更新失败\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_calendar_update, daemon=True)
                    thread.start()
                    return "", 200

                elif "检查Cookies" in content or "Cookies状态" in content or "cookies状态" in content or "检查cookies" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("检查Cookies"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在检查智能表格 Cookies 状态，请稍等...", from_user_id)

                    def process_check_cookies():
                        try:
                            from src.cookies_checker import check_cookies, CookiesError, CookiesNetworkError
                            try:
                                check_cookies(cfg)
                                reply = "✅ Cookies 状态正常\n智能表格读取功能可用"
                            except CookiesNetworkError as e:
                                reply = f"⚠️ 智能文档访问异常\n{e}\n\n未判定 Cookies 失效，未生成二维码。\n请稍后重试。"
                            except CookiesError as e:
                                reply = f"❌ Cookies 已过期\n{e}\n\n请发送「生成二维码」重新扫码登录"
                            except Exception as e:
                                reply = f"⚠️ 智能文档访问异常\n{e}\n\n未判定 Cookies 失效，未生成二维码。\n请稍后重试。"
                            _send_wechat_text(cfg.wechat, reply, from_user_id)
                        except Exception as e:
                            logger.error(f"Check cookies command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 检查异常\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_check_cookies, daemon=True)
                    thread.start()
                    return "", 200

                elif "重新发送今日日报" in content or "重发今日日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("重新发送今日日报"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 已收到指令，正在删除今日日报并重新提交，请稍等...", from_user_id)

                    def process_resend():
                        try:
                            success, result_msg = delete_daily_report(cfg)
                            if not success:
                                _send_wechat_text(cfg.wechat, f"❌ 删除失败，无法重发\n{result_msg}", from_user_id)
                                return
                            auto_submit_if_needed(cfg)
                        except Exception as e:
                            logger.error(f"Resend report command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 重发异常\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_resend, daemon=True)
                    thread.start()
                    return "", 200

                elif "撤回今日日报" in content or "删除今天日报" in content or "撤销今日" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("撤回今日日报"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在删除今日日报，请稍等...", from_user_id)

                    def process_delete():
                        try:
                            success, result_msg = delete_daily_report(cfg)
                            if success:
                                reply = f"✅ {result_msg}"
                            else:
                                reply = f"❌ {result_msg}"
                            _send_wechat_text(cfg.wechat, reply, from_user_id)
                        except Exception as e:
                            logger.error(f"Delete report command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 删除异常\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_delete, daemon=True)
                    thread.start()
                    return "", 200

                elif "今日状态" in content or "今天提交了吗" in content or "今日日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("今日状态"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在查询今日日报状态，请稍等...", from_user_id)

                    def process_today_status():
                        try:
                            status = get_report_status(cfg)
                            if status["exists"]:
                                content_preview = status["content"][:200] + ("..." if len(status["content"]) > 200 else "")
                                reply = f"""📊 今日日报状态

📅 日期：{status['date']}
📝 状态：{status['status']}
🏷️ 项目：{status['project'] or '-'}

📋 内容预览：
{content_preview}"""
                            else:
                                reply = f"""📊 今日日报状态

📅 日期：{status['date']}
📝 状态：❌ 未提交

💡 发送「发送日报」立即提交今日日报"""
                            _send_wechat_text(cfg.wechat, reply, from_user_id)
                        except Exception as e:
                            logger.error(f"Today status command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 查询失败\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_today_status, daemon=True)
                    thread.start()
                    return "", 200

                elif "最近记录" in content or "历史记录" in content or "最近日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("最近记录"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在查询最近提交记录，请稍等...", from_user_id)

                    def process_recent():
                        try:
                            reports = get_recent_reports(cfg, count=5)
                            if reports:
                                reply = "📋 最近5条日报记录\n\n"
                                for i, r in enumerate(reports, 1):
                                    status_icon = "✅" if r["status"] == "已审核" else "📝"
                                    reply += f"{i}. {status_icon} {r['date']} {r['status']}\n"
                                    reply += f"   {r['content_preview']}\n\n"
                            else:
                                reply = "ℹ️ 暂无日报记录"
                            _send_wechat_text(cfg.wechat, reply, from_user_id)
                        except Exception as e:
                            logger.error(f"Recent reports command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 查询失败\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_recent, daemon=True)
                    thread.start()
                    return "", 200

                elif "本月统计" in content or "提交统计" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("本月统计"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在统计本月提交情况，请稍等...", from_user_id)

                    def process_monthly():
                        try:
                            stats = get_monthly_statistics(cfg)
                            reply = f"""📊 {stats['year']}年{stats['month']}月 月报统计

📅 当月天数：{stats['total_days']} 天
📅 工作日：{stats['work_days']} 天
✅ 已提交：{stats['submitted_days']} 天
🏆 已审核：{stats['approved_days']} 天
📝 待审核：{stats['pending_days']} 天
❌ 缺交：{stats['missing_days']} 天（仅工作日）

📋 已提交日期：
"""
                            for d in stats["submitted_dates"]:
                                reply += f"  • {d}\n"
                            _send_wechat_text(cfg.wechat, reply, from_user_id)
                        except Exception as e:
                            logger.error(f"Monthly stats command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 统计失败\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_monthly, daemon=True)
                    thread.start()
                    return "", 200

                elif "查看配置" in content or "当前配置" in content:
                    from_user_id = msg.get("FromUserName", "")
                    try:
                        reply = f"""⚙️ 当前系统配置

⏰ 定时任务（工作日）：
  • Cookies 检查：{cfg.scheduler.cookie_check_hour:02d}:{cfg.scheduler.cookie_check_minute:02d}
  • 日报提交：{cfg.scheduler.report_submit_hour:02d}:{cfg.scheduler.report_submit_minute:02d}
  • 统计推送：{cfg.scheduler.stats_push_hour:02d}:{cfg.scheduler.stats_push_minute:02d}（周日/月末）

🎯 OA 目标系统：
  • 地址：{cfg.target.url}
  • 用户名：{cfg.target.username}
  • 默认项目：{cfg.target.default_project}

💬 企业微信：
  • 企业ID：{cfg.wechat.corpid if cfg.wechat else '未配置'}
  • 状态：{'已配置' if cfg.wechat and cfg.wechat.corpid else '未配置'}

📊 智能文档：
  • 文档ID：{cfg.source.doc_id}
  • 负责人：{', '.join(cfg.source.person_names)}

💹 净值监控：
  • 状态：{'开启' if cfg.nav_monitor.enabled else '关闭'}
  • 推送时间：{cfg.nav_monitor.push_hour:02d}:{cfg.nav_monitor.push_minute:02d}
  • 晚间补发：{cfg.nav_monitor.evening_push_hour:02d}:{cfg.nav_monitor.evening_push_minute:02d}（有当日净值才发，{'开启' if cfg.nav_monitor.evening_push_enabled else '关闭'}）
  • 收益预估：{cfg.nav_monitor.estimate_hour:02d}:{cfg.nav_monitor.estimate_minute:02d}（{'开启' if cfg.nav_monitor.estimate_enabled else '关闭'}）
  • 产品数量：{len(cfg.nav_monitor.products)}"""
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                    except Exception as e:
                        logger.error(f"View config command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 获取配置失败\n{e}", from_user_id)
                    return "", 200

                elif "本周统计" in content or "周报统计" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("本周统计"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在统计本周提交情况，请稍等...", from_user_id)

                    def process_weekly():
                        try:
                            stats = get_weekly_statistics(cfg)
                            reply = f"""📊 第{stats['week_num']}周 周报统计

📅 统计周期：{stats['start_date']} ~ {stats['end_date']}
📅 本周工作日：{stats['work_days']} 天
✅ 已提交：{stats['submitted_days']} 天
🏆 已审核：{stats['approved_days']} 天
📝 待审核：{stats['pending_days']} 天
❌ 缺交：{stats['missing_days']} 天（仅工作日）

📋 已提交日期：
"""
                            for d in stats["submitted_dates"]:
                                reply += f"  • {d}\n"
                            _send_wechat_text(cfg.wechat, reply, from_user_id)
                        except Exception as e:
                            logger.error(f"Weekly stats command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 统计失败\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_weekly, daemon=True)
                    thread.start()
                    return "", 200

                elif "查看定时配置" in content or "定时配置" in content or "定时任务" in content:
                    from_user_id = msg.get("FromUserName", "")
                    try:
                        reply = _format_schedule_config(cfg)
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                    except Exception as e:
                        logger.error(f"View schedule config command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 获取配置失败\n{e}", from_user_id)
                    return "", 200

                elif ("设置Cookies检查时间" in content or "修改Cookies检查时间" in content):
                    from_user_id = msg.get("FromUserName", "")
                    time_match = re.search(r'(\d{1,2})[:：](\d{2})', content)
                    if not time_match:
                        _send_wechat_text(cfg.wechat,
                            "❌ 时间格式不正确\n\n请使用格式：设置Cookies检查时间 HH:MM\n例如：设置Cookies检查时间 09:45",
                            from_user_id)
                        return "", 200
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                        _send_wechat_text(cfg.wechat, "❌ 时间范围不正确\n\n小时：0-23，分钟：0-59", from_user_id)
                        return "", 200
                    try:
                        import yaml
                        from src.config import save_config
                        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                        with open(config_path, "r", encoding="utf-8") as f:
                            original_data = yaml.safe_load(f) or {}
                        cfg.scheduler.cookie_check_hour = hour
                        cfg.scheduler.cookie_check_minute = minute
                        save_config(config_path, cfg, original_data)
                        from src.scheduler import update_runtime_config
                        update_runtime_config(cfg)
                        _send_wechat_text(cfg.wechat,
                            f"✅ Cookies 检查时间已修改\n\n新时间：{hour:02d}:{minute:02d}（工作日）\n立即生效，重启后仍然有效。",
                            from_user_id)
                        logger.info(f"Cookies check time changed to {hour:02d}:{minute:02d}")
                    except Exception as e:
                        logger.error(f"Set cookies check time failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 修改失败\n{e}", from_user_id)
                    return "", 200

                elif ("设置统计推送时间" in content or "修改统计推送时间" in content):
                    from_user_id = msg.get("FromUserName", "")
                    time_match = re.search(r'(\d{1,2})[:：](\d{2})', content)
                    if not time_match:
                        _send_wechat_text(cfg.wechat,
                            "❌ 时间格式不正确\n\n请使用格式：设置统计推送时间 HH:MM\n例如：设置统计推送时间 21:00",
                            from_user_id)
                        return "", 200
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                        _send_wechat_text(cfg.wechat, "❌ 时间范围不正确\n\n小时：0-23，分钟：0-59", from_user_id)
                        return "", 200
                    try:
                        import yaml
                        from src.config import save_config
                        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                        with open(config_path, "r", encoding="utf-8") as f:
                            original_data = yaml.safe_load(f) or {}
                        cfg.scheduler.stats_push_hour = hour
                        cfg.scheduler.stats_push_minute = minute
                        save_config(config_path, cfg, original_data)
                        from src.scheduler import update_runtime_config
                        update_runtime_config(cfg)
                        _send_wechat_text(cfg.wechat,
                            f"✅ 统计推送时间已修改\n\n新时间：{hour:02d}:{minute:02d}（周日/月末）\n立即生效，重启后仍然有效。",
                            from_user_id)
                        logger.info(f"Stats push time changed to {hour:02d}:{minute:02d}")
                    except Exception as e:
                        logger.error(f"Set stats push time failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 修改失败\n{e}", from_user_id)
                    return "", 200

                elif ("设置缓存清理时间" in content or "修改缓存清理时间" in content):
                    from_user_id = msg.get("FromUserName", "")
                    time_match = re.search(r'(\d{1,2})[:：](\d{2})', content)
                    if not time_match:
                        _send_wechat_text(cfg.wechat,
                            "❌ 时间格式不正确\n\n请使用格式：设置缓存清理时间 HH:MM\n例如：设置缓存清理时间 04:00",
                            from_user_id)
                        return "", 200
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                        _send_wechat_text(cfg.wechat, "❌ 时间范围不正确\n\n小时：0-23，分钟：0-59", from_user_id)
                        return "", 200
                    try:
                        import yaml
                        from src.config import save_config
                        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                        with open(config_path, "r", encoding="utf-8") as f:
                            original_data = yaml.safe_load(f) or {}
                        cfg.scheduler.cache_cleanup_hour = hour
                        cfg.scheduler.cache_cleanup_minute = minute
                        save_config(config_path, cfg, original_data)
                        from src.scheduler import update_runtime_config
                        update_runtime_config(cfg)
                        _send_wechat_text(cfg.wechat,
                            f"✅ 缓存清理时间已修改\n\n新时间：每月1号 {hour:02d}:{minute:02d}\n立即生效，重启后仍然有效。",
                            from_user_id)
                        logger.info(f"Cache cleanup time changed to {hour:02d}:{minute:02d}")
                    except Exception as e:
                        logger.error(f"Set cache cleanup time failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 修改失败\n{e}", from_user_id)
                    return "", 200

                elif "设置日报提交时间" in content or "修改日报提交时间" in content or "设置提交时间" in content or "修改提交时间" in content or "设置日报时间" in content:
                    from_user_id = msg.get("FromUserName", "")
                    time_match = re.search(r'(\d{1,2})[:：](\d{2})', content)
                    if not time_match:
                        _send_wechat_text(cfg.wechat,
                            "❌ 时间格式不正确\n\n请使用格式：设置日报提交时间 HH:MM\n例如：设置日报提交时间 20:00",
                            from_user_id)
                        return "", 200

                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                        _send_wechat_text(cfg.wechat,
                            "❌ 时间范围不正确\n\n小时：0-23，分钟：0-59",
                            from_user_id)
                        return "", 200

                    try:
                        import yaml
                        from src.config import save_config
                        config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")

                        with open(config_path, "r", encoding="utf-8") as f:
                            original_data = yaml.safe_load(f) or {}

                        cfg.scheduler.report_submit_hour = hour
                        cfg.scheduler.report_submit_minute = minute

                        save_config(config_path, cfg, original_data)

                        from src.scheduler import update_runtime_config
                        update_runtime_config(cfg)

                        reply = f"""✅ 日报提交时间已修改

新时间：{hour:02d}:{minute:02d}（工作日）

立即生效，重启后仍然有效。"""
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                        logger.info(f"Report submit time changed to {hour:02d}:{minute:02d} by wechat command")
                    except Exception as e:
                        logger.error(f"Set submit time command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 修改失败\n{e}", from_user_id)
                    return "", 200

                elif "服务器状态" in content or "系统状态" in content:
                    from_user_id = msg.get("FromUserName", "")
                    try:
                        project_dir = "/home/ubuntu/daily_report"

                        def fmt_size(b):
                            if b < 1024: return f"{b}B"
                            if b < 1024*1024: return f"{b/1024:.1f}KB"
                            if b < 1024*1024*1024: return f"{b/1024/1024:.1f}MB"
                            return f"{b/1024/1024/1024:.1f}GB"

                        # 运行时间
                        uptime = subprocess.run(["uptime", "-p"], capture_output=True, text=True, timeout=5).stdout.strip()

                        # CPU 负载
                        try:
                            with open("/proc/loadavg") as f:
                                load_parts = f.read().strip().split()
                                load_1m = float(load_parts[0])
                            nproc = int(subprocess.run(["nproc"], capture_output=True, text=True, timeout=5).stdout.strip())
                            cpu_pct = round(load_1m / nproc * 100)
                        except Exception:
                            load_1m = 0
                            nproc = 0
                            cpu_pct = 0

                        # 内存详情
                        mem_raw = subprocess.run(["free", "-b"], capture_output=True, text=True, timeout=5).stdout
                        mem_lines = mem_raw.strip().splitlines()
                        mem_info = {}
                        if len(mem_lines) > 1:
                            parts = mem_lines[1].split()
                            if len(parts) >= 7:
                                mem_info = {
                                    "total": int(parts[1]),
                                    "used": int(parts[2]),
                                    "free": int(parts[3]),
                                    "buff_cache": int(parts[5]),
                                    "available": int(parts[6]),
                                }

                        # Swap
                        swap_info = {}
                        if len(mem_lines) > 2:
                            parts = mem_lines[2].split()
                            if len(parts) >= 3:
                                swap_info = {"total": int(parts[1]), "used": int(parts[2])}

                        # 磁盘
                        disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5).stdout
                        disk_lines = disk.strip().splitlines()
                        disk_info = disk_lines[1].split() if len(disk_lines) > 1 else []
                        disk_used = disk_info[2] if len(disk_info) > 2 else "?"
                        disk_total = disk_info[1] if len(disk_info) > 1 else "?"

                        # 项目临时文件
                        tmp_png = subprocess.run(
                            ["find", project_dir, "-maxdepth", "1", "-name", "*.png", "-type", "f"],
                            capture_output=True, text=True, timeout=5
                        ).stdout.strip()
                        tmp_count = len(tmp_png.splitlines()) if tmp_png else 0
                        tmp_size = 0
                        if tmp_png:
                            for line in tmp_png.splitlines():
                                if os.path.exists(line):
                                    tmp_size += os.path.getsize(line)

                        # 服务日志大小
                        log_size = 0
                        for log_name in ("daily_send.log", "service.log"):
                            log_path = os.path.join(project_dir, log_name)
                            if os.path.exists(log_path):
                                log_size += os.path.getsize(log_path)

                        # APT 缓存大小
                        apt_size = 0
                        apt_dir = "/var/cache/apt/archives"
                        if os.path.isdir(apt_dir):
                            r = subprocess.run(["du", "-sb", apt_dir], capture_output=True, text=True, timeout=5)
                            if r.returncode == 0:
                                try:
                                    apt_size = int(r.stdout.strip().split()[0])
                                except (ValueError, IndexError):
                                    pass

                        # 系统日志大小
                        journal_size = 0
                        journal_dir = "/var/log/journal"
                        if os.path.isdir(journal_dir):
                            r = subprocess.run(["du", "-sb", journal_dir], capture_output=True, text=True, timeout=5)
                            if r.returncode == 0:
                                try:
                                    journal_size = int(r.stdout.strip().split()[0])
                                except (ValueError, IndexError):
                                    pass

                        reply = f"""🖥️ 服务器状态

⏱️ 运行时间：{uptime}
🖥️ CPU：{load_1m:.1f} / {nproc} 核（{cpu_pct}%）

💿 磁盘：{disk_used} / {disk_total}

💾 内存详情：
  总量：{fmt_size(mem_info.get('total', 0))}
  已用：{fmt_size(mem_info.get('used', 0))}
  缓存：{fmt_size(mem_info.get('buff_cache', 0))}（可回收）
  可用：{fmt_size(mem_info.get('available', 0))}
  Swap：{fmt_size(swap_info.get('used', 0))} / {fmt_size(swap_info.get('total', 0))}

📁 项目文件：
  截图：{tmp_count} 个（{fmt_size(tmp_size)}）
  日志：{fmt_size(log_size)}

🗄️ 系统缓存：
  APT 缓存：{fmt_size(apt_size)}
  系统日志：{fmt_size(journal_size)}

💡 发送「清理缓存」可释放以上缓存"""
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                    except Exception as e:
                        logger.error(f"Server status command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 查询失败\n{e}", from_user_id)
                    return "", 200

                elif "清理缓存" in content or "清除缓存" in content or "清理临时" in content:
                    from_user_id = msg.get("FromUserName", "")
                    try:
                        project_dir = "/home/ubuntu/daily_report"
                        script = "/usr/local/bin/clear-server-cache"
                        results = []

                        # 1. 清理服务级临时文件
                        cleaned_png = 0
                        for pattern in ("*.png",):
                            result = subprocess.run(
                                ["find", project_dir, "-maxdepth", "1", "-name", pattern, "-type", "f", "-mtime", "+1"],
                                capture_output=True, text=True, timeout=10
                            )
                            for fpath in result.stdout.strip().splitlines():
                                if fpath and os.path.exists(fpath):
                                    os.remove(fpath)
                                    cleaned_png += 1

                        # 2. 清理 __pycache__
                        pycache_result = subprocess.run(
                            ["find", project_dir, "-maxdepth", "2", "-name", "__pycache__", "-type", "d"],
                            capture_output=True, text=True, timeout=10
                        )
                        pycache_count = 0
                        for dpath in pycache_result.stdout.strip().splitlines():
                            if dpath and os.path.isdir(dpath):
                                import shutil
                                shutil.rmtree(dpath, ignore_errors=True)
                                pycache_count += 1

                        # 3. 清理服务日志
                        log_cleaned = 0
                        for log_name in ("daily_send.log", "service.log"):
                            log_path = os.path.join(project_dir, log_name)
                            if os.path.exists(log_path):
                                try:
                                    with open(log_path, "w") as f:
                                        pass
                                except PermissionError:
                                    subprocess.run(
                                        ["sudo", "truncate", "-s", "0", log_path],
                                        capture_output=True, timeout=5
                                    )
                                log_cleaned += 1

                        results.append(f"🖼️ 截图文件：清理 {cleaned_png} 个")
                        results.append(f"📦 缓存目录：清理 {pycache_count} 个 __pycache__")
                        results.append(f"📝 服务日志：清理 {log_cleaned} 个")

                        # 4. 清理 Linux 页面缓存
                        r = subprocess.run(
                            ["sudo", script, "pagecache"],
                            capture_output=True, text=True, timeout=15
                        )
                        if r.returncode == 0:
                            results.append("♻️ 系统页面缓存：已清理")
                        else:
                            results.append("♻️ 系统页面缓存：清理失败")

                        # 5. 清理 APT 包缓存
                        r = subprocess.run(
                            ["sudo", script, "apt"],
                            capture_output=True, text=True, timeout=30
                        )
                        results.append("📦 APT 包缓存：已清理")

                        # 6. 清理系统日志（7天前）
                        r = subprocess.run(
                            ["sudo", script, "journal"],
                            capture_output=True, text=True, timeout=15
                        )
                        results.append("📋 系统日志：已清理（保留7天）")

                        # 7. 清理 /tmp 临时文件（7天前）
                        r = subprocess.run(
                            ["sudo", script, "tmp"],
                            capture_output=True, text=True, timeout=10
                        )
                        results.append("🗂️ /tmp 临时文件：已清理（保留7天）")

                        # 获取清理后内存信息
                        mem_after = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5).stdout
                        mem_lines = mem_after.strip().splitlines()
                        if len(mem_lines) > 1:
                            mem_parts = mem_lines[1].split()
                            if len(mem_parts) >= 4:
                                mem_avail = mem_parts[6] if len(mem_parts) > 6 else mem_parts[3]
                                results.append(f"\n💾 可用内存：{mem_avail}MB")

                        reply = "🧹 系统级缓存清理完成\n\n" + "\n".join(results)
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                    except Exception as e:
                        logger.error(f"Clear cache command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 清理失败\n{e}", from_user_id)
                    return "", 200

                elif "查看日志" in content or "最近日志" in content:
                    from_user_id = msg.get("FromUserName", "")
                    try:
                        log_path = "/home/ubuntu/daily_report/daily_send.log"
                        if os.path.exists(log_path):
                            result = subprocess.run(
                                ["tail", "-n", "30", log_path],
                                capture_output=True, text=True, timeout=5
                            )
                            log_content = result.stdout.strip()
                            if log_content:
                                reply = f"📝 最近日志（最后30行）：\n\n{log_content}"
                            else:
                                reply = "📝 日志文件为空"
                        else:
                            reply = "📝 日志文件不存在"
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                    except Exception as e:
                        logger.error(f"View log command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 读取日志失败\n{e}", from_user_id)
                    return "", 200

                elif "重启服务" in content:
                    from_user_id = msg.get("FromUserName", "")
                    flag_path = "/tmp/daily_report_restart.flag"
                    try:
                        with open(flag_path, "w") as f:
                            f.write(from_user_id)
                    except Exception as e:
                        logger.warning(f"Failed to write restart flag: {e}")
                    _send_wechat_text(cfg.wechat, "✅ 服务即将在3秒后重启，重启完成后会自动发送确认消息。\n\n如长时间未恢复，请SSH手动重启：\nsudo systemctl restart daily-report", from_user_id)

                    def delayed_restart():
                        import time
                        time.sleep(3)
                        os._exit(0)

                    thread = threading.Thread(target=delayed_restart, daemon=True)
                    thread.start()
                    return "", 200

                elif _match_proxy_cmd(content, "xray"):
                    _handle_proxy_cmd(cfg, msg, "xray", "Xray")
                    return "", 200

                elif _match_proxy_cmd(content, "hysteria", "hy2"):
                    _handle_proxy_cmd(cfg, msg, "hysteria", "Hysteria")
                    return "", 200

                elif "运行服务" in content or "运行进程" in content:
                    from_user_id = msg.get("FromUserName", "")
                    try:
                        # 获取运行中的 systemd 服务
                        svc_result = subprocess.run(
                            ["systemctl", "list-units", "--type=service", "--state=running", "--no-pager", "--no-legend"],
                            capture_output=True, text=True, timeout=5
                        )
                        running_services = []
                        for line in svc_result.stdout.strip().splitlines():
                            parts = line.split()
                            if parts:
                                running_services.append(parts[0].replace(".service", ""))

                        # 获取进程内存排序
                        ps_result = subprocess.run(
                            ["ps", "aux", "--sort=-rss"],
                            capture_output=True, text=True, timeout=5
                        )
                        process_lines = ps_result.stdout.strip().splitlines()[1:]  # skip header

                        def fmt_mem(kb):
                            if kb < 1024: return f"{kb}KB"
                            return f"{kb/1024:.0f}MB"

                        # 服务名称映射
                        svc_names = {
                            "daily-report": "日报系统",
                            "status-page": "状态页面",
                            "xray": "Xray代理",
                            "hysteria": "Hysteria代理",
                            "ssh": "SSH服务",
                            "snapd": "Snap包管理",
                            "chrony": "时间同步",
                            "cron": "定时任务",
                            "rsyslog": "系统日志",
                            "systemd-journald": "日志服务",
                            "systemd-networkd": "网络管理",
                            "systemd-resolved": "DNS解析",
                            "systemd-logind": "登录管理",
                            "systemd-udevd": "设备管理",
                            "polkit": "权限管理",
                            "dbus": "消息总线",
                            "irqbalance": "中断均衡",
                            "packagekit": "包管理",
                            "networkd-dispatcher": "网络调度",
                        }

                        # 构建进程内存映射（按服务分组）
                        service_mem = {}
                        other_procs = []
                        for line in process_lines:
                            cols = line.split(None, 10)
                            if len(cols) < 11:
                                continue
                            rss_kb = int(cols[5])
                            cmd = cols[10]
                            if rss_kb < 1000:  # 跳过 <1MB 的进程
                                continue
                            matched = False
                            for svc in running_services:
                                if svc in cmd or cmd.startswith(svc):
                                    service_mem[svc] = service_mem.get(svc, 0) + rss_kb
                                    matched = True
                                    break
                            if not matched:
                                # 尝试匹配特殊进程
                                if "daily_report/main.py" in cmd:
                                    service_mem["daily-report"] = service_mem.get("daily-report", 0) + rss_kb
                                elif "status_page.py" in cmd:
                                    service_mem["status-page"] = service_mem.get("status-page", 0) + rss_kb
                                elif "xray" in cmd:
                                    service_mem["xray"] = service_mem.get("xray", 0) + rss_kb
                                elif "hysteria" in cmd:
                                    service_mem["hysteria"] = service_mem.get("hysteria", 0) + rss_kb
                                elif "sshd" in cmd:
                                    service_mem["ssh"] = service_mem.get("ssh", 0) + rss_kb
                                elif "snapd" in cmd:
                                    service_mem["snapd"] = service_mem.get("snapd", 0) + rss_kb
                                elif "chronyd" in cmd or "chrony" in cmd:
                                    service_mem["chrony"] = service_mem.get("chrony", 0) + rss_kb
                                elif "packagekit" in cmd:
                                    service_mem["packagekit"] = service_mem.get("packagekit", 0) + rss_kb
                                elif "networkd-dispatcher" in cmd:
                                    service_mem["networkd-dispatcher"] = service_mem.get("networkd-dispatcher", 0) + rss_kb

                        # 排序
                        sorted_services = sorted(service_mem.items(), key=lambda x: x[1], reverse=True)

                        # 获取系统内存信息
                        mem_raw = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5).stdout
                        mem_lines = mem_raw.strip().splitlines()
                        sys_total = sys_used = sys_avail = 0
                        if len(mem_lines) > 1:
                            parts = mem_lines[1].split()
                            if len(parts) >= 7:
                                sys_total = int(parts[1])
                                sys_used = int(parts[2])
                                sys_avail = int(parts[6])

                        reply_lines = ["🖥️ 运行服务及资源占用\n"]
                        for svc, mem_kb in sorted_services:
                            name = svc_names.get(svc, svc)
                            reply_lines.append(f"  {name}：{fmt_mem(mem_kb)}")

                        total_mem = sum(mem_kb for _, mem_kb in sorted_services)
                        reply_lines.append(f"\n📊 以上合计：{fmt_mem(total_mem)}")
                        reply_lines.append(f"💾 内存：总计 {sys_total}MB / 已用 {sys_used}MB / 可用 {sys_avail}MB")
                        reply_lines.append(f"🔗 端口：80(状态页) 8080(日报) 443(Xray) 22/2222(SSH)")

                        _send_wechat_text(cfg.wechat, "\n".join(reply_lines), from_user_id)
                    except Exception as e:
                        logger.error(f"Running services command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 查询失败\n{e}", from_user_id)
                    return "", 200

                elif "发送日报" in content or "提交日报" in content or "立即发送日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("发送日报"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 已收到发送日报指令，正在读取智能文档并提交日报，请稍等...", from_user_id)

                    def process_send_report():
                        try:
                            auto_submit_if_needed(cfg)
                        except Exception as e:
                            logger.error(f"Wechat send report command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 发送日报任务异常\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_send_report, daemon=True)
                    thread.start()
                    return "", 200

                elif content == "查询日报 日期格式错误":
                    from_user_id = msg.get("FromUserName", "")
                    _send_wechat_text(
                        cfg.wechat,
                        "❌ 未识别到有效日期\n例如：查询日报 20260709",
                        from_user_id,
                    )
                    return "", 200

                elif query_match := re.fullmatch(r"查询日报 (\d{4}-\d{2}-\d{2})", content):
                    from_user_id = msg.get("FromUserName", "")
                    report_date = query_match.group(1)
                    if not _try_start_cmd("查询指定日期日报"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, f"⏳ 正在查询 {report_date} 的日报，请稍等...", from_user_id)

                    def process_query_daily_report():
                        try:
                            from src.target import get_report_content_by_date

                            report_content = get_report_content_by_date(cfg, report_date)
                            reply = report_content or f"ℹ️ 未找到 {report_date} 的日报正文"
                        except Exception as e:
                            logger.error(f"Query daily report failed: {e}", exc_info=True)
                            reply = f"❌ 查询失败\n{e}"
                        finally:
                            _end_cmd()
                        _send_wechat_text(cfg.wechat, reply, from_user_id)

                    threading.Thread(target=process_query_daily_report, daemon=True).start()
                    return "", 200

                elif "读取智能文档内容" in content or "读取日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("读取智能文档"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在读取智能文档，请稍等...", from_user_id)

                    def process_extract():
                        try:
                            tasks = extract_tasks(cfg.source)
                            if tasks:
                                reply = f"📋 今日日报内容（共{len(tasks)}项）：\n\n"
                                for i, task in enumerate(tasks, 1):
                                    reply += f"{i}. {task}\n"
                            else:
                                reply = "ℹ️ 未提取到符合条件的任务内容\n\n请检查：\n1. 负责人是否正确\n2. 任务状态是否为「进行中/已完成」"
                        except ExtractError as e:
                            reply = f"❌ 读取失败\n{e}\n\n请检查 Cookies 是否有效"
                        except Exception as e:
                            reply = f"❌ 读取异常\n{str(e)}"
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                        _end_cmd()

                    thread = threading.Thread(target=process_extract, daemon=True)
                    thread.start()
                    return "", 200

                elif "获取前一天日报" in content or "读取上次日报" in content or "上一天日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("获取前一天日报"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在从 OA 系统读取前一天日报内容，请稍等...", from_user_id)

                    def process_get_previous():
                        try:
                            from src.beijing_time import today_str
                            today = today_str()
                            content = get_previous_report_content(cfg, before_date=today)
                            if content:
                                reply = f"📋 前一天日报内容：\n\n{content}"
                            else:
                                reply = "ℹ️ 未找到前一天的日报内容"
                        except Exception as e:
                            reply = f"❌ 读取失败\n{str(e)}"
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                        _end_cmd()

                    thread = threading.Thread(target=process_get_previous, daemon=True)
                    thread.start()
                    return "", 200

                elif "根据前一天内容发送" in content or "用昨天内容提交" in content or "沿用昨日日报" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("根据前一天内容发送"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 已收到指令，正在读取前一天日报内容并提交今日日报，请稍等...", from_user_id)

                    def process_submit_previous():
                        try:
                            from src.beijing_time import today_str
                            today = today_str()
                            prev_content = get_previous_report_content(cfg, before_date=today)
                            if not prev_content:
                                _send_wechat_text(cfg.wechat, "❌ 未找到前一天的日报内容，无法提交", from_user_id)
                                return
                            _submit_and_notify(
                                prev_content,
                                cfg,
                                report_source="previous_report",
                                report_meta={
                                    "smart_doc_status": "not_used",
                                    "smart_doc_error": "",
                                },
                            )
                        except Exception as e:
                            logger.error(f"Submit with previous content failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 提交失败\n{str(e)}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_submit_previous, daemon=True)
                    thread.start()
                    return "", 200

            return ""
        except Exception as e:
            logger.error(f"Wechat callback error: {e}", exc_info=True)
            return "", 500

    return app


def _build_text_reply(msg: dict, content: str) -> str:
    from_user = msg.get("ToUserName", "")
    to_user = msg.get("FromUserName", "")
    create_time = msg.get("CreateTime", "")
    return f"""<xml>
<ToUserName><![CDATA[{to_user}]]></ToUserName>
<FromUserName><![CDATA[{from_user}]]></FromUserName>
<CreateTime>{create_time}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""


def _handle_nav_command(cfg: Config, nav_command, from_user_id: str):
    from src.nav_monitor import (
        build_add_product_candidates,
        cancel_pending_nav_add,
        confirm_pending_nav_add,
        delete_nav_product,
        format_nav_config,
        format_product_candidates,
        get_provider,
        push_nav_period_report,
        push_nav_report,
        save_pending_nav_add,
        set_nav_product_shares_batch,
        set_nav_product_shares,
    )

    action = nav_command.action

    if action == "view_config":
        _send_wechat_markdown(cfg.wechat, format_nav_config(cfg), from_user_id)
        return

    if action == "view_holdings":
        from src.nav_holdings import format_holdings_profiles

        _send_wechat_markdown(cfg.wechat, format_holdings_profiles(code=nav_command.code), from_user_id)
        return

    if action == "view_holdings_status":
        from src.nav_holdings import format_holdings_status

        _send_wechat_markdown(cfg.wechat, format_holdings_status(), from_user_id)
        return

    if action == "estimate_holdings":
        if not _try_start_cmd("理财收益预估"):
            _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
            return
        market_date = nav_command.target_date
        if market_date:
            loading_text = f"⏳ 正在结合持仓画像和 {market_date:%Y-%m-%d} 行情预估，请稍等..."
        else:
            loading_text = "⏳ 正在结合持仓画像和市场行情预估，请稍等..."
        _send_wechat_text(cfg.wechat, loading_text, from_user_id)

        def process_estimate():
            try:
                from src.nav_holdings import push_estimate_report, refresh_quarterly_profiles_if_due

                refresh_quarterly_profiles_if_due(cfg, notify=False)
                push_estimate_report(cfg, to_user=from_user_id, market_date=market_date)
            except Exception as e:
                logger.error(f"NAV estimate command failed: {e}", exc_info=True)
                _send_wechat_text(cfg.wechat, f"❌ 理财收益预估失败\n{e}", from_user_id)
            finally:
                _end_cmd()

        threading.Thread(target=process_estimate, daemon=True).start()
        return

    if action == "update_holdings":
        if not _try_start_cmd("更新持仓画像"):
            _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
            return
        _send_wechat_text(cfg.wechat, "⏳ 正在检查官网最新定期报告并更新画像，请稍等...", from_user_id)

        def process_update_holdings():
            try:
                from src.nav_holdings import format_profile_update_result, refresh_all_profiles

                profiles = refresh_all_profiles(cfg)
                _send_wechat_markdown(cfg.wechat, format_profile_update_result(profiles), from_user_id)
            except Exception as e:
                logger.error(f"Update holdings command failed: {e}", exc_info=True)
                _send_wechat_text(cfg.wechat, f"❌ 更新持仓画像失败\n{e}", from_user_id)
            finally:
                _end_cmd()

        threading.Thread(target=process_update_holdings, daemon=True).start()
        return

    if action == "enable":
        cfg.nav_monitor.enabled = True
        _persist_runtime_config(cfg)
        _send_wechat_text(cfg.wechat, "✅ 净值监控已开启", from_user_id)
        return

    if action == "disable":
        cfg.nav_monitor.enabled = False
        _persist_runtime_config(cfg)
        _send_wechat_text(cfg.wechat, "✅ 净值监控已关闭", from_user_id)
        return

    if action == "set_time":
        cfg.nav_monitor.push_hour = nav_command.hour
        cfg.nav_monitor.push_minute = nav_command.minute
        _persist_runtime_config(cfg)
        _send_wechat_text(
            cfg.wechat,
            f"✅ 净值推送时间已修改\n\n新时间：{nav_command.hour:02d}:{nav_command.minute:02d}（工作日）\n立即生效，重启后仍然有效。",
            from_user_id,
        )
        return

    if action == "set_estimate_time":
        cfg.nav_monitor.estimate_hour = nav_command.hour
        cfg.nav_monitor.estimate_minute = nav_command.minute
        _persist_runtime_config(cfg)
        _send_wechat_text(
            cfg.wechat,
            f"✅ 收益预估时间已修改\n\n新时间：{nav_command.hour:02d}:{nav_command.minute:02d}（工作日）\n立即生效，重启后仍然有效。",
            from_user_id,
        )
        return

    if action == "delete_product":
        ok, message = delete_nav_product(cfg, nav_command.code)
        if ok:
            _persist_runtime_config(cfg)
        _send_wechat_text(cfg.wechat, ("✅ " if ok else "❌ ") + message, from_user_id)
        return

    if action == "set_shares":
        ok, message = set_nav_product_shares(cfg, nav_command.code, nav_command.shares)
        if ok:
            _persist_runtime_config(cfg)
        _send_wechat_text(cfg.wechat, ("✅ " if ok else "❌ ") + message, from_user_id)
        return

    if action == "set_daily_profit":
        from src.nav_dashboard import NavDashboardStore

        try:
            store = NavDashboardStore()
            target_date = nav_command.target_date
            if target_date is None:
                entries = store.state.get("profit_entries", [])
                max_nav_date = max((str(e.get("nav_date") or "") for e in entries), default="")
                if not max_nav_date:
                    _send_wechat_text(cfg.wechat, "❌ 看板尚无收益数据，无法设置", from_user_id)
                    return
                from datetime import date as _date

                target_date = _date.fromisoformat(max_nav_date)
            store.set_manual_daily_profit(target_date, nav_command.amount)
            amount_text = format(nav_command.amount, "f")
            _send_wechat_text(
                cfg.wechat,
                f"✅ 收益已设置\n\n📅 日期：{target_date:%Y-%m-%d}\n💰 收益：{amount_text} 元\n\n仅影响看板展示，不影响自动计算的净值流水。",
                from_user_id,
            )
        except Exception as e:
            logger.error(f"Set daily profit failed: {e}", exc_info=True)
            _send_wechat_text(cfg.wechat, f"❌ 设置收益失败\n{e}", from_user_id)
        return

    if action == "set_daily_profit_invalid_date":
        _send_wechat_text(
            cfg.wechat,
            f"❌ 日期格式无效：{nav_command.query}\n\n支持格式：\n• 设置收益 2026-07-21 150.5\n• 设置收益 昨天 100\n• 设置收益 20260721 -80",
            from_user_id,
        )
        return

    if action == "set_daily_profit_usage":
        _send_wechat_text(
            cfg.wechat,
            "❌ 设置收益格式不正确\n\n格式：设置收益 日期 金额\n\n示例：\n• 设置收益 2026-07-21 150.5\n• 设置收益 昨天 -80\n• 设置收益 20260721 200",
            from_user_id,
        )
        return

    if action == "set_shares_batch":
        ok, message = set_nav_product_shares_batch(
            cfg,
            nav_command.share_updates,
            nav_command.share_errors,
        )
        if ok:
            _persist_runtime_config(cfg)
        _send_wechat_markdown(cfg.wechat, message, from_user_id)
        return

    if action == "confirm_add":
        if not _try_start_cmd("确认添加净值产品"):
            _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
            return
        try:
            ok, message = confirm_pending_nav_add(cfg, nav_command.index)
            if ok:
                _persist_runtime_config(cfg)
            _send_wechat_text(cfg.wechat, ("✅ " if ok else "❌ ") + message, from_user_id)
        except Exception as e:
            logger.error(f"Confirm NAV add failed: {e}", exc_info=True)
            _send_wechat_text(cfg.wechat, f"❌ 添加净值产品失败\n{e}", from_user_id)
        finally:
            _end_cmd()
        return

    if action == "cancel_add":
        _send_wechat_text(cfg.wechat, cancel_pending_nav_add(), from_user_id)
        return

    if action == "unknown_provider":
        _send_wechat_text(cfg.wechat, "❌ 暂不支持该理财机构\n\n目前支持：信银、南银", from_user_id)
        return

    if action == "add_product_usage":
        _send_wechat_text(
            cfg.wechat,
            "❌ 添加净值产品格式不正确\n\n示例：\n添加净值产品 信银 AF233276B\n添加净值产品 南银 NYZY000022\n添加产品 信银 AF233276B",
            from_user_id,
        )
        return

    if action in ("query_latest", "query_date", "query_period"):
        if not _try_start_cmd("净值查询"):
            _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
            return
        target_date = nav_command.target_date
        _send_wechat_text(cfg.wechat, "⏳ 正在查询净值，请稍等...", from_user_id)

        def process_nav_query():
            try:
                if action == "query_period":
                    push_nav_period_report(cfg, nav_command.period, to_user=from_user_id)
                else:
                    push_nav_report(cfg, target_date=target_date, to_user=from_user_id)
            except Exception as e:
                logger.error(f"NAV query command failed: {e}", exc_info=True)
                _send_wechat_text(cfg.wechat, f"❌ 净值查询失败\n{e}", from_user_id)
            finally:
                _end_cmd()

        threading.Thread(target=process_nav_query, daemon=True).start()
        return

    if action == "add_product":
        if not _try_start_cmd("添加净值产品"):
            _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
            return
        _send_wechat_text(cfg.wechat, "⏳ 正在查询候选产品，请稍等...", from_user_id)

        def process_nav_add():
            try:
                provider = get_provider(nav_command.provider)
                candidates = build_add_product_candidates(provider, nav_command.query)
                if candidates:
                    save_pending_nav_add(candidates)
                _send_wechat_markdown(cfg.wechat, format_product_candidates(candidates), from_user_id)
            except Exception as e:
                logger.error(f"NAV add command failed: {e}", exc_info=True)
                _send_wechat_text(cfg.wechat, f"❌ 查询候选产品失败\n{e}", from_user_id)
            finally:
                _end_cmd()

        threading.Thread(target=process_nav_add, daemon=True).start()
        return

    _send_wechat_text(cfg.wechat, "❌ 暂不支持该净值指令", from_user_id)


def _persist_runtime_config(cfg: Config):
    import yaml
    from src.config import save_config
    from src.scheduler import update_runtime_config

    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        original_data = yaml.safe_load(f) or {}
    save_config(config_path, cfg, original_data)
    _sync_dashboard_after_config_save(cfg)
    update_runtime_config(cfg)


def _sync_dashboard_after_config_save(cfg: Config):
    try:
        from src.nav_dashboard import sync_dashboard_portfolio

        sync_dashboard_portfolio(cfg)
    except Exception as exc:
        logger.warning("NAV dashboard portfolio sync failed: %s", exc, exc_info=True)


def _handle_manual_submit(content: str, report_date: str, cfg: Config):
    try:
        success, msg, report_info = submit_daily_report(content, cfg, report_date=report_date)
        if success:
            notify_report_success(
                cfg,
                content,
                report_info,
                report_date=report_date,
                report_source="manual",
                smart_doc_status="not_used",
            )
        else:
            notify_report_failure(
                cfg,
                msg,
                report_date=report_date,
                report_source="manual",
                smart_doc_status="not_used",
                screenshot=report_info.get("screenshot"),
            )
    except Exception as e:
        logger.error(f"Manual submit error: {e}", exc_info=True)
        notify_report_failure(
            cfg,
            f"网页手动提交异常: {e}",
            report_date=report_date,
            report_source="manual",
            smart_doc_status="not_used",
        )


def _render_web_ui():
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日报提交系统</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: Microsoft YaHei, sans-serif; background: #f0f2f5; min-height: 100vh; }}
.header {{ background: linear-gradient(135deg, #1890ff, #096dd9); color: white; padding: 24px 32px; }}
.header h1 {{ font-size: 22px; margin-bottom: 4px; }}
.header p {{ opacity: 0.85; font-size: 14px; }}
.container {{ max-width: 720px; margin: 24px auto; padding: 0 16px; }}
.card {{ background: white; border-radius: 8px; padding: 24px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.card h2 {{ font-size: 16px; margin-bottom: 16px; color: #333; border-left: 3px solid #1890ff; padding-left: 10px; }}
.form-group {{ margin-bottom: 16px; }}
.form-group label {{ display: block; margin-bottom: 6px; color: #555; font-size: 14px; }}
.form-group input, .form-group textarea {{ width: 100%; padding: 10px 12px; border: 1px solid #d9d9d9; border-radius: 4px; font-size: 14px; font-family: Microsoft YaHei, sans-serif; transition: border 0.2s; }}
.form-group input:focus, .form-group textarea:focus {{ border-color: #1890ff; outline: none; box-shadow: 0 0 0 2px rgba(24,144,255,0.2); }}
.form-group textarea {{ min-height: 150px; resize: vertical; }}
.btn {{ display: inline-block; padding: 10px 32px; border: none; border-radius: 4px; font-size: 15px; cursor: pointer; font-family: Microsoft YaHei, sans-serif; transition: all 0.2s; }}
.btn-primary {{ background: #1890ff; color: white; }}
.btn-primary:hover {{ background: #096dd9; }}
.btn-primary:disabled {{ background: #91d5ff; cursor: not-allowed; }}
.result {{ margin-top: 12px; padding: 12px; border-radius: 4px; display: none; font-size: 14px; }}
.result.success {{ display: block; background: #f6ffed; border: 1px solid #b7eb8f; color: #52c41a; }}
.result.error {{ display: block; background: #fff2f0; border: 1px solid #ffccc7; color: #ff4d4f; }}
.result.loading {{ display: flex; align-items: center; gap: 8px; background: #e6f7ff; border: 1px solid #91d5ff; color: #1890ff; }}
.spinner {{ display: inline-block; width: 16px; height: 16px; border: 2px solid #91d5ff; border-top-color: #1890ff; border-radius: 50%; animation: spin 0.6s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.quick-dates {{ display: flex; gap: 8px; margin-top: 4px; }}
.quick-date {{ padding: 4px 12px; border: 1px solid #d9d9d9; border-radius: 4px; background: white; cursor: pointer; font-size: 13px; transition: all 0.2s; }}
.quick-date:hover {{ border-color: #1890ff; color: #1890ff; }}
.quick-date.active {{ background: #1890ff; color: white; border-color: #1890ff; }}
</style>
</head>
<body>
<div class="header">
  <h1>日报提交系统</h1>
  <p>OA 企业信息管理平台 — 自动填报服务</p>
</div>
<div class="container">
  <div class="card">
    <h2>填写日报</h2>
    <div class="form-group">
      <label>日报日期</label>
      <input type="date" id="reportDate" value="{today_str}">
      <div class="quick-dates">
        <button class="quick-date active" onclick="setQuickDate('{today_str}', this)">今天</button>
        <button class="quick-date" onclick="setQuickDate(getOffsetDate(-1), this)">昨天</button>
        <button class="quick-date" onclick="setQuickDate(getOffsetDate(-2), this)">前天</button>
        <button class="quick-date" onclick="setQuickDate(getLastFriday(), this)">上周五</button>
      </div>
    </div>
    <div class="form-group">
      <label>日报内容</label>
      <textarea id="reportContent" placeholder="请输入日报内容，每行一条...">1. 完成今日开发任务
2. 系统功能测试与验证</textarea>
    </div>
    <button class="btn btn-primary" id="submitBtn" onclick="submitReport()">提交日报</button>
    <div class="result" id="result"></div>
  </div>
</div>
<script>
function formatLocalDate(d) {{
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return `${{yyyy}}-${{mm}}-${{dd}}`;
}}
function getOffsetDate(offset) {{
  const d = new Date(new Date().getTime() + offset * 86400000);
  return formatLocalDate(d);
}}
function getLastFriday() {{
  const d = new Date();
  const day = d.getDay();
  const diff = day <= 5 ? day + 5 : day - 2 + 7;
  d.setDate(d.getDate() - diff);
  return formatLocalDate(d);
}}
function setQuickDate(dateStr, el) {{
  document.getElementById('reportDate').value = dateStr;
  document.querySelectorAll('.quick-date').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}}
async function submitReport() {{
  const btn = document.getElementById('submitBtn');
  const result = document.getElementById('result');
  const content = document.getElementById('reportContent').value.trim();
  const date = document.getElementById('reportDate').value;
  if (!content) {{
    result.className = 'result loading';
    result.textContent = '请填写日报内容';
    return;
  }}
  btn.disabled = true;
  result.className = 'result loading';
  result.textContent = '提交中请稍候...';
  try {{
    const resp = await fetch('/api/submit', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ message: content, date: date }})
    }});
    const data = await resp.json();
    if (data.success) {{
      result.className = 'result success';
      result.textContent = '请求已提交！系统将在后台处理，完成后会发送企业微信通知。日期: ' + (date || '当天');
    }} else {{
      result.className = 'result error';
      result.textContent = '失败: ' + data.msg;
    }}
  }} catch(e) {{
    result.className = 'result error';
    result.textContent = '网络错误: ' + e.message;
  }}
  btn.disabled = false;
}}
</script>
</body>
</html>"""


def _start_report_cookie_renewal(cfg: Config, cookies_error: Exception) -> None:
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.yaml",
    )
    completion_lock = threading.Lock()
    completion_handled = False

    def on_complete(success: bool, message: str) -> None:
        nonlocal completion_handled
        with completion_lock:
            if completion_handled:
                return
            completion_handled = True

        if not success:
            _send_wechat_text(
                cfg.wechat,
                f"❌ Cookies 续期未完成\n{message}\n今日日报尚未提交。",
            )
            return

        try:
            _refresh_runtime_source(cfg, config_path)
            auto_submit_if_needed(cfg, precheck_cookies=False)
        except Exception as exc:
            logger.error(
                "Cookie renewal succeeded but runtime refresh failed: %s",
                exc,
                exc_info=True,
            )
            _send_wechat_text(
                cfg.wechat,
                f"⚠️ Cookies 已写入配置，但运行时同步失败，需要重启服务。\n今日日报尚未提交。\n{exc}",
            )

    started = start_renew_cookies_by_qr(
        config_path,
        getattr(cfg.wechat, "to_user", None),
        f"定时日报提交前检查发现 Cookies 已过期：{cookies_error}",
        on_complete=on_complete,
    )
    with completion_lock:
        callback_was_handled = completion_handled
    if not started and not callback_was_handled:
        _send_wechat_text(
            cfg.wechat,
            "ℹ️ 当前续期任务完成后请再次发送日报。",
        )


def auto_submit_if_needed(cfg: Config, *, precheck_cookies: bool = True) -> bool:
    """Called by scheduler at 17:45. Auto-submits if no manual report was provided."""
    if precheck_cookies:
        from src.cookies_checker import CookiesError, CookiesNetworkError, check_cookies

        try:
            check_cookies(cfg)
        except CookiesNetworkError as exc:
            logger.warning("Report submission smart sheet network check failed: %s", exc)
            notify_report_failure(
                cfg,
                f"智能文档网络异常，系统已重试一次；未判定 Cookies 失效，未生成二维码。错误：{exc}",
                report_source="generation_failed",
                smart_doc_status="error",
                smart_doc_error=str(exc),
            )
            return False
        except CookiesError as exc:
            logger.warning("Report submission Cookie preflight failed: %s", exc)
            _start_report_cookie_renewal(cfg, exc)
            return False

    logger.info("No manual report today — extracting from smart sheet...")
    try:
        from src.report_builder import build_report_with_meta
        report, source, meta = build_report_with_meta(cfg)
        logger.info(f"Auto report content source: {source}")
    except Exception as e:
        logger.error(f"Auto-extraction failed: {e}")
        notify_report_failure(
            cfg,
            f"自动提取失败: {e}",
            report_source=getattr(e, "report_source", "generation_failed"),
            smart_doc_status=getattr(e, "smart_doc_status", "unknown"),
            smart_doc_error=getattr(e, "smart_doc_error", ""),
        )
        return False

    # 智能文档过期(cookies失效)时交互确认，不直接回退
    if source == "previous_report" and meta.get("smart_doc_status") == "error":
        smart_error = meta.get("smart_doc_error", "")
        if _is_cookies_expiry_error(smart_error):
            save_pending(report, source, meta)
            _send_pending_confirm_message(cfg, smart_error)
            return False

    return _submit_and_notify(report, cfg, source, meta)


def _submit_and_notify(content: str, cfg: Config, report_source: str = None,
                       report_meta: dict = None) -> bool:
    report_meta = report_meta or {}
    try:
        success, msg, report_info = submit_daily_report(content, cfg)
        if success:
            notify_report_success(
                cfg,
                content,
                report_info,
                report_source=report_source,
                smart_doc_status=report_meta.get("smart_doc_status"),
                smart_doc_error=report_meta.get("smart_doc_error"),
            )
            return True
        notify_report_failure(
            cfg,
            msg,
            report_source=report_source,
            smart_doc_status=report_meta.get("smart_doc_status"),
            smart_doc_error=report_meta.get("smart_doc_error"),
            screenshot=report_info.get("screenshot"),
        )
        return False
    except Exception as e:
        logger.error(f"Submit flow crashed: {e}", exc_info=True)
        notify_report_failure(
            cfg,
            f"日报提交流程异常: {e}",
            report_source=report_source,
            smart_doc_status=report_meta.get("smart_doc_status"),
            smart_doc_error=report_meta.get("smart_doc_error"),
        )
        return False


def _is_cookies_expiry_error(error: str) -> bool:
    """判断智能文档提取错误是否由 cookies 过期导致。"""
    if not error:
        return False
    lower = error.lower()
    return any(kw in lower for kw in ("login required", "cookies", "cookie", "登录"))


def _send_pending_confirm_message(cfg, error: str):
    """发送交互确认消息：智能文档过期，是否使用前一天日报。"""
    import src.beijing_time
    now_str = src.beijing_time.now().strftime("%Y-%m-%d %H:%M:%S")
    error_preview = str(error)[:200] + ("..." if len(str(error)) > 200 else "")

    msg = f"""## ⚠️ 定时日报提交 — 智能文档已过期

> **检测时间**：{now_str}
> **错误详情**：{error_preview}

系统已自动获取前一天日报内容作为备用。

**是否使用前一天日报提交？**
回复 **是** — 使用前一天日报内容提交
回复 **否** — 生成扫码登录二维码（需手动重新提交）

> ⏰ **5 分钟内未回复将自动使用前一天日报提交**"""
    _send_wechat_markdown(cfg.wechat, msg)


def _handle_pending_yes(cfg, from_user_id: str):
    """用户回复'是'：用前一天日报内容提交。"""
    data = load_pending()
    if not data:
        _send_wechat_text(cfg.wechat, "ℹ️ 当前没有待确认的日报提交", from_user_id)
        return

    clear_pending()
    _send_wechat_text(cfg.wechat, "⏳ 已确认，正在使用前一天日报提交...", from_user_id)

    try:
        success = _submit_and_notify(
            data["report_content"], cfg,
            data.get("report_source", "previous_report"),
            data.get("report_meta", {}),
        )
        logger.info("Pending confirmation: user chose YES, submitted previous report")
    except Exception as e:
        logger.error(f"Pending yes submit failed: {e}", exc_info=True)
        _send_wechat_text(cfg.wechat, f"❌ 提交失败\n{e}", from_user_id)


def _handle_pending_no(cfg, from_user_id: str):
    """用户回复'否'：生成扫码登录二维码。"""
    data = load_pending()
    if not data:
        _send_wechat_text(cfg.wechat, "ℹ️ 当前没有待确认的日报提交", from_user_id)
        return

    clear_pending()
    _send_wechat_text(cfg.wechat, "⏳ 已确认，正在生成扫码登录二维码...", from_user_id)

    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")

    def _on_pending_no_complete(success: bool, message: str) -> None:
        if not success:
            _send_wechat_text(cfg.wechat, f"❌ Cookies 续期未完成\n{message}\n今日日报尚未提交。")
            return
        try:
            _refresh_runtime_source(cfg, config_path)
            _send_wechat_text(cfg.wechat, "✅ Cookies 运行时配置已同步，无需重启服务。", from_user_id)
        except Exception as exc:
            logger.error(f"Pending-no runtime refresh failed: {exc}", exc_info=True)
            _send_wechat_text(
                cfg.wechat,
                f"⚠️ Cookies 已写入配置，但运行时同步失败，需要重启服务。\n{exc}",
                from_user_id,
            )

    started = start_renew_cookies_by_qr(
        config_path,
        from_user_id,
        "定时日报提交时发现Cookies过期，用户选择不沿用前一天日报",
        on_complete=_on_pending_no_complete,
    )
    if not started:
        _send_wechat_text(cfg.wechat, "ℹ️ 已有二维码登录任务在进行中，请先完成当前扫码", from_user_id)
    logger.info("Pending confirmation: user chose NO, started QR renew")


def _match_proxy_cmd(content: str, *names: str) -> bool:
    """匹配代理服务管理指令：启动Xray/停止Xray/重启Xray 等。"""
    lowered = content.lower()
    for name in names:
        if name in lowered and ("启动" in content or "停止" in content or "重启" in content):
            return True
    return False


def _handle_proxy_cmd(cfg, msg: dict, svc: str, label: str):
    """执行代理服务管理指令。"""
    from_user_id = msg.get("FromUserName", "")
    content = msg.get("Content", "")

    if "启动" in content:
        action, action_label = "start", "启动"
    elif "停止" in content:
        action, action_label = "stop", "停止"
    elif "重启" in content:
        action, action_label = "restart", "重启"
    else:
        return

    try:
        subprocess.run(
            ["sudo", "systemctl", action, svc],
            capture_output=True, text=True, timeout=15, check=True,
        )
        _send_wechat_text(cfg.wechat, f"✅ {label} 已{action_label}", from_user_id)
        logger.info(f"Proxy cmd: {action} {svc}")
    except subprocess.CalledProcessError as e:
        _send_wechat_text(cfg.wechat, f"❌ {label} {action_label}失败\n{e.stderr.strip()[-200:] or str(e)}", from_user_id)
    except Exception as e:
        _send_wechat_text(cfg.wechat, f"❌ {label} 操作异常\n{e}", from_user_id)


# ============================================================
# 原 server 文件尾部
