import smtplib
import logging
from html import escape
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from src.beijing_time import now as beijing_now

logger = logging.getLogger(__name__)
WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def send_email(smtp_host: str, smtp_port: int, sender: str, password: str,
               recipient: str, subject: str, body: str) -> bool:
    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
        server.quit()
        logger.info(f"Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


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
    safe_content = escape(content or "")

    subject = f"[日报提交成功] {report_date} {weekday_cn}"
    body = f"""
    <div style="font-family:Microsoft YaHei,sans-serif;max-width:600px;margin:0 auto;">
        <div style="background:#52c41a;color:white;padding:15px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">日报提交成功</h2>
        </div>
        <div style="border:1px solid #d9d9d9;border-top:none;padding:20px;border-radius:0 0 8px 8px;">
            <table style="width:100%;border-collapse:collapse;">
                <tr>
                    <td style="padding:8px 0;color:#666;width:100px;">发送状态</td>
                    <td style="padding:8px 0;"><span style="color:#52c41a;font-weight:bold;">成功</span></td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">发送时间</td>
                    <td style="padding:8px 0;">{send_time}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">日报日期</td>
                    <td style="padding:8px 0;">{report_date} {weekday_cn}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">日报类型</td>
                    <td style="padding:8px 0;">{source_label}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">智能文档读取状态</td>
                    <td style="padding:8px 0;">{smart_status_text}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">项目名称</td>
                    <td style="padding:8px 0;">{project}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">工作时长</td>
                    <td style="padding:8px 0;">{hours} 小时</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">是否出差</td>
                    <td style="padding:8px 0;">{travel}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">日志类型</td>
                    <td style="padding:8px 0;">{log_type}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">提交系统</td>
                    <td style="padding:8px 0;">OA企业信息管理平台</td>
                </tr>
            </table>
            <hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
            <p style="color:#666;margin-bottom:8px;">日报详情：</p>
            <div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:4px;padding:12px;">
                <pre style="margin:0;font-family:Microsoft YaHei,sans-serif;white-space:pre-wrap;">{safe_content}</pre>
            </div>
        </div>
    </div>
    """
    _send(cfg, subject, body)


def notify_report_failure(cfg, error: str, report_date: str = None, report_source: str = None,
                          smart_doc_status: str = None, smart_doc_error: str = None):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date, weekday_cn = _resolve_report_date({}, report_date, now.date())
    source_label = _report_source_label(report_source)
    smart_status_text = _smart_doc_status_text(smart_doc_status, smart_doc_error, report_source)
    safe_error = escape(error or "")

    subject = f"[日报提交失败] {report_date} {weekday_cn}"
    body = f"""
    <div style="font-family:Microsoft YaHei,sans-serif;max-width:600px;margin:0 auto;">
        <div style="background:#ff4d4f;color:white;padding:15px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">日报提交失败</h2>
        </div>
        <div style="border:1px solid #d9d9d9;border-top:none;padding:20px;border-radius:0 0 8px 8px;">
            <table style="width:100%;border-collapse:collapse;">
                <tr>
                    <td style="padding:8px 0;color:#666;width:100px;">发送状态</td>
                    <td style="padding:8px 0;"><span style="color:#ff4d4f;font-weight:bold;">失败</span></td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">发送时间</td>
                    <td style="padding:8px 0;">{send_time}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">日报日期</td>
                    <td style="padding:8px 0;">{report_date} {weekday_cn}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">日报类型</td>
                    <td style="padding:8px 0;">{source_label}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">智能文档读取状态</td>
                    <td style="padding:8px 0;">{smart_status_text}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">提交系统</td>
                    <td style="padding:8px 0;">OA企业信息管理平台</td>
                </tr>
            </table>
            <hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
            <p style="color:#666;margin-bottom:8px;">错误信息：</p>
            <div style="background:#fff2f0;border:1px solid #ffccc7;border-radius:4px;padding:12px;">
                <pre style="margin:0;color:#ff4d4f;font-family:Microsoft YaHei,sans-serif;white-space:pre-wrap;">{safe_error}</pre>
            </div>
            <p style="color:#999;margin-top:15px;">请手动登录 OA 系统提交日报。</p>
        </div>
    </div>
    """
    _send(cfg, subject, body)


def notify_cookies_expired(cfg, error: str):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date = now.strftime("%Y-%m-%d")
    safe_error = escape(error or "")

    subject = f"[智能文档异常提醒] Cookies过期或读取失败 {report_date}"
    body = f"""
    <div style="font-family:Microsoft YaHei,sans-serif;max-width:600px;margin:0 auto;">
        <div style="background:#fa8c16;color:white;padding:15px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">智能文档异常提醒</h2>
        </div>
        <div style="border:1px solid #d9d9d9;border-top:none;padding:20px;border-radius:0 0 8px 8px;">
            <table style="width:100%;border-collapse:collapse;">
                <tr>
                    <td style="padding:8px 0;color:#666;width:100px;">检测状态</td>
                    <td style="padding:8px 0;"><span style="color:#fa8c16;font-weight:bold;">异常</span></td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">检测时间</td>
                    <td style="padding:8px 0;">{send_time}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">检测日期</td>
                    <td style="padding:8px 0;">{report_date}</td>
                </tr>
            </table>
            <hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
            <p style="color:#666;margin-bottom:8px;">错误详情：</p>
            <div style="background:#fff7e6;border:1px solid #ffd591;border-radius:4px;padding:12px;">
                <pre style="margin:0;color:#fa8c16;font-family:Microsoft YaHei,sans-serif;white-space:pre-wrap;">{safe_error}</pre>
            </div>
            <hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
            <p style="color:#666;font-weight:bold;">如果错误详情提示 Cookies 过期，请按以下步骤更新 Cookies：</p>
            <ol style="color:#666;">
                <li>在浏览器中打开智能表格文档</li>
                <li>按 F12 打开开发者工具 → Application → Cookies → doc.weixin.qq.com</li>
                <li>将最新的 Cookies 以邮件回复本邮件，主题为"新cookies"</li>
                <li>系统将自动读取并更新配置</li>
            </ol>
        </div>
    </div>
    """
    _send(cfg, subject, body)


def notify_cookies_valid(cfg, updated_fields: list):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date = now.strftime("%Y-%m-%d")
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    fields_str = "、".join(updated_fields)

    subject = f"[Cookies更新成功] 新Cookies验证通过 {report_date}"
    body = f"""
    <div style="font-family:Microsoft YaHei,sans-serif;max-width:600px;margin:0 auto;">
        <div style="background:#52c41a;color:white;padding:15px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">Cookies 更新成功</h2>
        </div>
        <div style="border:1px solid #d9d9d9;border-top:none;padding:20px;border-radius:0 0 8px 8px;">
            <table style="width:100%;border-collapse:collapse;">
                <tr>
                    <td style="padding:8px 0;color:#666;width:100px;">验证状态</td>
                    <td style="padding:8px 0;"><span style="color:#52c41a;font-weight:bold;">有效</span></td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">验证时间</td>
                    <td style="padding:8px 0;">{send_time}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">验证日期</td>
                    <td style="padding:8px 0;">{report_date} {weekday_cn}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">更新字段</td>
                    <td style="padding:8px 0;">{fields_str}</td>
                </tr>
            </table>
            <hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
            <p style="color:#52c41a;">新 Cookies 已通过验证并生效，系统将继续正常运行。</p>
        </div>
    </div>
    """
    _send(cfg, subject, body)


def notify_cookies_invalid(cfg, error: str):
    if not _is_configured(cfg):
        return
    now = beijing_now()
    send_time = now.strftime("%Y-%m-%d %H:%M:%S")
    report_date = now.strftime("%Y-%m-%d")
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]

    subject = f"[Cookies验证失败] 新Cookies无效 {report_date}"
    body = f"""
    <div style="font-family:Microsoft YaHei,sans-serif;max-width:600px;margin:0 auto;">
        <div style="background:#ff4d4f;color:white;padding:15px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">Cookies 验证失败</h2>
        </div>
        <div style="border:1px solid #d9d9d9;border-top:none;padding:20px;border-radius:0 0 8px 8px;">
            <table style="width:100%;border-collapse:collapse;">
                <tr>
                    <td style="padding:8px 0;color:#666;width:100px;">验证状态</td>
                    <td style="padding:8px 0;"><span style="color:#ff4d4f;font-weight:bold;">无效</span></td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">验证时间</td>
                    <td style="padding:8px 0;">{send_time}</td>
                </tr>
                <tr>
                    <td style="padding:8px 0;color:#666;">验证日期</td>
                    <td style="padding:8px 0;">{report_date} {weekday_cn}</td>
                </tr>
            </table>
            <hr style="border:none;border-top:1px solid #eee;margin:15px 0;">
            <p style="color:#666;margin-bottom:8px;">无效原因：</p>
            <div style="background:#fff2f0;border:1px solid #ffccc7;border-radius:4px;padding:12px;">
                <pre style="margin:0;color:#ff4d4f;font-family:Microsoft YaHei,sans-serif;white-space:pre-wrap;">{error}</pre>
            </div>
            <p style="color:#999;margin-top:15px;">配置文件未被修改。请重新获取 Cookies 后再次发送邮件。</p>
        </div>
    </div>
    """
    _send(cfg, subject, body)


def _is_configured(cfg) -> bool:
    return bool(cfg.email and cfg.email.sender and cfg.email.smtp_host)


def _resolve_report_date(report_info: dict, explicit_date: str, fallback: date) -> tuple[str, str]:
    raw = explicit_date or (report_info or {}).get("report_date") or (report_info or {}).get("date")
    try:
        report_day = date.fromisoformat(str(raw).replace("/", "-")) if raw else fallback
    except ValueError:
        report_day = fallback
    return report_day.isoformat(), WEEKDAYS_CN[report_day.weekday()]


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
        detail = escape(str(error or "").strip())
        return f"异常，异常原因：{detail}" if detail else "异常，异常原因：未捕获到具体错误"
    if status in ("not_used", "manual"):
        return "未读取（手动填写）"
    return "未确定"


def _send(cfg, subject: str, body: str):
    e = cfg.email
    send_email(e.smtp_host, e.smtp_port, e.sender, e.password,
               e.recipient, subject, body)
