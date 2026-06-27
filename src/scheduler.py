import logging
import threading
import time
from src.beijing_time import now as beijing_now, today

logger = logging.getLogger(__name__)

COOKIES_EMAIL_CHECK_INTERVAL = 300

_scheduler_lock = threading.Lock()
_runtime_cfg = None


def _get_times(cfg):
    s = cfg.scheduler
    return (
        s.cookie_check_hour, s.cookie_check_minute,
        s.report_submit_hour, s.report_submit_minute,
        s.stats_push_hour, s.stats_push_minute,
        s.cache_cleanup_hour, s.cache_cleanup_minute,
    )


def start(cfg):
    global _runtime_cfg
    _runtime_cfg = cfg
    thread = threading.Thread(
        target=_run, args=(cfg,), daemon=True, name="scheduler"
    )
    thread.start()
    ch, cm, rh, rm, sh, sm, cch, ccm = _get_times(cfg)
    logger.info(
        f"Scheduler started: cookies check at {ch:02d}:{cm:02d}, "
        f"report submit at {rh:02d}:{rm:02d} on weekdays, "
        f"stats push at {sh:02d}:{sm:02d} on Sun/month-end, "
        f"cache cleanup at {cch:02d}:{ccm:02d}"
    )


def update_runtime_config(cfg):
    global _runtime_cfg
    with _scheduler_lock:
        _runtime_cfg = cfg
    ch, cm, rh, rm, sh, sm, cch, ccm = _get_times(cfg)
    logger.info(
        f"Scheduler config updated: cookies check {ch:02d}:{cm:02d}, "
        f"report submit {rh:02d}:{rm:02d}, "
        f"stats push {sh:02d}:{sm:02d}, "
        f"cache cleanup {cch:02d}:{ccm:02d}"
    )


def _run(cfg):
    last_cookies_date = None
    last_submit_date = None
    last_stats_date = None
    last_cache_cleanup_date = None
    last_email_check = 0

    while True:
        now = beijing_now()
        today_str = now.strftime("%Y-%m-%d")
        now_ts = time.time()

        with _scheduler_lock:
            current_cfg = _runtime_cfg or cfg

        ch, cm, rh, rm, sh, sm, cch, ccm = _get_times(current_cfg)

        if _is_workday(now.date()):
            if (now.hour == ch and
                    now.minute == cm and
                    last_cookies_date != today_str):
                last_cookies_date = today_str
                logger.info("Scheduler triggered: cookies check")
                _run_cookies_check(current_cfg)

            if (now.hour == rh and
                    now.minute == rm and
                    last_submit_date != today_str):
                last_submit_date = today_str
                logger.info("Scheduler triggered: report auto-submit")
                _run_auto_submit(current_cfg)

        if (now.hour == sh and
                now.minute == sm and
                last_stats_date != today_str):
            if _is_sunday(now.date()) or _is_last_day_of_month(now.date()):
                last_stats_date = today_str
                logger.info("Scheduler triggered: stats push")
                _run_stats_push(current_cfg, now.date())

        if (now.day == 1 and
                now.hour == cch and
                now.minute == ccm and
                last_cache_cleanup_date != today_str):
            last_cache_cleanup_date = today_str
            logger.info("Scheduler triggered: monthly cache cleanup")
            _run_cache_cleanup(current_cfg)

        if now_ts - last_email_check >= COOKIES_EMAIL_CHECK_INTERVAL:
            last_email_check = now_ts
            _run_cookies_email_check(current_cfg)

        time.sleep(30)


def _is_workday(day):
    from src.workday_calendar import is_workday
    return is_workday(day)


def _is_sunday(day):
    return day.weekday() == 6


def _is_last_day_of_month(day):
    from calendar import monthrange
    _, last = monthrange(day.year, day.month)
    return day.day == last


def _run_cookies_check(cfg):
    from src.cookies_checker import check_cookies, CookiesError
    from src.extractor import extract_tasks, ExtractError
    from src.notifier import notify_cookies_expired

    try:
        check_cookies(cfg)
        logger.info("Cookies check passed")
    except CookiesError as e:
        logger.error(f"Cookies check failed: {e}")
        notify_cookies_expired(cfg, f"Cookies 过期或无效: {e}")
        _start_qr_renew(cfg, f"自动检测到 Cookies 过期或无效: {e}")
        return
    except Exception as e:
        logger.error(f"Cookies check error: {e}", exc_info=True)
        notify_cookies_expired(cfg, f"Cookies 检查异常: {e}")
        _start_qr_renew(cfg, f"自动检测到 Cookies 检查异常: {e}")
        return

    try:
        tasks = extract_tasks(cfg.source)
        if tasks:
            logger.info(f"Smart document read check passed: {len(tasks)} matching tasks")
        else:
            logger.info("Smart document read check passed, no matching tasks; no email sent")
    except ExtractError as e:
        logger.error(f"Smart document read failed: {e}")
        notify_cookies_expired(cfg, f"智能文档读取异常: {e}")
        _start_qr_renew(cfg, f"自动检测到智能文档读取异常: {e}")
    except Exception as e:
        logger.error(f"Smart document read error: {e}", exc_info=True)
        notify_cookies_expired(cfg, f"智能文档读取异常: {e}")
        _start_qr_renew(cfg, f"自动检测到智能文档读取异常: {e}")


def _start_qr_renew(cfg, reason: str):
    try:
        from src.qr_login_renewer import start_renew_cookies_by_qr
        import os
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
        start_renew_cookies_by_qr(config_path, getattr(cfg.wechat, "to_user", None), reason)
    except Exception as e:
        logger.error(f"Start QR renew failed: {e}", exc_info=True)


def _run_auto_submit(cfg):
    from src.server import auto_submit_if_needed
    from src.notifier import notify_report_failure
    try:
        auto_submit_if_needed(cfg)
    except Exception as e:
        logger.error(f"Scheduler auto-submit failed: {e}", exc_info=True)
        notify_report_failure(cfg, str(e), report_source="unknown", smart_doc_status="unknown")


def _run_stats_push(cfg, day):
    from src.target import get_weekly_statistics, get_monthly_statistics
    from src.wechat_notifier import send_markdown as _send_wechat_md

    try:
        to_user = getattr(cfg.wechat, "to_user", None)
        msgs = []

        if _is_sunday(day):
            try:
                stats = get_weekly_statistics(cfg, year=day.year, month=day.month, day=day.day)
                msg = f"""📊 **{stats['year']}年第{stats['week_num']}周 周报统计**

📅 统计周期：{stats['start_date']} ~ {stats['end_date']}
📅 本周工作日：{stats['work_days']} 天
✅ 已提交：**{stats['submitted_days']}** 天
🏆 已审核：{stats['approved_days']} 天
📝 待审核：{stats['pending_days']} 天
❌ 缺交：{stats['missing_days']} 天（仅工作日）

📋 已提交日期：
"""
                for d in stats["submitted_dates"]:
                    msg += f"  • {d}\n"
                msgs.append(msg)
            except Exception as e:
                logger.error(f"Weekly stats push failed: {e}", exc_info=True)
                msgs.append(f"⚠️ 本周统计生成失败：{e}")

        if _is_last_day_of_month(day):
            try:
                stats = get_monthly_statistics(cfg, year=day.year, month=day.month)
                msg = f"""📊 **{stats['year']}年{stats['month']}月 月报统计**

📅 当月天数：{stats['total_days']} 天
📅 工作日：{stats['work_days']} 天
✅ 已提交：**{stats['submitted_days']}** 天
🏆 已审核：{stats['approved_days']} 天
📝 待审核：{stats['pending_days']} 天
❌ 缺交：{stats['missing_days']} 天（仅工作日）

📋 已提交日期：
"""
                for d in stats["submitted_dates"]:
                    msg += f"  • {d}\n"
                msgs.append(msg)
            except Exception as e:
                logger.error(f"Monthly stats push failed: {e}", exc_info=True)
                msgs.append(f"⚠️ 本月统计生成失败：{e}")

        for msg in msgs:
            try:
                _send_wechat_md(cfg.wechat, msg, to_user)
            except Exception as e:
                logger.error(f"Stats push wechat send failed: {e}")
    except Exception as e:
        logger.error(f"Stats push failed: {e}", exc_info=True)


def _run_cache_cleanup(cfg):
    """Run cache cleanup task."""
    import subprocess
    import shutil
    import os
    from src.wechat_notifier import send_text as _send_wechat_text

    project_dir = "/home/ubuntu/daily_report"
    script = "/usr/local/bin/clear-server-cache"
    results = []

    try:
        # 1. Clean service-level temp files
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

        # 2. Clean __pycache__
        pycache_result = subprocess.run(
            ["find", project_dir, "-maxdepth", "2", "-name", "__pycache__", "-type", "d"],
            capture_output=True, text=True, timeout=10
        )
        pycache_count = 0
        for dpath in pycache_result.stdout.strip().splitlines():
            if dpath and os.path.isdir(dpath):
                shutil.rmtree(dpath, ignore_errors=True)
                pycache_count += 1

        # 3. Clean service logs
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

        # 4. Clean Linux page cache
        r = subprocess.run(
            ["sudo", script, "pagecache"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            results.append("♻️ 系统页面缓存：已清理")
        else:
            results.append("♻️ 系统页面缓存：清理失败")

        # 5. Clean APT package cache
        r = subprocess.run(
            ["sudo", script, "apt"],
            capture_output=True, text=True, timeout=30
        )
        results.append("📦 APT 包缓存：已清理")

        # 6. Clean system logs (7 days ago)
        r = subprocess.run(
            ["sudo", script, "journal"],
            capture_output=True, text=True, timeout=15
        )
        results.append("📋 系统日志：已清理（保留7天）")

        # 7. Clean /tmp temp files (7 days ago)
        r = subprocess.run(
            ["sudo", script, "tmp"],
            capture_output=True, text=True, timeout=10
        )
        results.append("🗂️ /tmp 临时文件：已清理（保留7天）")

        # Get memory info after cleanup
        mem_after = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5).stdout
        mem_lines = mem_after.strip().splitlines()
        if len(mem_lines) > 1:
            mem_parts = mem_lines[1].split()
            if len(mem_parts) >= 4:
                mem_avail = mem_parts[6] if len(mem_parts) > 6 else mem_parts[3]
                results.append(f"\n💾 可用内存：{mem_avail}MB")

        reply = "🧹 定时缓存清理完成\n\n" + "\n".join(results)
        _send_wechat_text(cfg.wechat, reply, getattr(cfg.wechat, "to_user", None))
        logger.info("Cache cleanup completed successfully")

    except Exception as e:
        logger.error(f"Cache cleanup failed: {e}", exc_info=True)
        try:
            _send_wechat_text(cfg.wechat, f"❌ 定时缓存清理失败\n{e}", getattr(cfg.wechat, "to_user", None))
        except Exception:
            pass


def _run_cookies_email_check(cfg):
    from src.auto_cookies_updater import try_update_cookies
    import os
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    try:
        updated = try_update_cookies(config_path)
        if updated:
            logger.info("Cookies auto-updated from email!")
    except Exception as e:
        logger.debug(f"Cookies email check: {e}")
