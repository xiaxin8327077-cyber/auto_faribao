import logging
import threading
import time
from datetime import date, timedelta
from src.beijing_time import now as beijing_now, today
from src.pending_confirmation import handle_pending_timeout_if_needed

logger = logging.getLogger(__name__)

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
        f"cache cleanup at {cch:02d}:{ccm:02d}, "
        f"nav monitor at {cfg.nav_monitor.push_hour:02d}:{cfg.nav_monitor.push_minute:02d} on weekdays, "
        f"nav evening check at {cfg.nav_monitor.evening_push_hour:02d}:{cfg.nav_monitor.evening_push_minute:02d} on weekdays, "
        f"nav estimate at {cfg.nav_monitor.estimate_hour:02d}:{cfg.nav_monitor.estimate_minute:02d} on weekdays"
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
        f"cache cleanup {cch:02d}:{ccm:02d}, "
        f"nav monitor {cfg.nav_monitor.push_hour:02d}:{cfg.nav_monitor.push_minute:02d} on weekdays, "
        f"nav evening check {cfg.nav_monitor.evening_push_hour:02d}:{cfg.nav_monitor.evening_push_minute:02d} on weekdays, "
        f"nav estimate {cfg.nav_monitor.estimate_hour:02d}:{cfg.nav_monitor.estimate_minute:02d} on weekdays"
    )


def _run(cfg):
    last_cookies_date = None
    last_submit_date = None
    last_stats_date = None
    last_cache_cleanup_date = None
    last_nav_push_date = None
    last_nav_evening_push_date = None
    last_nav_estimate_date = None
    last_calendar_update_year = None

    while True:
        with _scheduler_lock:
            current_cfg = _runtime_cfg or cfg

        # 检查待确认超时（定时提交发现 cookies 过期时的交互确认）
        handle_pending_timeout_if_needed(current_cfg)

        now = beijing_now()
        today_str = now.strftime("%Y-%m-%d")

        ch, cm, rh, rm, sh, sm, cch, ccm = _get_times(current_cfg)

        if _should_run_nav_monitor(current_cfg, now, last_nav_push_date):
            last_nav_push_date = today_str
            logger.info("Scheduler triggered: nav monitor push")
            _run_nav_monitor_push(current_cfg, now.date())

        if _should_run_nav_estimate(current_cfg, now, last_nav_estimate_date):
            last_nav_estimate_date = today_str
            logger.info("Scheduler triggered: nav estimate push")
            _run_nav_estimate_push(current_cfg, now.date())

        if _should_run_nav_evening_push(current_cfg, now, last_nav_evening_push_date):
            last_nav_evening_push_date = today_str
            logger.info("Scheduler triggered: nav evening push")
            _run_nav_evening_push(current_cfg, now.date())

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

        # 每年 12 月 1 日自动更新下一年工作日历
        if (now.month == 12 and now.day == 1 and
                last_calendar_update_year != now.year):
            last_calendar_update_year = now.year
            _run_calendar_update(current_cfg)

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


def _should_run_nav_monitor(cfg, now, last_nav_push_date):
    nav = getattr(cfg, "nav_monitor", None)
    if not nav or not getattr(nav, "enabled", False):
        return False
    today_str = now.strftime("%Y-%m-%d")
    return (
        _is_workday(now.date())
        and now.hour == nav.push_hour
        and now.minute == nav.push_minute
        and last_nav_push_date != today_str
    )


def _should_run_nav_estimate(cfg, now, last_nav_estimate_date):
    nav = getattr(cfg, "nav_monitor", None)
    if not nav or not getattr(nav, "estimate_enabled", True):
        return False
    today_str = now.strftime("%Y-%m-%d")
    return (
        _is_workday(now.date())
        and now.hour == nav.estimate_hour
        and now.minute == nav.estimate_minute
        and last_nav_estimate_date != today_str
    )


def _should_run_nav_evening_push(cfg, now, last_nav_evening_push_date):
    nav = getattr(cfg, "nav_monitor", None)
    if not nav or not getattr(nav, "enabled", False) or not getattr(nav, "evening_push_enabled", True):
        return False
    today_str = now.strftime("%Y-%m-%d")
    return (
        _is_workday(now.date())
        and now.hour == nav.evening_push_hour
        and now.minute == nav.evening_push_minute
        and last_nav_evening_push_date != today_str
    )


def _nav_period_push_jobs(day: date) -> list[tuple[str, date]]:
    if not _is_workday(day):
        return []

    jobs = []
    one_day = timedelta(days=1)

    week_start = day - timedelta(days=day.weekday())
    if _is_first_workday_since(week_start, day):
        jobs.append(("week", week_start - one_day))

    month_start = date(day.year, day.month, 1)
    if _is_first_workday_since(month_start, day):
        previous_period_end = month_start - one_day
        jobs.append(("month", previous_period_end))

        quarter_start = _quarter_start(day)
        if month_start == quarter_start:
            jobs.append(("quarter", previous_period_end))

        half_year_start = _half_year_start(day)
        if month_start == half_year_start:
            jobs.append(("half_year", previous_period_end))

        if day.month == 1:
            jobs.append(("year", previous_period_end))

    return jobs


def _is_first_workday_since(start_day: date, day: date) -> bool:
    if not _is_workday(day):
        return False
    cursor = start_day
    while cursor < day:
        if _is_workday(cursor):
            return False
        cursor += timedelta(days=1)
    return True


def _quarter_start(day: date) -> date:
    month = ((day.month - 1) // 3) * 3 + 1
    return date(day.year, month, 1)


def _half_year_start(day: date) -> date:
    month = 1 if day.month <= 6 else 7
    return date(day.year, month, 1)


def _run_cookies_check(cfg):
    from src.cookies_checker import check_cookies, CookiesError
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


def _run_nav_monitor_push(cfg, day: date = None):
    try:
        from src.nav_monitor import push_nav_period_report, push_nav_report
        push_nav_report(cfg)
        target_day = day or today()
        to_user = getattr(cfg.wechat, "to_user", None)
        for period, base_date in _nav_period_push_jobs(target_day):
            logger.info("Scheduler triggered: nav period push %s as of %s", period, base_date)
            push_nav_period_report(cfg, period, base_date=base_date, to_user=to_user)
    except Exception as e:
        logger.error(f"Scheduler nav monitor push failed: {e}", exc_info=True)
        if _wechat_is_configured(cfg):
            try:
                from src.wechat_notifier import send_text as _send_wechat_text
                _send_wechat_text(
                    cfg.wechat,
                    f"❌ 净值监控推送失败\n{e}",
                    getattr(cfg.wechat, "to_user", None),
                )
            except Exception:
                logger.error("NAV failure notification failed", exc_info=True)


def _run_nav_estimate_push(cfg, day: date = None):
    try:
        from src.nav_holdings import push_estimate_report, refresh_quarterly_profiles_if_due

        target_day = day or today()
        refresh_quarterly_profiles_if_due(cfg, today=target_day, notify=False)
        push_estimate_report(cfg, to_user=getattr(cfg.wechat, "to_user", None))
    except Exception as e:
        logger.error(f"Scheduler nav estimate push failed: {e}", exc_info=True)
        if _wechat_is_configured(cfg):
            try:
                from src.wechat_notifier import send_text as _send_wechat_text

                _send_wechat_text(
                    cfg.wechat,
                    f"❌ 理财收益预估失败\n{e}",
                    getattr(cfg.wechat, "to_user", None),
                )
            except Exception:
                logger.error("NAV estimate failure notification failed", exc_info=True)


def _run_nav_evening_push(cfg, day: date = None):
    try:
        from src.nav_monitor import push_nav_evening_report

        target_day = day or today()
        push_nav_evening_report(
            cfg,
            as_of_date=target_day,
            to_user=getattr(cfg.wechat, "to_user", None),
        )
    except Exception as e:
        logger.error(f"Scheduler nav evening push failed: {e}", exc_info=True)
        if _wechat_is_configured(cfg):
            try:
                from src.wechat_notifier import send_text as _send_wechat_text

                _send_wechat_text(
                    cfg.wechat,
                    f"❌ 净值晚报推送失败\n{e}",
                    getattr(cfg.wechat, "to_user", None),
                )
            except Exception:
                logger.error("NAV evening failure notification failed", exc_info=True)


def _wechat_is_configured(cfg) -> bool:
    wechat = getattr(cfg, "wechat", None)
    return bool(
        wechat
        and getattr(wechat, "corpid", "")
        and getattr(wechat, "corpsecret", "")
        and getattr(wechat, "agentid", 0)
        and getattr(wechat, "to_user", "")
    )


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

        # 8. Clean old failed captcha images (7 days ago)
        captcha_failed_dir = os.path.join(project_dir, "data", "captcha", "failed")
        captcha_cleaned = 0
        if os.path.isdir(captcha_failed_dir):
            for fname in os.listdir(captcha_failed_dir):
                fpath = os.path.join(captcha_failed_dir, fname)
                try:
                    if os.path.isfile(fpath) and time.time() - os.path.getmtime(fpath) > 7 * 86400:
                        os.remove(fpath)
                        captcha_cleaned += 1
                except Exception:
                    pass
        results.append(f"🖼️ 验证码失败截图：清理 {captcha_cleaned} 个（保留7天）")

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


def _run_calendar_update(cfg):
    """每年 12 月 1 日自动更新下一年工作日历。"""
    from src.calendar_updater import update_calendar
    from src.wechat_notifier import send_text as _send_wx_text

    next_year = __import__("datetime").date.today().year + 1
    logger.info(f"Auto-updating workday calendar for {next_year}...")
    ok, msg = update_calendar(next_year)
    if ok:
        _send_wx_text(cfg.wechat, f"📅 {msg}", getattr(cfg.wechat, "to_user", None))
        from src.workday_calendar import reset_calendar
        reset_calendar()
    else:
        _send_wx_text(cfg.wechat, f"⚠️ {msg}\n请手动更新 config/mainland_workdays.json", getattr(cfg.wechat, "to_user", None))
