import os
import subprocess
import threading
import logging
from flask import Flask, request, jsonify
from src.config import Config
from src.notifier import notify_report_success, notify_report_failure
from src.target import submit_daily_report, get_previous_report_content, get_report_status, delete_daily_report, get_recent_reports, get_monthly_statistics, get_weekly_statistics
from src.wechat_callback import WechatCallbackCrypto, parse_message, extract_text_content, is_cookies_update_message
from src.auto_cookies_updater import update_cookies_from_wechat
from src.extractor import extract_tasks, ExtractError
from src.wechat_notifier import send_text as _send_wechat_text, send_markdown as _send_wechat_markdown
from src.qr_login_renewer import start_renew_cookies_by_qr

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


def _current_cmd() -> str:
    with _cmd_lock:
        return _cmd_name


def _busy_reply() -> str:
    return f"""⏳ 系统繁忙，请稍等

当前正在执行：{_current_cmd()}

为避免资源冲突，同一时间只能执行一个操作。
请等当前任务完成后再试～"""


def create_app(cfg: Config) -> Flask:
    app = Flask(__name__)

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
                from_user = msg.get("FromUserName", "")

                if is_cookies_update_message(content):
                    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                    from_user_id = msg.get("FromUserName", "")
                    
                    if not _try_start_cmd("Cookies更新"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    def process_cookies_update():
                        try:
                            success, updated_fields, error_msg = update_cookies_from_wechat(config_path, content, cfg)
                            if success and not updated_fields:
                                _send_wechat_text(cfg.wechat, "ℹ️ Cookies 无需更新\n所有字段值与配置相同，无需更新", from_user_id)
                        except Exception as e:
                            logger.error(f"Process cookies error: {e}")
                            _send_wechat_text(cfg.wechat, f"❌ 处理异常\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    _send_wechat_text(cfg.wechat, "⏳ 正在检查 Cookies 是否可用，请稍等...", from_user_id)

                    thread = threading.Thread(target=process_cookies_update, daemon=True)
                    thread.start()

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

## 📄 内容查询
**读取日报** — 读取智能文档
**获取前一天日报** — 读取OA上一条

## 🗑️ 操作管理
**撤回今日日报** — 删除已提交日报

## ⚙️ 系统管理
**生成二维码** — 续期Cookies
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

> 💡 手动触发仅发企业微信通知。"""
                    _send_wechat_markdown(cfg.wechat, help_text, from_user_id)
                    return "", 200

                elif "生成二维码" in content or "重新登录" in content or "扫码登录" in content:
                    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
                    from_user_id = msg.get("FromUserName", "")
                    _send_wechat_text(cfg.wechat, "⏳ 已收到请求，正在准备企业微信扫码登录二维码...", from_user_id)
                    started = start_renew_cookies_by_qr(config_path, from_user_id, "收到手动扫码登录指令")
                    if not started:
                        _send_wechat_text(cfg.wechat, "ℹ️ 已有二维码登录续期任务正在进行中，请先完成当前扫码。", from_user_id)
                    return "", 200

                elif "检查Cookies" in content or "Cookies状态" in content or "cookies状态" in content or "检查cookies" in content:
                    from_user_id = msg.get("FromUserName", "")
                    if not _try_start_cmd("检查Cookies"):
                        _send_wechat_text(cfg.wechat, _busy_reply(), from_user_id)
                        return "", 200

                    _send_wechat_text(cfg.wechat, "⏳ 正在检查智能表格 Cookies 状态，请稍等...", from_user_id)

                    def process_check_cookies():
                        try:
                            from src.cookies_checker import check_cookies, CookiesError
                            try:
                                check_cookies(cfg)
                                reply = "✅ Cookies 状态正常\n智能表格读取功能可用"
                            except CookiesError as e:
                                reply = f"❌ Cookies 已过期\n{e}\n\n请发送「生成二维码」重新扫码登录"
                            except Exception as e:
                                reply = f"❌ 检查失败\n{e}"
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
                            auto_submit_if_needed(cfg, send_email=False)
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

📧 邮件通知：
  • 发件人：{cfg.email.sender if cfg.email else '未配置'}
  • 收件人：{cfg.email.recipient if cfg.email else '未配置'}

💬 企业微信：
  • 企业ID：{cfg.wechat.corpid if cfg.wechat else '未配置'}
  • 状态：{'已配置' if cfg.wechat and cfg.wechat.corpid else '未配置'}

📊 智能文档：
  • 文档ID：{cfg.source.doc_id}
  • 负责人：{', '.join(cfg.source.person_names)}"""
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
                        s = cfg.scheduler
                        reply = f"""⏰ 定时任务配置

1️⃣ Cookies 检查：{s.cookie_check_hour:02d}:{s.cookie_check_minute:02d}（工作日）
2️⃣ 日报自动提交：{s.report_submit_hour:02d}:{s.report_submit_minute:02d}（工作日）
3️⃣ 统计自动推送：{s.stats_push_hour:02d}:{s.stats_push_minute:02d}（周日/月末）
4️⃣ 缓存自动清理：{s.cache_cleanup_hour:02d}:{s.cache_cleanup_minute:02d}（每月1号）

📝 修改指令：
• 设置Cookies检查时间 09:45
• 设置日报提交时间 20:00
• 设置统计推送时间 21:00
• 设置缓存清理时间 04:00"""
                        _send_wechat_text(cfg.wechat, reply, from_user_id)
                    except Exception as e:
                        logger.error(f"View schedule config command failed: {e}", exc_info=True)
                        _send_wechat_text(cfg.wechat, f"❌ 获取配置失败\n{e}", from_user_id)
                    return "", 200

                elif ("设置Cookies检查时间" in content or "修改Cookies检查时间" in content):
                    from_user_id = msg.get("FromUserName", "")
                    import re
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
                    import re
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
                    import re
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

                elif "设置提交时间" in content or "修改提交时间" in content or "设置日报时间" in content:
                    from_user_id = msg.get("FromUserName", "")
                    import re
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
                            auto_submit_if_needed(cfg, send_email=False)
                        except Exception as e:
                            logger.error(f"Wechat send report command failed: {e}", exc_info=True)
                            _send_wechat_text(cfg.wechat, f"❌ 发送日报任务异常\n{e}", from_user_id)
                        finally:
                            _end_cmd()

                    thread = threading.Thread(target=process_send_report, daemon=True)
                    thread.start()
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
                                send_email=False,
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
                send_email=False,
            )
        else:
            notify_report_failure(
                cfg,
                msg,
                report_date=report_date,
                report_source="manual",
                smart_doc_status="not_used",
                screenshot=report_info.get("screenshot"),
                send_email=False,
            )
    except Exception as e:
        logger.error(f"Manual submit error: {e}", exc_info=True)
        notify_report_failure(
            cfg,
            f"网页手动提交异常: {e}",
            report_date=report_date,
            report_source="manual",
            smart_doc_status="not_used",
            send_email=False,
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


def auto_submit_if_needed(cfg: Config, send_email: bool = True):
    """Called by scheduler at 17:45. Auto-submits if no manual report was provided."""
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
            send_email=send_email,
        )
        return

    _submit_and_notify(report, cfg, source, meta, send_email=send_email)


def _submit_and_notify(content: str, cfg: Config, report_source: str = None,
                       report_meta: dict = None, send_email: bool = True) -> bool:
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
                send_email=send_email,
            )
            return True
        notify_report_failure(
            cfg,
            msg,
            report_source=report_source,
            smart_doc_status=report_meta.get("smart_doc_status"),
            smart_doc_error=report_meta.get("smart_doc_error"),
            screenshot=report_info.get("screenshot"),
            send_email=send_email,
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
            send_email=send_email,
        )
        return False
