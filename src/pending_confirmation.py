"""定时日报提交发现智能文档过期时的交互确认状态管理。

当定时提交提取智能文档失败(cookies过期)时，不立即回退到前一天日报，
而是先发企微消息询问用户"是否使用前一天日报"。
用户回复"是"/"否"或超时后，执行相应操作。
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

PENDING_FILE = "/tmp/daily_report_pending.json"
PENDING_TIMEOUT = 300  # 5 分钟超时


def save_pending(report_content: str, source: str, meta: dict) -> bool:
    """保存待确认状态。返回 True 成功。"""
    now = time.time()
    data = {
        "report_content": report_content,
        "report_source": source,
        "report_meta": meta,
        "created_at": now,
        "timeout_at": now + PENDING_TIMEOUT,
    }
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        logger.info(f"Pending confirmation saved, timeout at {data['timeout_at']}")
        return True
    except Exception as e:
        logger.error(f"Failed to save pending confirmation: {e}")
        return False


def load_pending() -> dict | None:
    """读取待确认状态。超时自动清理返回 None。"""
    if not os.path.exists(PENDING_FILE):
        return None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load pending confirmation: {e}")
        return None

    if time.time() >= data.get("timeout_at", 0):
        logger.info("Pending confirmation expired")
        clear_pending()
        return None
    return data


def clear_pending():
    """清除待确认状态。"""
    try:
        if os.path.exists(PENDING_FILE):
            os.remove(PENDING_FILE)
            logger.info("Pending confirmation cleared")
    except Exception as e:
        logger.error(f"Failed to clear pending confirmation: {e}")


def is_pending_confirmation_reply(content: str) -> bool:
    """判断用户回复是否是/否（且有待确认状态）。"""
    if not os.path.exists(PENDING_FILE):
        return False
    text = content.strip().lower()
    return text in _YES_WORDS or text in _NO_WORDS


def is_yes(content: str) -> bool:
    return content.strip().lower() in _YES_WORDS


def is_no(content: str) -> bool:
    return content.strip().lower() in _NO_WORDS


_YES_WORDS = {"是", "1", "yes", "y", "确认", "同意", "好", "ok", "行", "可以"}
_NO_WORDS = {"否", "2", "no", "n", "拒绝", "不", "取消"}


def handle_pending_timeout_if_needed(cfg):
    """检查待确认是否超时，超时则自动用前一天日报提交并通知。
    供 scheduler 主循环调用。返回 True 表示处理了超时。
    """
    if not os.path.exists(PENDING_FILE):
        return False

    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False

    if time.time() < data.get("timeout_at", 0):
        return False  # 未超时

    # 超时，执行自动提交
    clear_pending()
    logger.info("Pending confirmation timed out, auto-submitting previous report")

    from src.config import load_config
    from src.target import submit_daily_report
    from src.notifier import notify_report_success, notify_report_failure

    report_content = data.get("report_content", "")
    report_source = data.get("report_source", "previous_report")
    report_meta = data.get("report_meta", {})

    try:
        success, msg, report_info = submit_daily_report(report_content, cfg)
        if success:
            notify_report_success(
                cfg, report_content, report_info,
                report_source=report_source,
                smart_doc_status=report_meta.get("smart_doc_status"),
                smart_doc_error=report_meta.get("smart_doc_error"),
            )
        else:
            notify_report_failure(
                cfg, msg,
                report_source=report_source,
                smart_doc_status=report_meta.get("smart_doc_status"),
                smart_doc_error=report_meta.get("smart_doc_error"),
                screenshot=report_info.get("screenshot"),
            )
    except Exception as e:
        logger.error(f"Pending timeout submit failed: {e}", exc_info=True)
        notify_report_failure(cfg, f"超时自动提交失败: {e}")

    # 超时通知
    if hasattr(cfg, "wechat") and cfg.wechat:
        from src.wechat_notifier import send_text as _send_wx_text
        now_str = __import__("src.beijing_time", fromlist=["now"]).\
            now().strftime("%H:%M:%S")
        _send_wx_text(cfg.wechat,
            f"⏰ 超时未回复（{now_str}）\n已自动使用前一天日报内容提交日报。")

    return True
