import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from src.browser_lock import browser_operation, launch_browser

from src.auto_cookies_updater import COOKIE_FIELDS, _get_updated_fields, _get_current_cookie_values, _snapshot_covers_fields, _revert_config, update_config_cookies
from src.config import load_config
from src.wechat_notifier import send_image, send_markdown, send_text

logger = logging.getLogger(__name__)
DOC_BASE = "https://doc.weixin.qq.com"
RENEW_LOCK = threading.Lock()
QR_TIMEOUT_SECONDS = 120
RenewCompleteCallback = Callable[[bool, str], None]


def renew_cookies_by_qr(config_path: str, to_user: str = None, reason: str = "Cookies 已过期") -> tuple[bool, str]:
    if not RENEW_LOCK.acquire(blocking=False):
        return False, "已有二维码登录续期任务正在进行中，请稍后再试"

    try:
        return _run_renew_subprocess(config_path, to_user, reason)
    finally:
        RENEW_LOCK.release()


def _run_renew_subprocess(config_path: str, to_user: str = None, reason: str = "Cookies 已过期") -> tuple[bool, str]:
    cmd = [
        sys.executable,
        "-m",
        "src.qr_login_renewer",
        "--worker",
        "--config",
        config_path,
        "--reason",
        reason,
    ]
    if to_user:
        cmd.extend(["--to-user", to_user])

    try:
        proc = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=210,
        )
        if proc.stdout:
            logger.info(f"QR renew worker stdout: {proc.stdout.strip()}")
        if proc.stderr:
            logger.error(f"QR renew worker stderr: {proc.stderr.strip()}")
        if proc.returncode == 0:
            return True, "扫码续期任务完成"
        return False, f"扫码续期子进程失败，退出码：{proc.returncode}"
    except subprocess.TimeoutExpired:
        return False, "扫码续期子进程超时"
    except Exception as e:
        logger.error(f"Run QR renew subprocess failed: {e}", exc_info=True)
        return False, str(e)


def _invoke_renew_complete_callback(
    on_complete: RenewCompleteCallback | None,
    success: bool,
    message: str,
) -> None:
    if on_complete is None:
        return

    try:
        on_complete(success, message)
    except Exception:
        logger.error("QR renew completion callback failed", exc_info=True)


def _run_renew_in_background(
    config_path: str,
    to_user: str | None,
    reason: str,
    on_complete: RenewCompleteCallback | None = None,
) -> None:
    success = False
    message = "二维码续期任务未完成"
    try:
        success, message = renew_cookies_by_qr(config_path, to_user, reason)
    except Exception as exc:
        message = str(exc)
        logger.error("QR renew background task crashed: %s", exc, exc_info=True)
    finally:
        _invoke_renew_complete_callback(on_complete, success, message)


def start_renew_cookies_by_qr(
    config_path: str,
    to_user: str = None,
    reason: str = "Cookies 已过期",
    on_complete: RenewCompleteCallback | None = None,
) -> bool:
    if RENEW_LOCK.locked():
        try:
            cfg = load_config(config_path)
            send_text(cfg.wechat, "ℹ️ 已有二维码登录续期任务正在进行中，请先完成当前扫码。", to_user)
        except Exception:
            pass
        return False

    try:
        thread = threading.Thread(
            target=_run_renew_in_background,
            args=(config_path, to_user, reason, on_complete),
            daemon=True,
            name="qr-cookie-renew",
        )
        thread.start()
    except Exception as exc:
        logger.error("Failed to start QR renew background task: %s", exc, exc_info=True)
        _invoke_renew_complete_callback(on_complete, False, str(exc))
        return False
    return True


def _renew_cookies_by_qr_locked(config_path: str, cfg, to_user: str, reason: str) -> tuple[bool, str]:
    doc_url = _build_doc_url(cfg.source)
    qr_dir = Path(config_path).parent / "runtime"
    qr_dir.mkdir(parents=True, exist_ok=True)
    qr_path = qr_dir / f"wechat_login_qr_{int(time.time())}.png"

    send_text(cfg.wechat, f"⏳ {reason}\n正在生成企业微信扫码登录二维码，请稍等...", to_user)

    with browser_operation(), sync_playwright() as p:
        browser = launch_browser(p)
        context = browser.new_context(viewport={"width": 1280, "height": 1024}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            page.goto(doc_url, timeout=45000, wait_until="commit")
            page.wait_for_timeout(8000)
            if not _capture_enterprise_qr(page, str(qr_path)):
                message = "未找到企业微信登录二维码，请稍后重试或手动发送 Cookies。"
                send_text(cfg.wechat, f"❌ 二维码生成失败\n{message}", to_user)
                return False, message

            send_text(cfg.wechat, f"请使用企业微信扫码登录智能文档。\n二维码有效期约 {QR_TIMEOUT_SECONDS} 秒，扫码后系统会自动提取并验证 Cookies。", to_user)
            if not send_image(cfg.wechat, str(qr_path), to_user):
                message = "二维码图片发送失败，请检查企业微信应用素材上传权限。"
                send_text(cfg.wechat, f"❌ {message}", to_user)
                return False, message

            if not _wait_login_success(page, QR_TIMEOUT_SECONDS):
                message = "等待扫码登录超时，未更新 Cookies。"
                send_text(cfg.wechat, f"⌛ {message}\n如需重试，请发送：生成二维码", to_user)
                return False, message

            cookies = _extract_cookie_values(context.cookies())
            if not cookies:
                message = "扫码已完成，但未提取到有效 Cookies。"
                send_text(cfg.wechat, f"❌ {message}\n请手动发送 Cookies。", to_user)
                return False, message

            updated_fields = _get_updated_fields(config_path, cookies)
            if updated_fields is None:
                message = "无法读取当前Cookie配置，已中止更新，磁盘配置未修改。"
                send_text(cfg.wechat, f"❌ {message}", to_user)
                return False, message

            if not updated_fields:
                send_text(cfg.wechat, "ℹ️ 扫码成功，但 Cookies 与当前配置一致，无需更新。", to_user)
                return True, "Cookies 无需更新"

            old_cookies = _get_current_cookie_values(config_path, updated_fields)
            if not _snapshot_covers_fields(old_cookies, updated_fields):
                message = "无法取得完整的当前Cookie快照，已中止更新，磁盘配置未修改。"
                send_text(cfg.wechat, f"❌ {message}", to_user)
                return False, message

            if not update_config_cookies(config_path, cookies):
                message = "写入配置文件失败。"
                send_text(cfg.wechat, f"❌ {message}", to_user)
                return False, message

            if not _verify_current_page(page):
                if _revert_config(config_path, old_cookies):
                    message = "扫码获取的 Cookies 未能通过当前智能文档页面验证，已回滚配置。"
                else:
                    message = "扫码获取的 Cookies 未能通过验证，且回滚失败！磁盘配置状态未知，请勿重启，请人工检查并恢复配置文件后再操作。"
                send_text(cfg.wechat, f"❌ {message}", to_user)
                return False, message

            fields_str = "、".join(updated_fields)
            send_markdown(
                cfg.wechat,
                f"""## ✅ 扫码续期成功

> **验证状态**：有效
> **更新字段**：{fields_str}

新 Cookies 已通过智能文档验证并写入配置，正在同步服务运行时配置。""",
                to_user,
            )
            return True, "扫码续期成功"

        except PlaywrightTimeout as e:
            message = f"二维码登录流程超时：{e}"
            logger.error(message)
            send_text(cfg.wechat, f"❌ {message}", to_user)
            return False, message
        except Exception as e:
            message = f"二维码登录流程异常：{e}"
            logger.error(message, exc_info=True)
            send_text(cfg.wechat, f"❌ {message}", to_user)
            return False, message
        finally:
            browser.close()
            try:
                if qr_path.exists():
                    qr_path.unlink()
            except Exception:
                pass


def _build_doc_url(source) -> str:
    return f"{DOC_BASE}/smartsheet/{source.doc_id}?scode={source.scode}&tab={source.tab_id}&viewId={source.view_id}"


def _capture_enterprise_qr(page, qr_path: str) -> bool:
    selectors = [
        "img.wwLogin_qrcode_img",
        "img[src*='/wwlogin/login/qrcode']",
    ]

    deadline = time.time() + 30
    while time.time() < deadline:
        for frame in page.frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() > 0:
                        box = locator.bounding_box(timeout=3000)
                        if box and box.get("width", 0) > 100 and box.get("height", 0) > 100:
                            locator.screenshot(path=qr_path, timeout=10000, animations="disabled")
                            return True
                except Exception:
                    continue
        page.wait_for_timeout(1000)
    return False


def _wait_login_success(page, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    last_url = page.url

    while time.time() < deadline:
        page.wait_for_timeout(3000)
        current_url = page.url
        if current_url != last_url:
            logger.info(f"QR login page url changed: {current_url}")
            last_url = current_url

        if _looks_logged_in(page):
            return True

    return False


def _looks_logged_in(page) -> bool:
    if "login" not in page.url.lower() and "doc.weixin.qq.com/smartsheet" in page.url:
        try:
            page.wait_for_function(
                """() => !!(
                    window.ContainerApp &&
                    window.ContainerApp.containerSdk &&
                    window.ContainerApp.containerSdk.smartSheetSdk
                )""",
                timeout=5000,
            )
            return True
        except Exception:
            pass

    try:
        body_text = page.inner_text("body", timeout=3000)
        login_words = ["企业微信扫码登录", "请使用企业微信扫描二维码登录", "企业身份登录", "切换个人身份登录"]
        return not any(word in body_text for word in login_words)
    except Exception:
        return False


def _verify_current_page(page) -> bool:
    try:
        if "login" in page.url.lower():
            return False
        page.wait_for_function(
            """() => !!(
                window.ContainerApp &&
                window.ContainerApp.containerSdk &&
                window.ContainerApp.containerSdk.smartSheetSdk &&
                window.ContainerApp.containerSdk.smartSheetSdk.editor &&
                window.ContainerApp.containerSdk.smartSheetSdk.editor.getCore
            )""",
            timeout=30000,
        )
        return True
    except Exception as e:
        logger.error(f"Current page verification failed: {e}")
        return False


def _extract_cookie_values(cookies: list[dict]) -> dict:
    values = {}
    for cookie in cookies:
        name = cookie.get("name", "")
        value = cookie.get("value", "")
        if name in COOKIE_FIELDS and value:
            values[name] = value
    return values


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--config", required=True)
    parser.add_argument("--to-user", default=None)
    parser.add_argument("--reason", default="Cookies 已过期")
    args = parser.parse_args()
    if args.worker:
        worker_cfg = load_config(args.config)
        ok, msg = _renew_cookies_by_qr_locked(args.config, worker_cfg, args.to_user, args.reason)
        print(msg)
        raise SystemExit(0 if ok else 1)
