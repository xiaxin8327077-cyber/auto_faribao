import time
import json
import hashlib
import logging
import requests
from datetime import date

from src.beijing_time import now as beijing_now, today_str
from src.config import WechatConfig

logger = logging.getLogger(__name__)
WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

ACCESS_TOKEN_CACHE = {"token": "", "expires_at": 0}


def get_access_token(cfg: WechatConfig) -> str:
    now = int(time.time())
    if ACCESS_TOKEN_CACHE["token"] and now < ACCESS_TOKEN_CACHE["expires_at"] - 60:
        return ACCESS_TOKEN_CACHE["token"]

    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={cfg.corpid}&corpsecret={cfg.corpsecret}"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"Failed to get access token: {data}")
            return ""
        ACCESS_TOKEN_CACHE["token"] = data["access_token"]
        ACCESS_TOKEN_CACHE["expires_at"] = now + data["expires_in"]
        return data["access_token"]
    except Exception as e:
        logger.error(f"Get access token error: {e}")
        return ""


def send_markdown(cfg: WechatConfig, content: str, to_user: str = None) -> bool:
    token = get_access_token(cfg)
    if not token:
        return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": to_user or cfg.to_user,
        "msgtype": "markdown",
        "agentid": cfg.agentid,
        "markdown": {"content": content},
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"Send message failed: {data}")
            return False
        return True
    except Exception as e:
        logger.error(f"Send message error: {e}")
        return False


def send_text(cfg: WechatConfig, content: str, to_user: str = None) -> bool:
    token = get_access_token(cfg)
    if not token:
        return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": to_user or cfg.to_user,
        "msgtype": "text",
        "agentid": cfg.agentid,
        "text": {"content": content},
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"Send message failed: {data}")
            return False
        return True
    except Exception as e:
        logger.error(f"Send message error: {e}")
        return False

def upload_image(cfg: WechatConfig, image_path: str) -> str:
    token = get_access_token(cfg)
    if not token:
        return ""

    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=image"
    try:
        with open(image_path, "rb") as f:
            resp = requests.post(url, files={"media": f}, timeout=20)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"Upload image failed: {data}")
            return ""
        return data.get("media_id", "")
    except Exception as e:
        logger.error(f"Upload image error: {e}")
        return ""


def send_image(cfg: WechatConfig, image_path: str, to_user: str = None) -> bool:
    media_id = upload_image(cfg, image_path)
    if not media_id:
        return False

    token = get_access_token(cfg)
    if not token:
        return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": to_user or cfg.to_user,
        "msgtype": "image",
        "agentid": cfg.agentid,
        "image": {"media_id": media_id},
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error(f"Send image failed: {data}")
            return False
        return True
    except Exception as e:
        logger.error(f"Send image error: {e}")
        return False


def _is_configured(cfg) -> bool:
    if not hasattr(cfg, "wechat") or not cfg.wechat:
        return False
    w = cfg.wechat
    return bool(w.corpid and w.corpsecret and w.agentid and w.to_user)


def _report_source_label(source: str = None) -> str:
    labels = {
        "smart_sheet": "来自智能文档",
        "previous_report": "来自上一次日报",
        "manual": "手动填写",
        "generation_failed": "未生成（智能文档和上一次日报均失败）",
        "unknown": "未确定",
        None: "未确定",
        "": "未确定",
    }
    return labels.get(source, str(source))


def _smart_doc_status_text(status: str = None, error: str = None, source: str = None) -> str:
    if not status:
        if source == "smart_sheet":
            status = "normal"
        elif source == "manual":
            status = "not_used"
        else:
            status = "unknown"

    if status in ("normal", "ok", "success"):
        return "正常"
    if status in ("error", "failed", "exception"):
        detail = str(error or "").strip()
        return f"异常，异常原因：{detail}" if detail else "异常，异常原因：未捕获到具体错误"
    if status in ("not_used", "manual"):
        return "未读取（手动填写）"
    return "未确定"


def _resolve_report_date(report_info: dict, explicit_date: str, fallback: date) -> tuple[str, str]:
    raw = explicit_date or (report_info or {}).get("report_date") or (report_info or {}).get("date")
    try:
        report_day = date.fromisoformat(str(raw).replace("/", "-")) if raw else fallback
    except ValueError:
        report_day = fallback
    return report_day.isoformat(), WEEKDAYS_CN[report_day.weekday()]


def notify_report_success(cfg, content: str, report_info: dict = None, report_date: str = None,
                          report_source: str = None, smart_doc_status: str = None,
                          smart_doc_error: str = None):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")

    info = report_info or {}
    report_date, weekday_cn = _resolve_report_date(info, report_date, now.date())
    project = info.get("project", "-")
    hours = info.get("hours", "-")
    travel = info.get("travel", "否")
    log_type = info.get("log_type", "实施日志")
    source = report_source or info.get("report_source")
    source_label = _report_source_label(source)
    smart_status_text = _smart_doc_status_text(
        smart_doc_status or info.get("smart_doc_status"),
        smart_doc_error or info.get("smart_doc_error"),
        source,
    )

    md_content = f"""## ✅ 日报提交成功

> **发送时间**：{send_time}
> **日报日期**：{report_date} {weekday_cn}
> **日报类型**：{source_label}
> **智能文档状态**：{smart_status_text}
> **项目名称**：{project}
> **工作时长**：{hours} 小时
> **是否出差**：{travel}
> **日志类型**：{log_type}
> **提交系统**：OA企业信息管理平台

### 日报详情
```text
{content}
```
"""
    send_markdown(cfg.wechat, md_content)


def notify_report_failure(cfg, error: str, report_date: str = None, report_source: str = None,
                          smart_doc_status: str = None, smart_doc_error: str = None,
                          screenshot: str = None):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date, weekday_cn = _resolve_report_date({}, report_date, now.date())
    source_label = _report_source_label(report_source)
    smart_status_text = _smart_doc_status_text(smart_doc_status, smart_doc_error, report_source)

    md_content = f"""## ❌ 日报提交失败

> **发送时间**：{send_time}
> **日报日期**：{report_date} {weekday_cn}
> **日报类型**：{source_label}
> **智能文档状态**：{smart_status_text}
> **提交系统**：OA企业信息管理平台

### 错误信息
```text
{error}
```

请手动登录 OA 系统提交日报。
"""
    send_markdown(cfg.wechat, md_content)
    if screenshot:
        import os
        if os.path.exists(screenshot):
            try:
                send_image(cfg.wechat, screenshot)
            except Exception as e:
                logger.warning(f"Failed to send screenshot to wechat: {e}")


def notify_cookies_network_error(cfg, error: str):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    md_content = f"""## ⚠️ 智能文档访问异常

> **检测状态**：网络或页面加载异常
> **检测时间**：{send_time}
> **Cookie 判断**：未判定失效
> **自动续期**：未启动

### 错误详情
```text
{error}
```

系统已自动重试一次。请稍后再次检查；只有明确登录失效时才会生成扫码二维码。
"""
    send_markdown(cfg.wechat, md_content)


def notify_cookies_expired(cfg, error: str):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date = now.strftime("%Y-%m-%d")

    md_content = f"""## ⚠️ 智能文档异常提醒

> **检测状态**：异常
> **检测时间**：{send_time}
> **检测日期**：{report_date}

### 错误详情
```text
{error}
```

### 更新 Cookies 方式
在群里 @日报机器人 并发送 Cookies 内容，格式如下：

```text
TOK=xxx;
traceid=xxx;
hashkey=xxx;
tdoc_uid=xxx;
wedoc_openid=xxx;
wedoc_sid=xxx;
wedoc_sids=xxx;
wedoc_skey=xxx;
wedoc_ticket=xxx;
fingerprint=xxx;
```

系统将自动解析并更新配置。
"""
    send_markdown(cfg.wechat, md_content)


def notify_cookies_valid(cfg, updated_fields: list):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date = now.strftime("%Y-%m-%d")
    weekday_cn = WEEKDAYS_CN[now.weekday()]
    fields_str = "、".join(updated_fields)

    md_content = f"""## ✅ Cookies 更新成功

> **验证状态**：有效
> **验证时间**：{send_time}
> **验证日期**：{report_date} {weekday_cn}
> **更新字段**：{fields_str}

新 Cookies 已通过验证并生效，系统将继续正常运行。
"""
    send_markdown(cfg.wechat, md_content)


def notify_cookies_invalid(cfg, error: str):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date = now.strftime("%Y-%m-%d")
    weekday_cn = WEEKDAYS_CN[now.weekday()]

    md_content = f"""## ❌ Cookies 验证失败

> **验证状态**：无效
> **验证时间**：{send_time}
> **验证日期**：{report_date} {weekday_cn}

### 无效原因
```text
{error}
```

配置文件未被修改，请重新获取 Cookies 后再次发送。
"""
    send_markdown(cfg.wechat, md_content)
