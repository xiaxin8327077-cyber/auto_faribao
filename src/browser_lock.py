"""全局浏览器互斥锁与低内存启动辅助。

911MB 小内存服务器上，多个 chromium 并发会触发 swap，
导致微信文档 JS SDK 初始化超时。本模块确保任一时刻只有一个 chromium 运行，
并统一低内存启动参数以降低单实例内存占用。
"""
import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# 全局浏览器互斥锁：所有 chromium 操作串行执行，避免内存争抢
_BROWSER_LOCK = threading.Lock()

# 低内存启动参数（保守组合，不含 --single-process 以保证稳定性）
_LOW_MEM_ARGS = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",  # 避免 /dev/shm 不足导致崩溃
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
]


@contextmanager
def browser_operation():
    """串行化所有 chromium 操作的上下文管理器。

    用法：
        with browser_operation():
            with sync_playwright() as p:
                browser = launch_browser(p)
                ...
    确保前一个浏览器完全关闭（含 browser.close）后才释放锁，
    下一个操作才能启动新浏览器。
    """
    _BROWSER_LOCK.acquire()
    try:
        yield
    finally:
        _BROWSER_LOCK.release()


def launch_browser(p):
    """以低内存参数启动 headless chromium。

    统一所有启动点参数，降低单实例内存占用。
    """
    return p.chromium.launch(headless=True, args=_LOW_MEM_ARGS)
