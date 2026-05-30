#!/usr/bin/env python3
import cgi
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


HOST = os.getenv("STATUS_PAGE_HOST", "0.0.0.0")
PORT = int(os.getenv("STATUS_PAGE_PORT", "80"))
REFRESH = max(3, int(os.getenv("STATUS_PAGE_REFRESH", "8")))
SAMPLE = max(10, int(os.getenv("STATUS_PAGE_SAMPLE_INTERVAL", "60")))
METRIC_INTERVAL = max(10, int(os.getenv("STATUS_PAGE_METRIC_INTERVAL", "20")))
METRIC_KEEP = max(60, int(os.getenv("STATUS_PAGE_METRIC_KEEP", "240")))
STATE_DIR = os.getenv("STATUS_PAGE_STATE_DIR", "/var/lib/status-page")
STATE_FILE = os.path.join(STATE_DIR, "traffic_state.json")
REPORT_HISTORY_FILE = os.path.join(STATE_DIR, "report_history.json")
METRICS_FILE = os.path.join(STATE_DIR, "metric_history.json")
UPLOAD_DIR = os.path.join(STATE_DIR, "uploads")
AUTH_FILE = os.path.join(STATE_DIR, "auth_config.json")
DEFAULT_FULL_PASSWORD = "570927904"
DEFAULT_READONLY_PASSWORD = "123456"
COOKIE_NAME = "status_page_session"
COOKIE_AGE = 12 * 60 * 60
MAX_UPLOAD = 200 * 1024 * 1024
PREVIEW_LIMIT = 64 * 1024
TEXT_PREVIEW_EXTS = {
    ".txt",
    ".log",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".conf",
    ".cfg",
    ".csv",
    ".py",
    ".sh",
    ".js",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".xml",
    ".sql",
    ".toml",
    ".env",
}
IMAGE_PREVIEW_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

SESSIONS = {}
LOGIN_RATE = {}
TAILSCALE_CACHE = {"ts": 0.0, "data": {}}
XRAY_CACHE = {"ts": 0.0, "data": {}}
METRICS_STATE = None

LOCK = threading.Lock()
TLOCK = threading.Lock()
MLOCK = threading.Lock()
RATELOCK = threading.Lock()
TSLOCK = threading.Lock()
XLOCK = threading.Lock()
RECLOCK = threading.RLock()

CN_TZ = ZoneInfo("Asia/Shanghai") if ZoneInfo else timezone(timedelta(hours=8), name="Asia/Shanghai")
LEGACY_TIMEZONE_LABEL = "UTC+08:00"
TIMEZONE_LABEL = "Asia/Shanghai"


def cn_now():
    return datetime.now(CN_TZ)


def fmt_dt(dt):
    return dt.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def parse_saved_time(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for suffix, tzinfo in ((" UTC", timezone.utc), (f" {LEGACY_TIMEZONE_LABEL}", CN_TZ), (f" {TIMEZONE_LABEL}", CN_TZ)):
        if text.endswith(suffix):
            base = text[: -len(suffix)]
            try:
                return datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tzinfo)
            except ValueError:
                pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=CN_TZ)
    except ValueError:
        return None


def normalize_saved_time(value, fallback=None):
    parsed = parse_saved_time(value)
    if parsed:
        return fmt_dt(parsed)
    return fallback or now_local()


def now_local():
    return fmt_dt(cn_now())


def today_key():
    return cn_now().strftime("%Y-%m-%d")


def fmt_bytes(value):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(value)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def fmt_secs(value):
    value = int(value)
    days, rem = divmod(value, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def ensure_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def hash_password(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def save_auth_config(config):
    ensure_dirs()
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def load_auth_config():
    ensure_dirs()
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
            if not isinstance(config, dict):
                config = {}
    except Exception:
        config = {}

    password_cfg = config.get("passwords")
    if not isinstance(password_cfg, dict):
        password_cfg = {}

    changed = False
    if not password_cfg.get("full_hash"):
        raw = str(password_cfg.get("full_password") or DEFAULT_FULL_PASSWORD).strip()
        password_cfg["full_hash"] = hash_password(raw)
        password_cfg.pop("full_password", None)
        changed = True
    if not password_cfg.get("readonly_hash"):
        raw = str(password_cfg.get("readonly_password") or DEFAULT_READONLY_PASSWORD).strip()
        password_cfg["readonly_hash"] = hash_password(raw)
        password_cfg.pop("readonly_password", None)
        changed = True

    password_cfg["updated_at"] = normalize_saved_time(password_cfg.get("updated_at"), now_local())
    config["passwords"] = password_cfg
    if changed or not os.path.exists(AUTH_FILE):
        save_auth_config(config)
    return config


def password_role(password):
    candidate = hash_password(str(password).strip())
    config = load_auth_config()["passwords"]
    if hmac.compare_digest(candidate, config["full_hash"]):
        return "full"
    if hmac.compare_digest(candidate, config["readonly_hash"]):
        return "readonly"
    return ""


def password_info():
    config = load_auth_config()["passwords"]
    return {
        "updated_at": config.get("updated_at", now_local()),
        "storage": "password_hash",
        "full_enabled": True,
        "readonly_enabled": True,
    }


def validate_new_password(value, label):
    value = str(value).strip()
    if len(value) < 4:
        raise ValueError(f"{label}至少需要 4 个字符。")
    if len(value) > 64:
        raise ValueError(f"{label}最多支持 64 个字符。")
    return value


def update_passwords(current_password, new_full="", new_readonly=""):
    config = load_auth_config()
    password_cfg = config["passwords"]
    if password_role(current_password) != "full":
        raise ValueError("当前完整版密码不正确。")

    new_full = str(new_full).strip()
    new_readonly = str(new_readonly).strip()
    if not new_full and not new_readonly:
        raise ValueError("请至少填写一个新密码。")

    next_full_hash = password_cfg["full_hash"]
    next_readonly_hash = password_cfg["readonly_hash"]
    changed_labels = []

    if new_full:
        next_full_hash = hash_password(validate_new_password(new_full, "完整版密码"))
        changed_labels.append("完整版密码")
    if new_readonly:
        next_readonly_hash = hash_password(validate_new_password(new_readonly, "只读版密码"))
        changed_labels.append("只读版密码")

    if next_full_hash == next_readonly_hash:
        raise ValueError("完整版密码与只读版密码不能相同。")

    password_cfg["full_hash"] = next_full_hash
    password_cfg["readonly_hash"] = next_readonly_hash
    password_cfg["updated_at"] = now_local()
    save_auth_config(config)
    return {
        "message": "已更新：" + "、".join(changed_labels),
        "updated_at": password_cfg["updated_at"],
    }


def os_name():
    data = {}
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    key, value = line.split("=", 1)
                    data[key] = value.strip().strip('"')
    except OSError:
        return sys.platform
    return data.get("PRETTY_NAME", sys.platform)


def cpu_info():
    model = "Unknown CPU"
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"model": model, "cores": os.cpu_count() or 0}


def mem_info():
    info = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    key, value = line.split(":", 1)
                    info[key] = int(value.strip().split()[0]) * 1024
    except OSError:
        return {"total": 0, "used": 0, "available": 0, "percent": 0}

    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = max(total - available, 0)
    percent = round((used / total * 100) if total else 0, 1)
    return {"total": total, "used": used, "available": available, "percent": percent}


def uptime():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return float(f.read().split()[0])
    except OSError:
        return 0.0


def loads():
    try:
        a, b, c = os.getloadavg()
        return [round(a, 2), round(b, 2), round(c, 2)]
    except OSError:
        return [0.0, 0.0, 0.0]


def disks():
    items = []
    for mount in ["/", "/boot"]:
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            continue
        used = usage.total - usage.free
        items.append(
            {
                "mount": mount,
                "total": usage.total,
                "used": used,
                "free": usage.free,
                "percent": round((used / usage.total * 100) if usage.total else 0, 1),
            }
        )
    return items


def net_totals():
    total_rx = total_tx = ts_rx = ts_tx = 0
    ifaces = []
    ts_ifaces = []
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f.readlines()[2:]:
                name, rest = line.split(":", 1)
                iface = name.strip()
                if iface == "lo":
                    continue
                cols = rest.split()
                rx = int(cols[0])
                tx = int(cols[8])
                total_rx += rx
                total_tx += tx
                ifaces.append(iface)
                if iface.startswith("tailscale"):
                    ts_ifaces.append(iface)
                    ts_rx += rx
                    ts_tx += tx
    except OSError:
        pass
    return {
        "rx": total_rx,
        "tx": total_tx,
        "ts_rx": ts_rx,
        "ts_tx": ts_tx,
        "ifaces": sorted(ifaces),
        "ts_ifaces": sorted(ts_ifaces),
    }


def load_traffic():
    ensure_dirs()
    totals = net_totals()
    stamp = now_local()
    day = today_key()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            if not isinstance(state, dict):
                state = {}
    except Exception:
        state = {
            "tracking_started_at": stamp,
            "last": {"rx": totals["rx"], "tx": totals["tx"], "ts_rx": totals["ts_rx"], "ts_tx": totals["ts_tx"]},
            "days": {day: {"rx": 0, "tx": 0, "ts_rx": 0, "ts_tx": 0, "updated_at": stamp}},
        }

    state.setdefault("tracking_started_at", stamp)
    state.setdefault("days", {})
    state.setdefault("last", {})
    state["tracking_started_at"] = normalize_saved_time(state.get("tracking_started_at"), stamp)
    if "last_seen_at" in state:
        state["last_seen_at"] = normalize_saved_time(state.get("last_seen_at"), stamp)

    for key, fallback in {"rx": totals["rx"], "tx": totals["tx"], "ts_rx": totals["ts_rx"], "ts_tx": totals["ts_tx"]}.items():
        try:
            state["last"][key] = int(state["last"].get(key, fallback))
        except Exception:
            state["last"][key] = int(fallback)

    for day_key, rec in list(state["days"].items()):
        if not isinstance(rec, dict):
            rec = {}
            state["days"][day_key] = rec
        rec.setdefault("rx", 0)
        rec.setdefault("tx", 0)
        rec.setdefault("ts_rx", 0)
        rec.setdefault("ts_tx", 0)
        rec["updated_at"] = normalize_saved_time(rec.get("updated_at"), stamp)

    state["days"].setdefault(day, {"rx": 0, "tx": 0, "ts_rx": 0, "ts_tx": 0, "updated_at": stamp})
    state["days"][day]["updated_at"] = normalize_saved_time(state["days"][day].get("updated_at"), stamp)
    return state, totals


def save_traffic(state):
    ensure_dirs()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_traffic():
    with TLOCK:
        state, totals = load_traffic()
        stamp = now_local()
        day = today_key()
        item = state["days"][day]
        last = state["last"]
        for key in ["rx", "tx", "ts_rx", "ts_tx"]:
            delta = totals[key] - int(last.get(key, totals[key]))
            if delta < 0:
                delta = totals[key]
            item[key] = int(item.get(key, 0)) + delta
        item["updated_at"] = stamp
        state["last"] = {"rx": totals["rx"], "tx": totals["tx"], "ts_rx": totals["ts_rx"], "ts_tx": totals["ts_tx"]}
        state["last_seen_at"] = stamp
        if len(state["days"]) > 30:
            for old in sorted(state["days"].keys())[:-30]:
                state["days"].pop(old, None)
        save_traffic(state)

        days = []
        for day_key in sorted(state["days"].keys(), reverse=True):
            rec = state["days"][day_key]
            days.append(
                {
                    "date": day_key,
                    "rx": int(rec.get("rx", 0)),
                    "tx": int(rec.get("tx", 0)),
                    "ts_rx": int(rec.get("ts_rx", 0)),
                    "ts_tx": int(rec.get("ts_tx", 0)),
                    "updated_at": rec.get("updated_at", stamp),
                }
            )
        return {
            "tracking_started_at": state["tracking_started_at"],
            "timezone": TIMEZONE_LABEL,
            "ifaces": totals["ifaces"],
            "ts_ifaces": totals["ts_ifaces"],
            "today": days[0] if days else {"date": day, "rx": 0, "tx": 0, "ts_rx": 0, "ts_tx": 0, "updated_at": stamp},
            "days": days,
        }


def traffic_worker():
    while True:
        try:
            update_traffic()
        except Exception as exc:
            sys.stderr.write(f"traffic error: {exc}\n")
        time.sleep(SAMPLE)


def read_cpu_jiffies():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first = f.readline().split()
        if not first or first[0] != "cpu":
            return []
        return [int(item) for item in first[1:8]]
    except OSError:
        return []


def cpu_percent_from(prev, current):
    if not prev or not current or len(prev) < 5 or len(current) < 5:
        return 0.0
    prev_idle = prev[3] + prev[4]
    current_idle = current[3] + current[4]
    total_delta = sum(current) - sum(prev)
    idle_delta = current_idle - prev_idle
    if total_delta <= 0:
        return 0.0
    busy = total_delta - idle_delta
    return round(max(0.0, min(100.0, busy / total_delta * 100.0)), 1)


def load_metrics_state():
    ensure_dirs()
    try:
        with open(METRICS_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            if not isinstance(state, dict):
                state = {}
    except Exception:
        state = {}

    samples = state.get("samples")
    if not isinstance(samples, list):
        samples = []

    cleaned = []
    for item in samples[-METRIC_KEEP:]:
        if not isinstance(item, dict):
            continue
        try:
            cleaned.append(
                {
                    "epoch": int(item.get("epoch", 0)),
                    "timestamp": normalize_saved_time(item.get("timestamp"), now_local()),
                    "cpu_percent": round(float(item.get("cpu_percent", 0)), 1),
                    "memory_percent": round(float(item.get("memory_percent", 0)), 1),
                    "load1": round(float(item.get("load1", 0)), 2),
                    "load5": round(float(item.get("load5", 0)), 2),
                    "load15": round(float(item.get("load15", 0)), 2),
                }
            )
        except Exception:
            continue

    last_cpu = state.get("last_cpu")
    if not isinstance(last_cpu, list):
        last_cpu = read_cpu_jiffies()

    try:
        last_epoch = float(state.get("last_epoch", 0) or 0)
    except Exception:
        last_epoch = 0.0

    return {"samples": cleaned, "last_cpu": last_cpu, "last_epoch": last_epoch}


def save_metrics_state(state):
    ensure_dirs()
    payload = {
        "samples": state.get("samples", [])[-METRIC_KEEP:],
        "last_cpu": state.get("last_cpu", []),
        "last_epoch": state.get("last_epoch", 0),
    }
    with open(METRICS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def metrics_state_locked():
    global METRICS_STATE
    if METRICS_STATE is None:
        METRICS_STATE = load_metrics_state()
    return METRICS_STATE


def capture_metric_sample(force=False):
    with MLOCK:
        state = metrics_state_locked()
        now_epoch = time.time()
        if not force and state.get("last_epoch") and now_epoch - state["last_epoch"] < max(3, METRIC_INTERVAL - 2):
            return {"interval": METRIC_INTERVAL, "samples": list(state.get("samples", []))}

        current_cpu = read_cpu_jiffies()
        prev_cpu = state.get("last_cpu") or current_cpu
        sample_loads = loads()
        sample_mem = mem_info()
        sample = {
            "epoch": int(now_epoch),
            "timestamp": now_local(),
            "cpu_percent": cpu_percent_from(prev_cpu, current_cpu),
            "memory_percent": sample_mem["percent"],
            "load1": sample_loads[0],
            "load5": sample_loads[1],
            "load15": sample_loads[2],
        }
        state["samples"].append(sample)
        state["samples"] = state["samples"][-METRIC_KEEP:]
        state["last_cpu"] = current_cpu
        state["last_epoch"] = now_epoch
        save_metrics_state(state)
        return {"interval": METRIC_INTERVAL, "samples": list(state["samples"])}


def metrics_worker():
    while True:
        try:
            capture_metric_sample(force=True)
        except Exception as exc:
            sys.stderr.write(f"metric error: {exc}\n")
        time.sleep(METRIC_INTERVAL)


def rate_state_locked(ip_addr):
    item = LOGIN_RATE.get(ip_addr)
    if not isinstance(item, dict):
        item = {"attempts": [], "fails": [], "blocked_until": 0.0}
        LOGIN_RATE[ip_addr] = item
    return item


def consume_login_slot(ip_addr):
    now_ts = time.time()
    with RATELOCK:
        item = rate_state_locked(ip_addr)
        item["attempts"] = [ts for ts in item.get("attempts", []) if now_ts - ts < 60]
        item["fails"] = [ts for ts in item.get("fails", []) if now_ts - ts < 10 * 60]
        blocked_until = float(item.get("blocked_until", 0) or 0)
        if blocked_until > now_ts:
            return False, int(max(1, blocked_until - now_ts))
        item["attempts"].append(now_ts)
        if len(item["attempts"]) > 12:
            item["blocked_until"] = now_ts + 90
            return False, 90
    return True, 0


def record_login_result(ip_addr, success):
    now_ts = time.time()
    with RATELOCK:
        item = rate_state_locked(ip_addr)
        item["attempts"] = [ts for ts in item.get("attempts", []) if now_ts - ts < 60]
        item["fails"] = [ts for ts in item.get("fails", []) if now_ts - ts < 10 * 60]
        if success:
            item["fails"] = []
            item["blocked_until"] = 0.0
            return
        item["fails"].append(now_ts)
        if len(item["fails"]) >= 6:
            item["blocked_until"] = max(float(item.get("blocked_until", 0) or 0), now_ts + 10 * 60)


def session_from_header(cookie_header):
    if not cookie_header:
        return None
    jar = cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
    except cookies.CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    if not morsel:
        return None
    sid = morsel.value
    with LOCK:
        now_ts = time.time()
        for key in list(SESSIONS.keys()):
            if SESSIONS[key]["exp"] < now_ts:
                SESSIONS.pop(key, None)
        item = SESSIONS.get(sid)
        if not item:
            return None
        item["exp"] = now_ts + COOKIE_AGE
        return {"id": sid, "role": item["role"]}


def create_session(role):
    with LOCK:
        sid = secrets.token_urlsafe(24)
        SESSIONS[sid] = {"role": role, "exp": time.time() + COOKIE_AGE}
        return sid


def root_path():
    ensure_dirs()
    return Path(UPLOAD_DIR).resolve()


def norm_rel(rel=""):
    rel = str(rel or "").replace("\\", "/").strip()
    if rel in {"", ".", "/"}:
        return ""
    parts = [part for part in Path(rel).parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("非法路径。")
    return "/".join(parts)


def safe_path(rel=""):
    target = (root_path() / norm_rel(rel)).resolve()
    if not target.is_relative_to(root_path()):
        raise ValueError("非法路径。")
    return target


def rel_of(path_obj):
    rel = path_obj.resolve().relative_to(root_path()).as_posix()
    return "" if rel == "." else rel


def ascii_download_name(name):
    cleaned = "".join(ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\"} else "_" for ch in name).strip(" .")
    return cleaned or "download"


def preview_kind(path_obj):
    suffix = path_obj.suffix.lower()
    mime = mimetypes.guess_type(path_obj.name)[0] or ""
    if suffix in IMAGE_PREVIEW_EXTS or mime.startswith("image/"):
        return "image"
    if suffix in TEXT_PREVIEW_EXTS or mime.startswith("text/"):
        return "text"
    return ""


def list_files(rel="", query_text=""):
    current = safe_path(rel)
    if not current.exists() or not current.is_dir():
        raise FileNotFoundError("目录不存在。")

    search_text = str(query_text or "").strip().lower()
    all_items = []
    for child in sorted(current.iterdir(), key=lambda p: (0 if p.is_dir() else 1, p.name.lower())):
        stats = child.stat()
        item = {
            "name": child.name,
            "path": rel_of(child),
            "type": "dir" if child.is_dir() else "file",
            "size": stats.st_size if child.is_file() else 0,
            "size_human": "-" if child.is_dir() else fmt_bytes(stats.st_size),
            "modified": fmt_dt(datetime.fromtimestamp(stats.st_mtime, CN_TZ)),
            "previewable": child.is_file() and bool(preview_kind(child)),
            "preview_kind": preview_kind(child) if child.is_file() else "",
        }
        all_items.append(item)

    items = [item for item in all_items if search_text in item["name"].lower()] if search_text else all_items
    parent = rel_of(current.parent) if current != root_path() else ""
    return {
        "current_path": rel_of(current),
        "parent_path": parent,
        "query": str(query_text or ""),
        "total_count": len(all_items),
        "filtered_count": len(items),
        "items": items,
    }


def unique_target(folder, name):
    target = folder / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    idx = 1
    while True:
        candidate = folder / f"{stem} ({idx}){suffix}"
        if not candidate.exists():
            return candidate
        idx += 1


def delete_targets(raw_paths):
    if not raw_paths:
        raise ValueError("请选择要删除的文件或目录。")

    seen = set()
    targets = []
    for raw in raw_paths:
        target = safe_path(raw)
        if target == root_path():
            raise ValueError("根目录不允许删除。")
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)

    targets.sort(key=lambda item: len(item.parts), reverse=True)
    deleted = []
    for target in targets:
        if not target.exists():
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        deleted.append(target.name)

    if not deleted:
        raise ValueError("未找到可删除项。")
    return deleted


def read_text_preview(path_obj, limit=PREVIEW_LIMIT):
    with open(path_obj, "rb") as f:
        chunk = f.read(limit + 1)
    truncated = len(chunk) > limit
    chunk = chunk[:limit]
    if b"\x00" in chunk:
        raise ValueError("该文件不支持文本预览。")
    for encoding in ["utf-8", "gb18030", "latin-1"]:
        try:
            text = chunk.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = chunk.decode("utf-8", errors="replace")
    return text, truncated


def preview_payload(rel):
    target = safe_path(rel)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError("文件不存在。")

    stats = target.stat()
    kind = preview_kind(target)
    base = {
        "name": target.name,
        "path": rel_of(target),
        "kind": kind or "unsupported",
        "size_human": fmt_bytes(stats.st_size),
        "modified": fmt_dt(datetime.fromtimestamp(stats.st_mtime, CN_TZ)),
        "download_url": "/download?path=" + quote(rel_of(target)),
    }

    if kind == "image":
        base["view_url"] = "/view?path=" + quote(rel_of(target))
        return base
    if kind == "text":
        text, truncated = read_text_preview(target)
        base["content"] = text
        base["truncated"] = truncated
        return base
    return base


def run_cmd(args, timeout=5):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "").strip(), (exc.stderr or "timeout").strip()


def collect_tailscale_status():
    installed = bool(shutil.which("tailscale"))
    systemctl = bool(shutil.which("systemctl"))
    service = "unknown"
    enabled = "unknown"

    tailscale_uptime = ""
    tailscale_started_at = ""
    if systemctl:
        rc, out, _ = run_cmd(["systemctl", "is-active", "tailscaled"], timeout=4)
        service = out or ("active" if rc == 0 else "inactive")
        rc, out, _ = run_cmd(["systemctl", "is-enabled", "tailscaled"], timeout=4)
        enabled = out or "unknown"

        # collect uptime
        rc_ts, out_ts, _ = run_cmd(["systemctl", "show", "tailscaled", "--property=ActiveEnterTimestamp"], timeout=4)
        if rc_ts == 0 and out_ts:
            ts_raw = out_ts.split("=", 1)[-1].strip().replace(" UTC", "")
            try:
                ts_dt = datetime.strptime(ts_raw.split(".")[0], "%a %Y-%m-%d %H:%M:%S")
                if ZoneInfo:
                    ts_dt = ts_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(CN_TZ)
                diff = datetime.now(CN_TZ) - ts_dt
                days = diff.days
                hours, rem = divmod(diff.seconds, 3600)
                minutes = rem // 60
                if days > 0:
                    tailscale_uptime = f"{days}天 {hours}小时"
                elif hours > 0:
                    tailscale_uptime = f"{hours}小时 {minutes}分钟"
                else:
                    tailscale_uptime = f"{minutes}分钟"
                tailscale_started_at = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                tailscale_uptime = ""
                tailscale_started_at = ""
        else:
            tailscale_uptime = ""
            tailscale_started_at = ""

    data = {
        "installed": installed,
        "service": service,
        "enabled": enabled,
        "uptime": tailscale_uptime,
        "started_at": tailscale_started_at,
        "running": service == "active",
        "backend": "stopped" if service != "active" else "running",
        "version": "未安装",
        "ips": [],
        "dns_name": "",
        "checked_at": now_local(),
    }

    if not installed:
        return data

    rc, out, err = run_cmd(["tailscale", "version"], timeout=4)
    if out:
        data["version"] = out.splitlines()[0]
    elif err:
        data["version"] = err
    else:
        data["version"] = "已安装"

    rc, out, _ = run_cmd(["tailscale", "status", "--json"], timeout=4)
    if rc == 0 and out:
        try:
            status = json.loads(out)
            data["backend"] = status.get("BackendState") or data["backend"]
            self_node = status.get("Self") or {}
            data["dns_name"] = self_node.get("DNSName", "") or ""
            ips = self_node.get("TailscaleIPs") or []
            if isinstance(ips, list):
                data["ips"] = [item for item in ips if isinstance(item, str)]
        except Exception:
            pass
    elif data["running"]:
        rc, out, _ = run_cmd(["tailscale", "ip", "-4"], timeout=4)
        if rc == 0 and out:
            data["ips"] = [line.strip() for line in out.splitlines() if line.strip()]

    return data


def tailscale_status(refresh=False):
    now_ts = time.time()
    with TSLOCK:
        if not refresh and TAILSCALE_CACHE.get("data") and now_ts - TAILSCALE_CACHE.get("ts", 0) < 5:
            return TAILSCALE_CACHE["data"]
    data = collect_tailscale_status()
    with TSLOCK:
        TAILSCALE_CACHE["ts"] = now_ts
        TAILSCALE_CACHE["data"] = data
    return data


def set_tailscale_service(action):
    if action not in {"start", "stop"}:
        raise ValueError("不支持的 Tailscale 操作。")
    if not shutil.which("systemctl"):
        raise ValueError("当前系统不支持 systemctl。")
    rc, _, err = run_cmd(["systemctl", action, "tailscaled"], timeout=10)
    if rc != 0:
        raise ValueError(err or f"Tailscale {action} 失败。")
    return tailscale_status(refresh=True)



def collect_xray_status():
    xray_bin = shutil.which("xray") or ("/usr/local/bin/xray" if os.path.exists("/usr/local/bin/xray") else "")
    installed = bool(xray_bin)
    systemctl = bool(shutil.which("systemctl"))
    service = "unknown"
    enabled = "unknown"
    config_path = "/usr/local/etc/xray/config.json"

    if systemctl:
        rc, out, _ = run_cmd(["systemctl", "is-active", "xray"], timeout=4)
        service = out or ("active" if rc == 0 else "inactive")
        rc, out, _ = run_cmd(["systemctl", "is-enabled", "xray"], timeout=4)
        enabled = out or "unknown"

    data = {
        "installed": installed,
        "service": service,
        "enabled": enabled,
        "running": service == "active",
        "version": "未安装",
        "port": "",
        "protocol": "",
        "security": "",
        "network": "",
        "config_readable": False,
        "listening": False,
        "listener_detail": "",
        "pid": "",
        "memory": "",
        "uptime": "",
        "checked_at": now_local(),
    }
    if not installed:
        return data

    rc, out, err = run_cmd([xray_bin, "version"], timeout=4)
    if out:
        data["version"] = out.splitlines()[0]
    elif err:
        data["version"] = err
    else:
        data["version"] = "已安装"

    rc, out, _ = run_cmd(["systemctl", "cat", "xray"], timeout=4)
    if rc == 0 and out:
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.startswith("ExecStart="):
                parts = ln.split("-config ")
                if len(parts) > 1:
                    config_path = parts[1].strip().split()[0]
                break

    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as cf:
                cfg = json.load(cf)
            inbounds = [ib for ib in (cfg.get("inbounds") or []) if isinstance(ib, dict)]
            protocols = []
            securities = []
            networks = []
            ports = []
            for inbound in inbounds:
                if inbound.get("protocol"):
                    protocols.append(str(inbound["protocol"]).upper())
                if inbound.get("port"):
                    ports.append(str(inbound["port"]))
                stream = inbound.get("streamSettings") or {}
                if stream.get("security"):
                    securities.append(str(stream["security"]).upper())
                if stream.get("network"):
                    networks.append(str(stream["network"]).upper())
            data["config_readable"] = True
            data["protocol"] = "、".join(dict.fromkeys(protocols))
            data["security"] = "、".join(dict.fromkeys(securities))
            data["network"] = "、".join(dict.fromkeys(networks))
            if ports:
                data["port"] = ports[0]
        except Exception:
            pass

    rc, out, _ = run_cmd(["ss", "-lntH"], timeout=4)
    if rc == 0 and out and data["port"]:
        suffix = f":{data['port']}"
        matches = []
        for ln in out.splitlines():
            parts = ln.split()
            local_addr = parts[3] if len(parts) > 3 else ln
            if local_addr.endswith(suffix):
                matches.append(local_addr)
        matches = list(dict.fromkeys(matches))
        data["listening"] = bool(matches)
        data["listener_detail"] = "、".join(matches)

    if data["running"]:
        rc, out, _ = run_cmd(["systemctl", "show", "xray", "--property=MainPID,MemoryCurrent,ActiveEnterTimestamp"], timeout=4)
        if rc == 0 and out:
            props = {}
            for ln in out.splitlines():
                if "=" in ln:
                    k, v = ln.split("=", 1)
                    props[k.strip()] = v.strip()
            if props.get("MainPID") and props["MainPID"] != "0":
                data["pid"] = props["MainPID"]
            if props.get("MemoryCurrent"):
                try:
                    mem_kb = int(props["MemoryCurrent"]) // 1024
                    data["memory"] = f"{mem_kb / 1024:.1f} MB" if mem_kb >= 1024 else f"{mem_kb} KB"
                except ValueError:
                    pass
            if props.get("ActiveEnterTimestamp"):
                ts_str = props["ActiveEnterTimestamp"].replace(" UTC", "")
                try:
                    ts_dt = datetime.strptime(ts_str.split(".")[0], "%a %Y-%m-%d %H:%M:%S")
                    if ZoneInfo:
                        ts_dt = ts_dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(CN_TZ)
                    diff = datetime.now(CN_TZ) - ts_dt
                    days = diff.days
                    hours, rem = divmod(diff.seconds, 3600)
                    minutes = rem // 60
                    if days > 0:
                        data["uptime"] = f"{days}天 {hours}小时"
                    elif hours > 0:
                        data["uptime"] = f"{hours}小时 {minutes}分钟"
                    else:
                        data["uptime"] = f"{minutes}分钟"
                except Exception:
                    pass
    return data


def xray_status(refresh=False):
    now_ts = time.time()
    with XLOCK:
        if not refresh and XRAY_CACHE.get("data") and now_ts - XRAY_CACHE.get("ts", 0) < 5:
            return XRAY_CACHE["data"]
    data = collect_xray_status()
    with XLOCK:
        XRAY_CACHE["ts"] = now_ts
        XRAY_CACHE["data"] = data
    return data


def set_xray_service(action):
    if action not in {"start", "stop", "restart"}:
        raise ValueError("不支持的 Xray 操作。")
    if not shutil.which("systemctl"):
        raise ValueError("当前系统不支持 systemctl。")
    rc, _, err = run_cmd(["systemctl", action, "xray"], timeout=10)
    if rc != 0:
        raise ValueError(err or f"Xray {action} 失败。")
    return xray_status(refresh=True)


def status_payload():
    mem = mem_info()
    metric_history = capture_metric_sample(force=False)
    traffic = update_traffic()
    disks_info = [
        {
            "mount": item["mount"],
            "used_human": fmt_bytes(item["used"]),
            "free_human": fmt_bytes(item["free"]),
            "total_human": fmt_bytes(item["total"]),
            "percent": item["percent"],
        }
        for item in disks()
    ]
    metric_samples = metric_history.get("samples", [])
    current_cpu_percent = metric_samples[-1]["cpu_percent"] if metric_samples else 0
    return {
        "generated_at": now_local(),
        "timezone": TIMEZONE_LABEL,
        "sample": SAMPLE,
        "metric_interval": METRIC_INTERVAL,
        "server": {
            "hostname": socket.gethostname(),
            "fqdn": socket.getfqdn(),
            "os": os_name(),
            "kernel": os.uname().release,
            "python": sys.version.split()[0],
            "uptime_human": fmt_secs(uptime()),
        },
        "cpu": {"current_percent": current_cpu_percent, **cpu_info()},
        "memory": {
            "total_human": fmt_bytes(mem["total"]),
            "used_human": fmt_bytes(mem["used"]),
            "available_human": fmt_bytes(mem["available"]),
            "percent": mem["percent"],
        },
        "load": loads(),
        "disks": disks_info,
        "traffic": traffic,
        "metrics": metric_history,
        "tailscale": tailscale_status(refresh=False),
        "xray": xray_status(refresh=False),
    }


LOGIN_PAGE_TEMPLATE = """<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>访问验证</title>
<link rel='preconnect' href='https://fonts.googleapis.com'>
<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&family=Inter:wght@400;500;600&display=swap' rel='stylesheet'>
<link href='https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0' rel='stylesheet'>
<style>
:root{--bg:#00101b;--bg2:#10283b;--panel:rgba(255,255,255,.07);--ink:#edf6ff;--muted:#8b9fb0;--accent:#00675f;--accent2:#28bcff;--line:rgba(255,255,255,.10);--danger:#fb5151;--shadow:0 28px 80px rgba(0,0,0,.38)}
*{box-sizing:border-box}
html,body{margin:0}
body{min-height:100vh;font-family:'Inter','Microsoft YaHei',sans-serif;background:radial-gradient(circle at 18% 18%,rgba(0,103,95,.32) 0,rgba(0,103,95,0) 28%),radial-gradient(circle at 84% 14%,rgba(40,188,255,.16) 0,rgba(40,188,255,0) 24%),linear-gradient(145deg,var(--bg) 0,var(--bg2) 62%,#12364b 100%);color:var(--ink);display:grid;place-items:center;padding:24px;overflow:hidden}
body::before{content:'';position:fixed;inset:0;background-image:radial-gradient(circle at 2px 2px,rgba(255,255,255,.05) 1px,transparent 0);background-size:40px 40px;opacity:.42;pointer-events:none}
.material-symbols-outlined{font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24}
.login-card{position:relative;width:min(480px,100%);background:var(--panel);border:1px solid rgba(255,255,255,.10);border-radius:28px;box-shadow:var(--shadow);padding:32px 30px 28px;backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px)}
.login-head{display:grid;gap:10px;margin-bottom:20px;text-align:center}
.login-brand{width:60px;height:60px;margin:0 auto 6px;border-radius:18px;background:linear-gradient(135deg,#00675f,#28bcff);display:grid;place-items:center;box-shadow:0 0 24px rgba(0,103,95,.28)}
.login-brand .material-symbols-outlined{font-size:32px;color:#e9f4ff}
.login-kicker{font-size:12px;letter-spacing:.26em;text-transform:uppercase;color:#8b9fb0}
.login-title{font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:34px;font-weight:800;letter-spacing:-.02em;color:#ffffff}
.login-sub{color:var(--muted);font-size:14px;line-height:1.8;max-width:340px;margin:0 auto}
.field{display:grid;gap:8px;margin-top:14px}
.field span{font-size:12px;color:#9ab0c1;text-transform:uppercase;letter-spacing:.16em}
.input{width:100%;border:1px solid rgba(255,255,255,.10);border-radius:16px;padding:14px 15px;font-size:16px;outline:none;background:rgba(0,16,27,.48);color:#fff}
.input::placeholder{color:rgba(139,159,176,.48)}
.input:focus{border-color:rgba(126,240,226,.58);box-shadow:0 0 0 4px rgba(0,103,95,.18)}
.ghost-btn,.primary-btn{appearance:none;border:none;border-radius:16px;padding:12px 14px;font:inherit;cursor:pointer;font-weight:700}
.ghost-btn{background:rgba(255,255,255,.06);color:#dce9f5;border:1px solid rgba(255,255,255,.10)}
.primary-btn{width:100%;margin-top:18px;background:linear-gradient(135deg,var(--accent),#0a8d7f);color:#fff;box-shadow:0 16px 34px rgba(0,103,95,.34)}
.primary-btn:disabled,.ghost-btn:disabled{opacity:.65;cursor:wait}
.hint{min-height:20px;margin-top:12px;font-size:14px;color:var(--danger);text-align:center}
.login-foot{margin-top:18px;color:var(--muted);font-size:12px;text-align:center}
.login-foot:empty{display:none}
</style>
</head>
<body>
  <div class='login-card'>
    <div class='login-head'>
      <div class='login-brand'><span class='material-symbols-outlined'>shield</span></div>
      <div class='login-kicker'>The Warden</div>
      <div class='login-title'>访问登录</div>
      <div class='login-sub'>输入访问密码后继续。</div>
    </div>
    <label class='field'><span>访问密码</span><input id='pwd' class='input' type='password' autocomplete='current-password' placeholder='请输入访问密码'></label>
    <div id='msg' class='hint'></div>
    <button id='btn' class='primary-btn' type='button'>进入页面</button>
    <div class='login-foot'></div>
  </div>
<script>
function setMsg(text){ document.getElementById('msg').textContent = text || ''; }
async function login(){
  const btn = document.getElementById('btn');
  const password = document.getElementById('pwd').value.trim();
  if(!password){ setMsg('请输入访问密码。'); return; }
  btn.disabled = true;
  btn.textContent = '验证中...';
  try{
    const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password:password})});
    const data = await r.json();
    if(!r.ok){
      setMsg(data.message || '验证失败。');
      return;
    }
    location.href = '/';
  }catch(err){
    setMsg('请求失败，请稍后重试。');
  }finally{
    btn.disabled = false;
    btn.textContent = '进入页面';
  }
}
document.getElementById('btn').onclick = () => login();
document.getElementById('pwd').addEventListener('keydown', event => { if(event.key === 'Enter'){ login(); }});
</script>
</body>
</html>
"""

FULL_FILE_NAV = "<button class='nav-btn' type='button' data-tab-target='files'>文件管理</button>"
FULL_SETTINGS_NAV = "<button class='nav-btn' type='button' data-tab-target='settings'>访问设置</button>"
FULL_TAILSCALE_ACTIONS = """
FULL_FILE_NAV = "<button class='nav-btn' type='button' data-tab-target='files'><span class='material-symbols-outlined nav-icon'>folder_open</span><span>鏂囦欢绠＄悊</span></button>"
FULL_SETTINGS_NAV = "<button class='nav-btn' type='button' data-tab-target='settings'><span class='material-symbols-outlined nav-icon'>admin_panel_settings</span><span>璁块棶璁剧疆</span></button>"
<div class='tail-actions'>
  <button id='tsStartBtn' class='ghost-btn' type='button'>启动 Tailscale</button>
  <button id='tsStopBtn' class='danger-btn' type='button'>停止 Tailscale</button>
</div>
"""

FULL_TAILSCALE_ACTIONS = """
<div class='tail-actions'>
  <button id='tsStartBtn' class='ghost-btn' type='button'>鍚姩 Tailscale</button>
  <button id='tsStopBtn' class='danger-btn' type='button'>鍋滄 Tailscale</button>
</div>
"""

FULL_FILE_NAV = "<button class='nav-btn' type='button' data-tab-target='files'><span class='material-symbols-outlined nav-icon'>folder_open</span><span>鏂囦欢绠＄悊</span></button>"
FULL_SETTINGS_NAV = "<button class='nav-btn' type='button' data-tab-target='settings'><span class='material-symbols-outlined nav-icon'>admin_panel_settings</span><span>璁块棶璁剧疆</span></button>"

FULL_TAILSCALE_ACTIONS = """
<div class='tail-actions'>
  <button id='tsStartBtn' class='ghost-btn' type='button'>Start Tailscale</button>
  <button id='tsStopBtn' class='danger-btn' type='button'>Stop Tailscale</button>
</div>
"""

FULL_FILE_NAV = "<button class='nav-btn' type='button' data-tab-target='files'><span class='material-symbols-outlined nav-icon'>folder_open</span><span>Files</span></button>"
FULL_SETTINGS_NAV = "<button class='nav-btn' type='button' data-tab-target='settings'><span class='material-symbols-outlined nav-icon'>admin_panel_settings</span><span>Access</span></button>"


FULL_XRAY_ACTIONS = """
<div class="tail-actions">
  <button id="xrStartBtn" class="ghost-btn" type="button">启动 Xray</button>
  <button id="xrStopBtn" class="danger-btn" type="button">停止 Xray</button>
  <button id="xrRestartBtn" class="ghost-btn" type="button">重启 Xray</button>
</div>
"""
FULL_REPORT_NAV = """<button class="nav-btn" type="button" data-tab-target="report"><span class="material-symbols-outlined nav-icon">send</span><span>日报发送</span></button>"""
FULL_REPORT_SECTION = """<section class="tab-panel" data-tab="report">
  <section class="panel section">
    <div class="section-head">
      <div>
        <div class="eyebrow">Daily Report</div>
        <h2>日报发送</h2>
        <p class="section-copy">选择日期后点击发送，系统将自动从智能表格提取当日任务并提交日报到 OA 系统。</p>
      </div>
    </div>
    <div class="tool-grid">
      <div class="tool-card">
        <div class="tool-title">补发日报</div>
        <div class="tool-muted" style="margin-top:4px">选择需要补发的日期，系统将自动提取智能表格内容</div>
        <div class="form-grid" style="margin-top:14px">
          <label><span>日报日期</span><input class="input" type="date" id="reportDate" /></label>
          <label><span>快捷日期</span>
            <div style="display:flex;gap:6px;flex-wrap:wrap;align-self:end">
              <button class="ghost-btn compact-btn" type="button" onclick="setReportDate(0)">今天</button>
              <button class="ghost-btn compact-btn" type="button" onclick="setReportDate(-1)">昨天</button>
              <button class="ghost-btn compact-btn" type="button" onclick="setReportDate(-2)">前天</button>
              <button class="ghost-btn compact-btn" type="button" onclick="setReportDate(-3)">三天前</button>
            </div>
          </label>
        </div>
        <div style="display:flex;gap:10px;margin-top:14px;align-items:center">
          <button id="sendReportBtn" type="button" onclick="sendReport()">发送日报</button>
          <span id="reportMsg" class="hint-line" style="margin-top:0"></span>
        </div>
      </div>
    </div>
  </section>
  <section class="panel section">
    <div class="section-head">
      <div class="eyebrow">History</div>
      <h3>发送记录</h3>
    </div>
    <div class="table-shell">
      <table>
        <thead>
          <tr><th>时间</th><th>类型</th><th>日期</th><th>内容</th><th>状态</th><th>详情</th></tr>
        </thead>
        <tbody id="reportHistoryBody">
          <tr><td colspan="6" style="text-align:center;color:#667989">暂无发送记录</td></tr>
        </tbody>
      </table>
    </div>
  </section>
</section>"""

FULL_FILE_SECTION = """
<section class='tab-panel' data-tab='files'>
  <section class='panel section'>
    <div class='section-head'>
      <div>
        <div class='eyebrow'>Workspace</div>
        <h2>文件管理</h2>
        <p class='section-copy'>支持搜索、批量删除、图片与文本预览，下载仍直接保存到你的本地设备。</p>
      </div>
      <div class='toolbar-inline'>
        <span id='filePath' class='path-pill'>/</span>
        <button id='upBtn' class='ghost-btn' type='button'>返回上一级</button>
        <button id='refreshFilesBtn' class='ghost-btn' type='button'>刷新</button>
      </div>
    </div>
    <div class='tool-grid'>
      <div class='tool-card'>
        <div class='tool-title'>搜索与批量操作</div>
        <div class='tool-row'><input id='searchInput' class='input' type='text' placeholder='按文件名搜索当前目录'><button id='searchBtn' type='button'>搜索</button><button id='clearSearchBtn' class='ghost-btn' type='button'>清空</button></div>
        <div class='tool-row'><button id='deleteSelectedBtn' class='danger-btn' type='button'>删除选中</button><span id='selectionInfo' class='tool-muted'>已选 0 项</span></div>
      </div>
      <div class='tool-card'>
        <div class='tool-title'>新建文件夹</div>
        <div class='tool-row'><input id='folderName' class='input' type='text' placeholder='输入新文件夹名称'><button id='mkBtn' type='button'>创建</button></div>
      </div>
      <div class='tool-card wide'>
        <div class='tool-title'>上传文件</div>
        <div class='tool-row'><input id='uploadInput' class='input' type='file' multiple><button id='upFileBtn' type='button'>上传</button></div>
      </div>
    </div>
    <div id='fileMsg' class='hint-line'></div>
    <div class='table-meta'><span id='fileSummary'>目录加载中...</span><span id='fileSearchMeta'>当前未使用搜索</span></div>
    <div class='file-layout'>
      <div class='table-shell'>
        <table>
          <thead><tr><th class='tiny'><input id='selectAllFiles' type='checkbox'></th><th>名称</th><th>类型</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead>
          <tbody id='fileRows'><tr><td colspan='6'>加载中...</td></tr></tbody>
        </table>
      </div>
      <aside class='preview-shell'>
        <div class='preview-head'>
          <div><div class='eyebrow'>Preview</div><h3>文件预览</h3></div>
          <button id='clearPreviewBtn' class='ghost-btn' type='button'>清空预览</button>
        </div>
        <div id='previewMeta' class='preview-meta'>选择图片或文本文件后可在这里查看内容。</div>
        <div id='previewBody' class='preview-body preview-empty'>暂无预览内容</div>
      </aside>
    </div>
  </section>
</section>
"""

FULL_SETTINGS_SECTION = """
<section class='tab-panel' data-tab='settings'>
  <section class='panel section'>
    <div class='section-head'>
      <div>
        <div class='eyebrow'>Access Settings</div>
        <h2>访问设置</h2>
        <p class='section-copy'>修改后立即生效，页面只保存密码哈希，不回显明文。</p>
      </div>
    </div>
    <div class='tool-grid'>
      <div class='tool-card'>
        <div class='tool-title'>密码状态</div>
        <div class='settings-meta'>
          <div><span>最近更新时间</span><strong id='pwdUpdatedAt'>加载中...</strong></div>
          <div><span>存储方式</span><strong id='pwdStorage'>加载中...</strong></div>
        </div>
      </div>
      <div class='tool-card wide'>
        <div class='tool-title'>修改密码</div>
        <div class='form-grid'>
          <label><span>当前完整版密码</span><input id='currentPassword' class='input' type='password' placeholder='用于验证当前权限'></label>
          <label><span>新完整版密码</span><input id='newFullPassword' class='input' type='password' placeholder='留空则不修改'></label>
          <label><span>确认新完整版密码</span><input id='confirmFullPassword' class='input' type='password' placeholder='再次输入新完整版密码'></label>
          <label><span>新只读版密码</span><input id='newReadonlyPassword' class='input' type='password' placeholder='留空则不修改'></label>
          <label><span>确认新只读版密码</span><input id='confirmReadonlyPassword' class='input' type='password' placeholder='再次输入新只读版密码'></label>
        </div>
        <div class='tool-row'><button id='changePasswordBtn' type='button'>保存修改</button><button id='resetPasswordFormBtn' class='ghost-btn' type='button'>清空表单</button></div>
        <div id='passwordMsg' class='hint-line'></div>
      </div>
    </div>
  </section>
</section>
"""

DASHBOARD_TEMPLATE = """<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>服务器状态页</title>
<link rel='preconnect' href='https://fonts.googleapis.com'>
<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&family=Inter:wght@400;500;600;700&display=swap' rel='stylesheet'>
<link href='https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0' rel='stylesheet'>
<style>
:root{--bg:#f0f7ff;--panel:#ffffff;--panel2:#ffffff;--panel-soft:#e5f2ff;--panel-strong:#d4ebff;--shell:#00101b;--line:#d9e7f2;--ink:#1d313e;--muted:#4a5e6d;--accent:#00675f;--accent2:#28bcff;--accent-soft:#7ef0e2;--warn:#ffb703;--danger:#b31b25;--danger2:#fb5151;--shadow:0 14px 34px rgba(12,39,58,.08)}
*{box-sizing:border-box}
html,body{margin:0}
.material-symbols-outlined{font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24}
body{min-height:100vh;font-family:'Inter','Microsoft YaHei',sans-serif;color:var(--ink);background:var(--bg)}
body::before{display:none}
.app-shell{position:relative;width:100%;max-width:none;margin:0 auto;display:grid;grid-template-columns:256px minmax(0,1fr);gap:0;min-height:100vh}
.panel{background:var(--panel);border:1px solid rgba(155,176,193,.22);border-radius:22px;box-shadow:var(--shadow)}
.sidebar{position:sticky;top:0;align-self:start;height:100vh;padding:24px 16px 18px;display:grid;gap:16px;background:var(--shell);box-shadow:24px 0 40px rgba(0,0,0,.18)}
.brand{display:flex;align-items:center;gap:12px;padding:0 8px 8px}
.brand-mark{width:44px;height:44px;border-radius:14px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;box-shadow:0 0 24px rgba(0,103,95,.26)}
.brand-mark .material-symbols-outlined{font-size:26px;color:#e9f4ff}
.brand-title{font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-.03em}
.brand-sub{font-size:11px;color:#7e93a5;margin-top:4px;letter-spacing:.26em;text-transform:uppercase}
.access-card{padding:18px;border-radius:18px;background:linear-gradient(145deg,#0b2435,#00675f);color:#f4fbff;border:1px solid rgba(255,255,255,.06)}
.access-card .small{font-size:11px;color:rgba(244,251,255,.64);text-transform:uppercase;letter-spacing:.16em}
.access-card .value{font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:28px;font-weight:800;margin-top:8px}
.access-card .copy{font-size:13px;line-height:1.75;color:rgba(244,251,255,.84);margin-top:10px}
.side-nav{display:grid;gap:8px}
button{appearance:none;border:none;border-radius:12px;padding:10px 16px;min-height:42px;font:inherit;font-size:14px;line-height:1;cursor:pointer;font-weight:700;white-space:nowrap;display:inline-flex;align-items:center;justify-content:center;gap:8px;flex:0 0 auto;background:var(--accent);color:#fff;box-shadow:0 10px 24px rgba(0,103,95,.16)}
button:disabled{opacity:.62;cursor:not-allowed;box-shadow:none}
.nav-btn{width:100%;text-align:left;justify-content:flex-start;background:transparent;color:#8b9fb0;box-shadow:none;border-radius:14px;border-left:2px solid transparent;padding:12px 14px}
.nav-btn.active{background:rgba(0,103,95,.16);color:#ffffff;border-left-color:#14b8a6;box-shadow:none}
.nav-icon{font-size:20px}
.nav-btn[data-tab-target='overview']::before,.logout-btn::before{font-family:'Material Symbols Outlined';font-size:20px;line-height:1}
.nav-btn[data-tab-target='overview']::before{content:'dns'}
.logout-btn::before{content:'logout'}
.ghost-btn{background:#ffffff;color:#1d313e;box-shadow:none;border:1px solid rgba(102,121,137,.14)}
.danger-btn{background:linear-gradient(135deg,#c73b2b,#d1495b);color:#fff}
.compact-btn{padding:9px 12px;min-height:38px;font-size:13px}
.sidebar-meta{padding:12px 14px;border-radius:16px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.06);color:#a8bccd;font-size:12px;line-height:1.7}
.logout-btn{width:100%;background:rgba(255,255,255,.05);color:#eef7ff;border:1px solid rgba(255,255,255,.08)}
.main{display:grid;gap:16px;padding:24px}
.hero{padding:24px;display:grid;grid-template-columns:minmax(0,1.1fr) minmax(300px,.9fr);gap:18px}
.hero h1{margin:0;font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:clamp(28px,3vw,46px);line-height:1.02;letter-spacing:-.04em}
.hero-copy{margin-top:8px;color:var(--muted);line-height:1.7;max-width:52ch;font-size:14px}
.hero-badges{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:999px;background:var(--panel-soft);border:1px solid rgba(0,103,95,.08);color:#415463;font-size:11px;white-space:nowrap;font-weight:700}
.hero-side{display:grid;gap:12px}
.hero-card{padding:16px 18px;border-radius:18px;background:var(--panel2);border:1px solid var(--line)}
.hero-card .small{font-size:11px;color:#667989;text-transform:uppercase;letter-spacing:.18em}
.hero-card .value{font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:22px;font-weight:800;margin-top:8px;line-height:1.18;word-break:break-word;overflow-wrap:anywhere}
.hero-card .copy{font-size:12px;color:#51657b;line-height:1.6;margin-top:8px;word-break:break-word;overflow-wrap:anywhere}
.brand > div:empty,.login-head > div:empty{display:none!important}
.brand::before,.login-head::before,.eyebrow::before{content:none!important;display:none!important}
.tab-panel{display:none}
.tab-panel.active{display:grid;gap:0}
.tab-panel > .panel.section{border-radius:0;box-shadow:none}
.tab-panel > .panel.section:first-child{border-top-left-radius:22px;border-top-right-radius:22px}
.tab-panel > .panel.section:last-child{border-bottom-left-radius:22px;border-bottom-right-radius:22px}
.tab-panel > .panel.section:only-child{border-radius:22px;box-shadow:var(--shadow)}
.tab-panel > .panel.section + .panel.section{margin-top:-1px}
.section{padding:22px 24px}
.section-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap;margin-bottom:14px}
.eyebrow{display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(0,103,95,.10);color:#00675f;font-size:11px;letter-spacing:.18em;text-transform:uppercase;font-weight:700}
.section h2,.section h3{margin:8px 0 0;font-family:'Manrope','Microsoft YaHei',sans-serif}
.section-copy{margin:6px 0 0;color:var(--muted);line-height:1.75;max-width:72ch;font-size:13px}
.metrics-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.metric-card{position:relative;overflow:hidden;padding:18px;border-radius:18px;background:var(--panel2);border:1px solid var(--line);box-shadow:0 8px 22px rgba(12,39,58,.06)}
.metric-label{font-size:11px;color:#667989;text-transform:uppercase;letter-spacing:.16em;font-weight:700}
.metric-value{font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:clamp(20px,1.5vw,28px);font-weight:800;line-height:1.18;margin-top:8px;white-space:normal;word-break:break-word;overflow-wrap:anywhere}
.metric-meta{margin-top:6px;font-size:12px;color:#576b81;line-height:1.55;word-break:break-word;overflow-wrap:anywhere}
.two-col{display:grid;grid-template-columns:minmax(0,1.28fr) minmax(340px,.72fr);gap:16px}
.chart-card,.table-shell,.tool-card,.status-card,.preview-shell{background:var(--panel2);border:1px solid var(--line);border-radius:18px}
.chart-card,.status-card,.tool-card,.preview-shell{padding:20px;box-shadow:0 8px 22px rgba(12,39,58,.06)}
.chart-head{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px}
.chart-head h3,.chart-head h4{margin:0}
.chart-copy{color:var(--muted);font-size:12px;line-height:1.6}
.chart-wrap{min-height:244px;border-radius:16px;background:#f7fbfd;border:1px solid rgba(18,38,58,.06);padding:12px}
.chart-svg{width:100%;height:250px;display:block}
.legend{display:flex;flex-wrap:wrap;gap:12px}
.legend span{display:inline-flex;align-items:center;gap:8px;font-size:12px;color:#4e6379;white-space:nowrap}
.legend i{width:16px;height:3px;border-radius:999px;display:inline-block}
.mini-grid,.status-grid,.gauge-grid{display:grid;gap:14px}
.mini-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
.status-grid{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.status-card{background:var(--shell);border-color:rgba(255,255,255,.06)}
.status-card .chart-copy,.status-card .status-key{color:#8b9fb0}
.status-card .chart-head h3,.status-card .status-value{color:#ffffff}
.status-card .status-copy{color:#c6d5e2}
.status-key{font-size:12px;color:#71839a;text-transform:uppercase;letter-spacing:.12em}
.status-value{font-family:'Manrope','Microsoft YaHei',sans-serif;font-size:clamp(18px,1.2vw,22px);font-weight:800;margin-top:6px;line-height:1.2;word-break:break-word;overflow-wrap:anywhere}
.status-copy{font-size:13px;color:#53687d;line-height:1.6;margin-top:8px}
.tail-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.track{height:10px;border-radius:999px;background:#e5eef3;overflow:hidden;margin-top:10px}
.fill{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.fill.warm{background:linear-gradient(90deg,#f4b942,#ffd166)}
.gauge-box{padding:14px;border-radius:16px;background:#f7fbfd;border:1px solid rgba(18,38,58,.06)}
.gauge{width:136px;height:136px;margin:0 auto;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--accent) 0%,rgba(15,139,141,.12) 0%);position:relative}
.gauge::after{content:'';position:absolute;width:92px;height:92px;border-radius:50%;background:#fff;box-shadow:inset 0 0 0 1px rgba(18,38,58,.06)}
.gauge-center{position:relative;z-index:1;text-align:center}
.gauge-center strong{display:block;font-size:28px;line-height:1}
.gauge-center span{display:block;font-size:13px;color:#6f8297;margin-top:6px}
.table-shell{overflow:hidden}
table{width:100%;border-collapse:collapse}
th,td{padding:14px 14px;border-bottom:1px solid var(--line);text-align:left;font-size:14px;vertical-align:middle}
th{font-size:11px;color:#667989;text-transform:uppercase;letter-spacing:.16em;background:#eef6ff;font-weight:800}
tr:last-child td{border-bottom:none}
.tiny{width:48px}
.table-meta{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-bottom:10px}
.toolbar-inline,.tool-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.tool-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:12px;margin-bottom:10px}
.tool-card.wide{grid-column:1 / -1}
.tool-title{font-size:17px;font-weight:800;font-family:'Manrope','Microsoft YaHei',sans-serif}
.tool-muted{font-size:12px;color:var(--muted)}
.input{appearance:none;border:1px solid #d4e4f1;border-radius:14px;padding:11px 13px;font:inherit;background:#fff;min-width:0;flex:1}
.input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 4px rgba(15,139,141,.10)}
input[type='file']::file-selector-button{appearance:none;border:none;border-radius:12px;padding:8px 12px;margin-right:10px;background:var(--panel-soft);color:#274a66;font:inherit;font-size:13px;font-weight:700;cursor:pointer}
.path-pill{display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;background:var(--panel-soft);border:1px solid var(--line);font-size:12px;color:#274a66;max-width:100%;word-break:break-all}
.file-layout{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,.45fr);gap:12px}
.file-actions{display:flex;gap:8px;flex-wrap:nowrap}
.file-actions button{min-width:64px;padding:8px 12px;font-size:13px}
.file-layout .table-shell table th:last-child,.file-layout .table-shell table td:last-child{width:220px;white-space:nowrap}
.file-layout .table-shell table th:nth-child(5),.file-layout .table-shell table td:nth-child(5){width:160px}
.file-layout .table-shell table th:nth-child(4),.file-layout .table-shell table td:nth-child(4){width:88px}
.file-layout .table-shell table th:nth-child(3),.file-layout .table-shell table td:nth-child(3){width:74px}
.tool-card:first-child .tool-row:first-of-type{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:center}
.tool-card:first-child .tool-row:last-of-type{flex-wrap:nowrap;gap:12px}
.tool-card:nth-child(2) .tool-row,.tool-card.wide .tool-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center}
#selectionInfo{white-space:nowrap}
.link-btn{color:#11666f;text-decoration:none;font-weight:700}
.preview-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.preview-meta{color:var(--muted);font-size:12px;line-height:1.6;margin-top:10px}
.preview-body{margin-top:12px;min-height:360px;max-height:620px;overflow:auto;border-radius:16px;background:#f7fbfd;border:1px solid rgba(18,38,58,.06);padding:12px}
.preview-empty{display:grid;place-items:center;color:#7a8da2;text-align:center}
.preview-body img{display:block;max-width:100%;height:auto;margin:0 auto;border-radius:16px}
.preview-body pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:'Cascadia Code','Consolas',monospace;font-size:13px;line-height:1.7;color:#183248}
.settings-meta{display:grid;gap:10px;margin-top:12px}
.settings-meta div{padding:12px 14px;border-radius:16px;background:#f7fbfd;border:1px solid rgba(18,38,58,.06)}
.settings-meta span{display:block;font-size:12px;color:#74869d;letter-spacing:.12em;text-transform:uppercase}
.settings-meta strong{display:block;font-size:18px;margin-top:6px;font-family:'Manrope','Microsoft YaHei',sans-serif}
.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:12px}
.form-grid label{display:grid;gap:6px}
.form-grid span{font-size:13px;color:#4e657a}
.hint-line{min-height:20px;margin-top:10px;font-size:13px;color:#0b6970}
.footer-note{color:#7c90a4;font-size:12px;text-align:center;padding:4px 0 8px}
@media (max-width:1180px){.app-shell{grid-template-columns:1fr}.sidebar{position:static}.hero,.two-col,.file-layout,.tool-grid,.form-grid{grid-template-columns:1fr}.tab-panel > .panel.section{border-radius:22px;box-shadow:var(--shadow)}.tab-panel > .panel.section + .panel.section{margin-top:12px}}
@media (max-width:1480px){.metrics-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:1080px){.metrics-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:760px){.app-shell{padding:10px}.hero,.section,.sidebar{padding:16px}.metrics-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.mini-grid,.gauge-grid{grid-template-columns:1fr}th,td{padding:11px 10px}.table-shell{overflow:auto}.status-value,.hero-card .value{font-size:18px}}
@media (max-width:560px){.metrics-grid{grid-template-columns:1fr}.hero h1{font-size:26px}.metric-value{font-size:22px}}
</style>
</head>
<body>
<div class='app-shell'>
  <aside class='sidebar'>
    <div class='brand'><div class='brand-mark'><span class='material-symbols-outlined'>shield</span></div><div><div class='brand-title'>The Warden</div><div class='brand-sub'>Admin Console</div></div></div>
    <div class='access-card'><div class='small'>当前访问级别</div><div class='value'>@@ACCESS_LABEL@@</div><div class='copy'>@@MODE_HINT@@</div></div>
    <nav class='side-nav'>
      <button class='nav-btn active' type='button' data-tab-target='overview'>系统状态</button>
      @@FILE_NAV@@
      @@SETTINGS_NAV@@
      @@REPORT_NAV@@
    </nav>
    <div class='sidebar-meta'><div id='generatedAtSide'>最后刷新：加载中...</div><div>自动刷新：@@REFRESH@@ 秒</div><div>采样间隔：<span id='metricIntervalSide'>加载中...</span></div></div>
    <button id='logoutBtn' class='ghost-btn logout-btn' type='button'>退出登录</button>
  </aside>

  <main class='main'>
    <section class='panel hero'>
      <div>
        <div class='eyebrow'>Runtime Overview</div>
        <h1>服务器状态总览</h1>
        <div class='hero-copy'>系统资源、网络流量、Tailscale / Xray 状态和文件管理都集中在这里，页面时间统一按东八区显示。</div>
        <div class='hero-badges'><span class='badge'>访问级别：@@ACCESS_LABEL@@</span><span class='badge'>刷新频率：@@REFRESH@@ 秒</span></div>
      </div>
      <div class='hero-side'>
        <div class='hero-card'><div class='small'>主机</div><div id='heroHost' class='value'>加载中...</div><div id='heroKernel' class='copy'>正在获取系统信息...</div></div>
        <div class='hero-card'><div class='small'>最新状态</div><div id='heroGenerated' class='value'>加载中...</div><div id='heroGeneratedMeta' class='copy'>等待首次状态数据...</div></div>
      </div>
    </section>

    <section class='tab-panel active' data-tab='overview'>
      <section class='panel section'>
        <div class='section-head'><div><div class='eyebrow'>Core Metrics</div><h2>系统状态</h2><p class='section-copy'>这里展示当前 CPU、内存、负载、磁盘和网络状态，同时保留最近一段时间的趋势数据。</p></div></div>
        <div class='metrics-grid'>
          <article class='metric-card'><div class='metric-label'>主机名</div><div id='hostname' class='metric-value'>-</div><div id='fqdn' class='metric-meta'>-</div></article>
          <article class='metric-card'><div class='metric-label'>系统信息</div><div id='osName' class='metric-value'>-</div><div id='kernel' class='metric-meta'>-</div></article>
          <article class='metric-card'><div class='metric-label'>当前 CPU 占用</div><div id='cpuCurrent' class='metric-value'>-</div><div id='cpuModel' class='metric-meta'>-</div></article>
          <article class='metric-card'><div class='metric-label'>内存使用率</div><div id='memoryPercent' class='metric-value'>-</div><div id='memoryDetail' class='metric-meta'>-</div></article>
          <article class='metric-card'><div class='metric-label'>系统负载</div><div id='loadAvg' class='metric-value'>-</div><div class='metric-meta'>1 / 5 / 15 分钟平均负载</div></article>
          <article class='metric-card'><div class='metric-label'>运行时长</div><div id='uptime' class='metric-value'>-</div><div class='metric-meta'>状态页服务持续在线</div></article>
          <article class='metric-card'><div class='metric-label'>今日总流入</div><div id='inTotal' class='metric-value'>-</div><div class='metric-meta'>包含公网与 Tailscale</div></article>
          <article class='metric-card'><div class='metric-label'>今日总流出</div><div id='outTotal' class='metric-value'>-</div><div class='metric-meta'>包含公网与 Tailscale</div></article>
        </div>
      </section>

      <section class='panel section'>
        <div class='two-col'>
          <div class='chart-card'>
            <div class='chart-head'><div><h3>CPU / 内存趋势</h3><div class='chart-copy'>展示最近一段时间的资源曲线，采样点越往右越新。</div></div><div class='legend'><span><i style='background:#0f8b8d'></i>CPU</span><span><i style='background:#ffb703'></i>内存</span></div></div>
            <div class='chart-wrap'><div id='historyChart'>加载中...</div></div>
          </div>
          <div class='chart-card'>
            <div class='chart-head'><div><h3>资源仪表</h3><div class='chart-copy'>快速查看内存和系统盘占用，并观察 Tailscale 流量占比。</div></div></div>
            <div class='gauge-grid'>
              <div class='gauge-box'><div id='memoryGauge' class='gauge'><div class='gauge-center'><strong id='memoryGaugeValue'>-</strong><span>内存使用率</span></div></div></div>
              <div class='gauge-box'><div id='diskGauge' class='gauge'><div class='gauge-center'><strong id='diskGaugeValue'>-</strong><span>系统盘使用率</span></div></div></div>
            </div>
            <div style='margin-top:16px'>
              <div class='status-key'>Tailscale 流量占比</div>
              <div class='track'><div id='tsInShareBar' class='fill'></div></div>
              <div id='tsInShareValue' class='chart-copy'>-</div>
              <div class='track'><div id='tsOutShareBar' class='fill warm'></div></div>
              <div id='tsOutShareValue' class='chart-copy'>-</div>
            </div>
          </div>
        </div>
      </section>

      <section class='panel section'>
        <div class='two-col'>
          <div class='status-card'>
            <div class='chart-head'><div><h3>Tailscale 服务</h3><div class='chart-copy'>显示服务运行状态，并在完整版中提供启停控制。</div></div></div>
            <div class='status-grid'>
              <div><div class='status-key'>服务状态</div><div id='tsService' class='status-value'>-</div><div id='tsBackend' class='status-copy'>-</div></div>
              <div><div class='status-key'>版本</div><div id='tsVersion' class='status-value' style='font-size:20px'>-</div><div id='tsEnabled' class='status-copy'>-</div></div>
              <div><div class='status-key'>Tailscale IP</div><div id='tsIps' class='status-value' style='font-size:20px'>-</div><div id='tsDns' class='status-copy'>-</div></div>
              <div><div class='status-key'>运行时长</div><div id='tsUptime' class='status-value' style='font-size:20px'>-</div><div id='tsStartedAt' class='status-copy'>-</div></div>
            </div>
            @@TAILSCALE_ACTIONS@@
            <div id='tsMsg' class='hint-line'></div>
          </div>
          <div class='status-card'>
            <div class='chart-head'><div><h3>Xray (v2rayN)</h3><div class='chart-copy'>显示 Xray 代理服务运行状态和端口监听状态。</div></div></div>
            <div class='status-grid'>
              <div><div class='status-key'>服务状态</div><div id='xrService' class='status-value'>-</div><div id='xrServiceDetail' class='status-copy'>-</div></div>
              <div><div class='status-key'>版本</div><div id='xrVersion' class='status-value' style='font-size:20px'>-</div><div id='xrEnabled' class='status-copy'>-</div></div>
              <div><div class='status-key'>监听端口</div><div id='xrPort' class='status-value' style='font-size:20px'>-</div><div id='xrProtocol' class='status-copy'>-</div></div>
              <div><div class='status-key'>PID / 内存</div><div id='xrPid' class='status-value' style='font-size:20px'>-</div><div id='xrMemory' class='status-copy'>-</div></div>
              <div><div class='status-key'>运行时长</div><div id='xrUptime' class='status-value' style='font-size:20px'>-</div><div id='xrConfig' class='status-copy'>-</div></div>
            </div>
            @@XRAY_ACTIONS@@
            <div id='xrMsg' class='hint-line'></div>
          </div>
        </div>
        <div class='chart-card' style='margin-top:24px'>
          <div class='chart-head'><div><h3>近七日流量</h3><div class='chart-copy'>蓝绿色代表流入，暖黄色代表流出。</div></div></div>
          <div class='chart-wrap'><div id='trafficChart'>加载中...</div></div>
        </div>
      </section>

      <section class='panel section'>
        <div class='section-head'><div><div class='eyebrow'>Storage & Traffic</div><h2>磁盘与流量台账</h2><p class='section-copy'>每日流量统计按全部非 loopback 网卡汇总，并单独记录 tailscale0 等 Tailscale 接口流量。</p></div></div>
        <div class='mini-grid'>
          <div class='table-shell'><table><thead><tr><th>挂载点</th><th>已用</th><th>剩余</th><th>总量</th><th>使用率</th></tr></thead><tbody id='diskRows'><tr><td colspan='5'>加载中...</td></tr></tbody></table></div>
          <div class='table-shell'><table><thead><tr><th>日期</th><th>总流入</th><th>总流出</th><th>Tailscale 流入</th><th>Tailscale 流出</th><th>更新时间</th></tr></thead><tbody id='trafficRows'><tr><td colspan='6'>加载中...</td></tr></tbody></table></div>
        </div>
        <div class='table-meta' style='margin-top:14px'><span id='trafficSummary'>加载中...</span><span id='trafficNote'>加载中...</span></div>
      </section>
    </section>

    @@REPORT_SECTION@@
    @@FILE_SECTION@@
    @@SETTINGS_SECTION@@

    <div class='footer-note'>页面时间按本机时间显示。</div>
  </main>
</div>
<script>
const ROLE = @@ROLE_JSON@@;
const REFRESH = Number(@@REFRESH@@);
let currentPath = '';
let currentSearch = '';
let selectedPaths = new Set();
let currentPreviewPath = '';
function esc(value){ return String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;'); }
function fmtBytes(value){ const units=['B','KB','MB','GB','TB']; let num=Number(value||0), idx=0; while(num>=1024 && idx<units.length-1){ num/=1024; idx++; } return num.toFixed(1)+' '+units[idx]; }
function pct(part,total){ const base=Number(total||0); if(!base){ return 0; } return Math.max(0, Math.min(100, Number(part||0)/base*100)); }
function setText(id, value){ const node=document.getElementById(id); if(node){ node.textContent=value; } }
function setGauge(id, percent, textId){ const clamped=Math.max(0, Math.min(100, Number(percent||0))); const node=document.getElementById(id); if(node){ node.style.background=`conic-gradient(#0f8b8d ${clamped}%, rgba(15,139,141,.12) ${clamped}% 100%)`; } setText(textId, `${clamped.toFixed(0)}%`); }
function setFill(id, percent){ const node=document.getElementById(id); if(node){ node.style.width=`${Math.max(0, Math.min(100, Number(percent||0)))}%`; } }
function openTab(name){ const valid=Array.from(document.querySelectorAll('[data-tab]')).map(node => node.getAttribute('data-tab')); const target=valid.includes(name) ? name : 'overview'; document.querySelectorAll('[data-tab-target]').forEach(btn => btn.classList.toggle('active', btn.getAttribute('data-tab-target') === target)); document.querySelectorAll('[data-tab]').forEach(panel => panel.classList.toggle('active', panel.getAttribute('data-tab') === target)); if(target === 'files' && ROLE === 'full'){ loadFiles(currentPath, currentSearch).catch(console.error); } if(target === 'settings' && ROLE === 'full'){ loadPasswordInfo().catch(console.error); } if(target === 'report' && ROLE === 'full'){ setReportDate(0); loadReportHistory(); } if(location.hash !== `#${target}`){ history.replaceState(null, '', `#${target}`); } }
function bindTabMenu(){ document.querySelectorAll('[data-tab-target]').forEach(btn => { btn.onclick = () => openTab(btn.getAttribute('data-tab-target')); }); openTab((location.hash || '#overview').slice(1)); }
function linePath(data, innerW, innerH, padX, padY, key){ if(!data.length){ return ''; } const step=data.length > 1 ? innerW/(data.length-1) : 0; return data.map((item, idx) => { const x=padX + idx * step; const y=padY + innerH - (Math.max(0, Math.min(100, Number(item[key]||0))) / 100) * innerH; return `${idx ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`; }).join(' '); }
function areaPath(data, innerW, innerH, padX, padY, key){ if(!data.length){ return ''; } const step=data.length > 1 ? innerW/(data.length-1) : 0; const firstX=padX; const lastX=padX + step * (data.length - 1); const points=data.map((item, idx) => { const x=padX + idx * step; const y=padY + innerH - (Math.max(0, Math.min(100, Number(item[key]||0))) / 100) * innerH; return `${idx ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`; }).join(' '); return `${points} L${lastX.toFixed(1)},${(padY+innerH).toFixed(1)} L${firstX.toFixed(1)},${(padY+innerH).toFixed(1)} Z`; }
function renderHistoryChart(samples){ const host=document.getElementById('historyChart'); const data=(samples||[]).slice(-36); if(!data.length){ host.innerHTML="<div class='preview-empty'>暂无趋势数据</div>"; return; } const width=760, height=240, padX=34, padY=16, innerW=width-padX*2, innerH=height-padY*2-22; const cpuLine=linePath(data, innerW, innerH, padX, padY, 'cpu_percent'); const memLine=linePath(data, innerW, innerH, padX, padY, 'memory_percent'); const memArea=areaPath(data, innerW, innerH, padX, padY, 'memory_percent'); const labels=[0, Math.floor((data.length-1)/2), data.length-1].filter((v, i, arr) => arr.indexOf(v) === i); const step=data.length > 1 ? innerW/(data.length-1) : 0; const xLabels=labels.map(idx => { const x=padX + idx * step; const stamp=String(data[idx].timestamp || '').slice(11, 16) || '-'; return `<text x='${x}' y='${height-6}' text-anchor='middle' fill='#6d8198' font-size='11'>${esc(stamp)}</text>`; }).join(''); const yGuides=[0,25,50,75,100].map(v => { const y=padY + innerH - (v/100) * innerH; return `<g><line x1='${padX}' y1='${y}' x2='${width-padX}' y2='${y}' stroke='rgba(18,38,58,.08)' stroke-dasharray='4 4'/><text x='8' y='${y+4}' fill='#6d8198' font-size='11'>${v}%</text></g>`; }).join(''); host.innerHTML=`<svg class='chart-svg' viewBox='0 0 ${width} ${height}' preserveAspectRatio='none' role='img' aria-label='CPU和内存趋势图'>${yGuides}<path d='${memArea}' fill='rgba(255,183,3,.12)'></path><path d='${memLine}' fill='none' stroke='#ffb703' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'></path><path d='${cpuLine}' fill='none' stroke='#0f8b8d' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'></path>${xLabels}</svg>`; }
function renderTrafficChart(days){ const host=document.getElementById('trafficChart'); const data=(days||[]).slice(0,7).reverse(); if(!data.length){ host.innerHTML="<div class='preview-empty'>暂无流量数据</div>"; return; } const max=Math.max(1, ...data.flatMap(item => [Number(item.rx||0), Number(item.tx||0)])); const bars=data.map(item => { const rx=Math.max(10, Math.round((Number(item.rx||0)/max)*148)); const tx=Math.max(10, Math.round((Number(item.tx||0)/max)*148)); return `<div style='display:flex;flex-direction:column;align-items:center;gap:10px'><div style='height:156px;display:flex;align-items:flex-end;gap:8px'><div style='width:18px;height:${rx}px;border-radius:999px 999px 6px 6px;background:linear-gradient(180deg,#28b7ba,#0f8b8d)'></div><div style='width:18px;height:${tx}px;border-radius:999px 999px 6px 6px;background:linear-gradient(180deg,#ffd166,#ffb703)'></div></div><div style='font-size:12px;color:#66788f;text-align:center;line-height:1.6'><div>${esc(String(item.date||'').slice(5))}</div><div>${fmtBytes(item.rx)} / ${fmtBytes(item.tx)}</div></div></div>`; }).join(''); host.innerHTML=`<div style='display:grid;grid-template-columns:repeat(${data.length},minmax(0,1fr));gap:12px;height:100%;align-items:end'>${bars}</div>`; }
function renderDisks(disks){ const rows=document.getElementById('diskRows'); const items=disks||[]; rows.innerHTML=items.map(item => `<tr><td>${esc(item.mount)}</td><td>${esc(item.used_human)}</td><td>${esc(item.free_human)}</td><td>${esc(item.total_human)}</td><td>${esc(item.percent)}%</td></tr>`).join('') || "<tr><td colspan='5'>暂无数据</td></tr>"; }
function renderTrafficTable(traffic){ const rows=document.getElementById('trafficRows'); rows.innerHTML=(traffic.days||[]).map(item => `<tr><td>${esc(item.date)}</td><td>${fmtBytes(item.rx)}</td><td>${fmtBytes(item.tx)}</td><td>${fmtBytes(item.ts_rx)}</td><td>${fmtBytes(item.ts_tx)}</td><td>${esc(item.updated_at)}</td></tr>`).join('') || "<tr><td colspan='6'>暂无数据</td></tr>"; const ifaceLabel=(traffic.ifaces||[]).join('、') || '无'; const tsLabel=(traffic.ts_ifaces||[]).join('、') || '未检测到'; setText('trafficSummary', `记账起点：${traffic.tracking_started_at || '-'}，当前网卡：${ifaceLabel}`); setText('trafficNote', `Tailscale 接口：${tsLabel}`); }
function renderTailscale(data){ const serviceText=data.running ? '运行中' : (data.installed ? '未运行' : '未安装'); setText('tsService', serviceText); setText('tsBackend', `后台状态：${data.backend || '-'} | 检查时间：${data.checked_at || '-'}`); setText('tsVersion', data.version || '-'); setText('tsEnabled', `开机自启：${data.enabled || 'unknown'}`); setText('tsIps', (data.ips || []).join('、') || '-'); setText('tsDns', data.dns_name || '未检测到 DNS 名称'); setText('tsUptime', data.uptime || '-'); setText('tsStartedAt', data.started_at || '-'); const startBtn=document.getElementById('tsStartBtn'); const stopBtn=document.getElementById('tsStopBtn'); if(startBtn){ startBtn.disabled=!!data.running; } if(stopBtn){ stopBtn.disabled=!data.running; } }
function renderXray(data){ const serviceText=data.running ? '运行中' : (data.installed ? '未运行' : '未安装'); const protocolText=[data.protocol, data.security, data.network].filter(Boolean).join(' / '); const listenText=data.listening ? `监听正常${data.listener_detail ? '：' + data.listener_detail : ''}` : (data.running ? '未检测到监听端口' : '服务未运行'); setText('xrService', serviceText); setText('xrServiceDetail', `检查时间：${data.checked_at || '-'}`); setText('xrVersion', data.version || '-'); setText('xrEnabled', `开机自启：${data.enabled || 'unknown'}`); setText('xrPort', data.port ? ':' + data.port : '-'); setText('xrProtocol', `${protocolText ? '协议：' + protocolText + ' | ' : ''}${listenText}`); setText('xrPid', data.pid || '-'); setText('xrMemory', data.memory || '-'); setText('xrUptime', data.uptime || '-'); setText('xrConfig', data.config_readable ? '配置文件已读取' : '未读取配置文件'); const startBtn=document.getElementById('xrStartBtn'); const stopBtn=document.getElementById('xrStopBtn'); const restartBtn=document.getElementById('xrRestartBtn'); if(startBtn){ startBtn.disabled=!!data.running; } if(stopBtn){ stopBtn.disabled=!data.running; } if(restartBtn){ restartBtn.disabled=!data.running; } }
async function loadStatus(){ const r=await fetch('/api/status', {cache:'no-store'}); if(r.status===401){ location.href='/'; return; } const data=await r.json(); setText('generatedAtSide', `最后刷新：${data.generated_at}`); setText('metricIntervalSide', `${data.metric_interval} 秒`); setText('heroHost', data.server.hostname); setText('heroKernel', `${data.server.os} | Kernel ${data.server.kernel}`); setText('heroGenerated', data.generated_at); setText('heroGeneratedMeta', `Python ${data.server.python} | 自动刷新 ${data.sample} 秒`); setText('hostname', data.server.hostname); setText('fqdn', data.server.fqdn || '-'); setText('osName', data.server.os); setText('kernel', `Kernel ${data.server.kernel} | Python ${data.server.python}`); setText('cpuCurrent', `${Number(data.cpu.current_percent || 0).toFixed(1)}%`); setText('cpuModel', `${data.cpu.cores} vCPU | ${data.cpu.model}`); setText('memoryPercent', `${Number(data.memory.percent || 0).toFixed(1)}%`); setText('memoryDetail', `已用 ${data.memory.used_human} / 总计 ${data.memory.total_human}，可用 ${data.memory.available_human}`); setText('loadAvg', (data.load || []).join(' / ')); setText('uptime', data.server.uptime_human); setText('inTotal', fmtBytes(data.traffic.today.rx)); setText('outTotal', fmtBytes(data.traffic.today.tx)); setGauge('memoryGauge', data.memory.percent, 'memoryGaugeValue'); const rootDisk=(data.disks || [])[0] || {percent:0}; setGauge('diskGauge', rootDisk.percent || 0, 'diskGaugeValue'); const inShare=pct(data.traffic.today.ts_rx, data.traffic.today.rx); const outShare=pct(data.traffic.today.ts_tx, data.traffic.today.tx); setFill('tsInShareBar', inShare); setFill('tsOutShareBar', outShare); setText('tsInShareValue', `Tailscale 流入占比：${inShare.toFixed(1)}%`); setText('tsOutShareValue', `Tailscale 流出占比：${outShare.toFixed(1)}%`); renderHistoryChart(data.metrics.samples || []); renderTrafficChart(data.traffic.days || []); renderDisks(data.disks || []); renderTrafficTable(data.traffic || {}); renderTailscale(data.tailscale || {});  renderXray(data.xray || {}); }
function fileMsg(text){ setText('fileMsg', text || ''); }
function updateSelectionInfo(){ const info=document.getElementById('selectionInfo'); if(info){ info.textContent=`已选 ${selectedPaths.size} 项`; } const selectAll=document.getElementById('selectAllFiles'); if(selectAll){ const checks=Array.from(document.querySelectorAll('[data-file-check]')); selectAll.checked=!!checks.length && checks.every(node => node.checked); } }
function resetPreview(text){ currentPreviewPath=''; setText('previewMeta', text || '选择图片或文本文件后可在这里查看内容。'); const box=document.getElementById('previewBody'); if(box){ box.className='preview-body preview-empty'; box.innerHTML='暂无预览内容'; } }
async function previewFile(path){ const r=await fetch('/api/preview?path=' + encodeURIComponent(path), {cache:'no-store'}); if(r.status===401){ location.href='/'; return; } const data=await r.json(); if(!r.ok){ resetPreview('预览失败'); fileMsg(data.message || '预览失败。'); return; } currentPreviewPath=data.path || ''; setText('previewMeta', `${data.name} | ${data.size_human} | ${data.modified}`); const box=document.getElementById('previewBody'); box.className='preview-body'; if(data.kind === 'image'){ box.innerHTML=`<img src='${esc(data.view_url)}' alt='${esc(data.name)}'>`; } else if(data.kind === 'text'){ const more=data.truncated ? "\\n\\n[预览已截断，仅展示前 64 KB]" : ''; box.innerHTML=`<pre>${esc((data.content || '') + more)}</pre>`; } else { box.innerHTML=`<div class='preview-empty'>该文件暂不支持在线预览。<br><br><a class='link-btn' href='${esc(data.download_url)}'>下载到本地</a></div>`; } }
function triggerDownload(path, name){ const anchor=document.createElement('a'); anchor.href='/download?path=' + encodeURIComponent(path); anchor.download=name || ''; anchor.rel='noopener'; document.body.appendChild(anchor); anchor.click(); anchor.remove(); fileMsg(`已开始下载：${name}`); }
function renderFileRows(payload){ const current=payload.current_path || ''; const items=payload.items || []; const rows=[]; if(current){ rows.push("<tr><td></td><td><a class='link-btn' href='#' data-parent='1'>返回上一级</a></td><td>目录</td><td>-</td><td>-</td><td>-</td></tr>"); } rows.push(...items.map(item => { const openLink=item.type === 'dir' ? `<a class='link-btn' href='#' data-open='${esc(item.path)}'>${esc(item.name)}</a>` : esc(item.name); const ops=[]; if(item.type === 'dir'){ ops.push(`<button class='ghost-btn' type='button' data-open='${esc(item.path)}'>打开</button>`); } else { if(item.previewable){ ops.push(`<button class='ghost-btn' type='button' data-preview='${esc(item.path)}'>预览</button>`); } ops.push(`<button type='button' data-download='${esc(item.path)}' data-name='${esc(item.name)}'>下载</button>`); } ops.push(`<button class='danger-btn' type='button' data-delete='${esc(item.path)}'>删除</button>`); return `<tr><td><input type='checkbox' data-file-check='${esc(item.path)}'></td><td>${openLink}</td><td>${item.type === 'dir' ? '目录' : '文件'}</td><td>${esc(item.size_human)}</td><td>${esc(item.modified)}</td><td><div class='file-actions'>${ops.join('')}</div></td></tr>`; })); document.getElementById('fileRows').innerHTML=rows.join('') || "<tr><td colspan='6'>当前目录为空</td></tr>"; document.querySelectorAll('[data-open]').forEach(node => { node.onclick = event => { event.preventDefault(); loadFiles(node.getAttribute('data-open'), currentSearch).catch(console.error); }; }); document.querySelectorAll('[data-parent]').forEach(node => { node.onclick = event => { event.preventDefault(); loadFiles(payload.parent_path || '', currentSearch).catch(console.error); }; }); document.querySelectorAll('[data-delete]').forEach(node => { node.onclick = async () => { const target=node.getAttribute('data-delete'); if(!confirm('确认删除这个文件或目录吗？')){ return; } const r=await fetch('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path: target})}); const data=await r.json(); fileMsg(data.message || ''); if(r.ok){ selectedPaths.delete(target); if(currentPreviewPath === target){ resetPreview(); } loadFiles(currentPath, currentSearch).catch(console.error); } }; }); document.querySelectorAll('[data-download]').forEach(node => { node.onclick = event => { event.preventDefault(); triggerDownload(node.getAttribute('data-download'), node.getAttribute('data-name') || 'download'); }; }); document.querySelectorAll('[data-preview]').forEach(node => { node.onclick = event => { event.preventDefault(); previewFile(node.getAttribute('data-preview')).catch(err => fileMsg('预览失败：' + err)); }; }); document.querySelectorAll('[data-file-check]').forEach(node => { const path=node.getAttribute('data-file-check'); node.checked=selectedPaths.has(path); node.onchange = () => { if(node.checked){ selectedPaths.add(path); } else { selectedPaths.delete(path); } updateSelectionInfo(); }; }); updateSelectionInfo(); }
async function loadFiles(path='', q=''){ if(ROLE !== 'full'){ return; } const r=await fetch('/api/files?path=' + encodeURIComponent(path || '') + '&q=' + encodeURIComponent(q || ''), {cache:'no-store'}); if(r.status===401){ location.href='/'; return; } const data=await r.json(); if(!r.ok){ fileMsg(data.message || '目录读取失败。'); return; } currentPath=data.current_path || ''; currentSearch=data.query || ''; setText('filePath', '/' + currentPath); setText('fileSummary', `当前目录共 ${data.total_count} 项，当前显示 ${data.filtered_count} 项`); setText('fileSearchMeta', currentSearch ? `搜索关键字：${currentSearch}` : '当前未使用搜索'); const searchInput=document.getElementById('searchInput'); if(searchInput && searchInput.value !== currentSearch){ searchInput.value=currentSearch; } selectedPaths=new Set(Array.from(selectedPaths).filter(item => (data.items || []).some(node => node.path === item))); renderFileRows(data); }
async function batchDelete(){ const paths=Array.from(selectedPaths); if(!paths.length){ fileMsg('请先勾选要删除的项目。'); return; } if(!confirm(`确认删除已选中的 ${paths.length} 项吗？`)){ return; } const r=await fetch('/api/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paths: paths})}); const data=await r.json(); fileMsg(data.message || ''); if(r.ok){ selectedPaths.clear(); resetPreview(); loadFiles(currentPath, currentSearch).catch(console.error); } }
async function mkdir(){ const name=document.getElementById('folderName').value.trim(); if(!name){ fileMsg('请输入文件夹名称。'); return; } const r=await fetch('/api/mkdir', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path: currentPath, name: name})}); const data=await r.json(); fileMsg(data.message || ''); if(r.ok){ document.getElementById('folderName').value=''; loadFiles(currentPath, currentSearch).catch(console.error); } }
async function upload(){ const input=document.getElementById('uploadInput'); const button=document.getElementById('upFileBtn'); if(!input.files.length){ fileMsg('请先选择要上传的文件。'); return; } const form=new FormData(); for(const file of input.files){ form.append('files', file); } button.disabled=true; button.textContent='上传中...'; try{ const r=await fetch('/api/upload?path=' + encodeURIComponent(currentPath), {method:'POST', body: form}); const data=await r.json(); fileMsg(data.message || ''); if(r.ok){ input.value=''; loadFiles(currentPath, currentSearch).catch(console.error); } } finally { button.disabled=false; button.textContent='上传'; } }
async function loadPasswordInfo(){ if(ROLE !== 'full'){ return; } const r=await fetch('/api/password-info', {cache:'no-store'}); if(r.status===401){ location.href='/'; return; } const data=await r.json(); setText('pwdUpdatedAt', data.updated_at); setText('pwdStorage', data.storage === 'password_hash' ? '密码哈希' : data.storage); }
function resetPasswordForm(){ ['currentPassword','newFullPassword','confirmFullPassword','newReadonlyPassword','confirmReadonlyPassword'].forEach(id => { const node=document.getElementById(id); if(node){ node.value=''; } }); setText('passwordMsg', ''); }
async function changePasswords(){ const current=document.getElementById('currentPassword').value.trim(); const newFull=document.getElementById('newFullPassword').value.trim(); const confirmFull=document.getElementById('confirmFullPassword').value.trim(); const newReadonly=document.getElementById('newReadonlyPassword').value.trim(); const confirmReadonly=document.getElementById('confirmReadonlyPassword').value.trim(); const btn=document.getElementById('changePasswordBtn'); if(!current){ setText('passwordMsg', '请输入当前完整版密码。'); return; } if(!newFull && !newReadonly){ setText('passwordMsg', '请至少填写一个新密码。'); return; } if(newFull && newFull !== confirmFull){ setText('passwordMsg', '两次输入的完整版密码不一致。'); return; } if(newReadonly && newReadonly !== confirmReadonly){ setText('passwordMsg', '两次输入的只读版密码不一致。'); return; } btn.disabled=true; btn.textContent='保存中...'; try{ const r=await fetch('/api/passwords', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({current_password: current, full_password: newFull, readonly_password: newReadonly})}); const data=await r.json(); setText('passwordMsg', data.message || (r.ok ? '密码已更新。' : '密码更新失败。')); if(r.ok){ resetPasswordForm(); loadPasswordInfo().catch(console.error); } } finally { btn.disabled=false; btn.textContent='保存修改'; } }
async function toggleTailscale(action){ const startBtn=document.getElementById('tsStartBtn'); const stopBtn=document.getElementById('tsStopBtn'); if(startBtn){ startBtn.disabled=true; } if(stopBtn){ stopBtn.disabled=true; } setText('tsMsg', action === 'start' ? '正在启动 Tailscale...' : '正在停止 Tailscale...'); try{ const r=await fetch('/api/tailscale', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action: action})}); const data=await r.json(); setText('tsMsg', data.message || ''); if(r.ok){ renderTailscale(data.tailscale || {}); loadStatus().catch(console.error); } } finally { loadStatus().catch(console.error); } }
async function toggleXray(action){ const startBtn=document.getElementById('xrStartBtn'); const stopBtn=document.getElementById('xrStopBtn'); const restartBtn=document.getElementById('xrRestartBtn'); if(startBtn){ startBtn.disabled=true; } if(stopBtn){ stopBtn.disabled=true; } if(restartBtn){ restartBtn.disabled=true; } const labels={'start':'启动','stop':'停止','restart':'重启'}; setText('xrMsg', `正在${labels[action]||action} Xray...`); try{ const r=await fetch('/api/xray', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action: action})}); const data=await r.json(); setText('xrMsg', data.message || ''); if(r.ok){ renderXray(data.xray || {}); loadStatus().catch(console.error); } } finally { loadStatus().catch(console.error); } }
async function logout(){ await fetch('/api/logout', {method:'POST'}); location.href='/'; }
document.getElementById('logoutBtn').onclick = () => logout();
bindTabMenu();
loadStatus().catch(console.error);
setInterval(() => loadStatus().catch(console.error), REFRESH * 1000);
if(ROLE === 'full'){
  document.getElementById('mkBtn').onclick = () => mkdir().catch(err => fileMsg('操作失败：' + err));
  document.getElementById('upFileBtn').onclick = () => upload().catch(err => fileMsg('操作失败：' + err));
  document.getElementById('upBtn').onclick = () => { if(!currentPath){ loadFiles('', currentSearch).catch(console.error); return; } loadFiles(currentPath.includes('/') ? currentPath.slice(0, currentPath.lastIndexOf('/')) : '', currentSearch).catch(console.error); };
  document.getElementById('refreshFilesBtn').onclick = () => loadFiles(currentPath, currentSearch).catch(console.error);
  document.getElementById('searchBtn').onclick = () => loadFiles(currentPath, document.getElementById('searchInput').value.trim()).catch(console.error);
  document.getElementById('searchInput').addEventListener('keydown', event => { if(event.key === 'Enter'){ loadFiles(currentPath, document.getElementById('searchInput').value.trim()).catch(console.error); }});
  document.getElementById('clearSearchBtn').onclick = () => { document.getElementById('searchInput').value=''; loadFiles(currentPath, '').catch(console.error); };
  document.getElementById('deleteSelectedBtn').onclick = () => batchDelete().catch(err => fileMsg('操作失败：' + err));
  document.getElementById('clearPreviewBtn').onclick = () => resetPreview();
  document.getElementById('selectAllFiles').onclick = event => { const checked=!!event.target.checked; document.querySelectorAll('[data-file-check]').forEach(node => { node.checked=checked; const path=node.getAttribute('data-file-check'); if(checked){ selectedPaths.add(path); } else { selectedPaths.delete(path); } }); updateSelectionInfo(); };
  document.getElementById('changePasswordBtn').onclick = () => changePasswords().catch(err => setText('passwordMsg', '操作失败：' + err));
  document.getElementById('resetPasswordFormBtn').onclick = () => resetPasswordForm();
  const tsStartBtn=document.getElementById('tsStartBtn');
  const tsStopBtn=document.getElementById('tsStopBtn');
  if(tsStartBtn){ tsStartBtn.onclick = () => toggleTailscale('start').catch(err => setText('tsMsg', '操作失败：' + err)); }
  if(tsStopBtn){ tsStopBtn.onclick = () => toggleTailscale('stop').catch(err => setText('tsMsg', '操作失败：' + err)); }
  const xrStartBtn=document.getElementById('xrStartBtn');
  const xrStopBtn=document.getElementById('xrStopBtn');
  const xrRestartBtn=document.getElementById('xrRestartBtn');
  if(xrStartBtn){ xrStartBtn.onclick = () => toggleXray('start').catch(err => setText('xrMsg', '操作失败：' + err)); }
  if(xrStopBtn){ xrStopBtn.onclick = () => toggleXray('stop').catch(err => setText('xrMsg', '操作失败：' + err)); }
  if(xrRestartBtn){ xrRestartBtn.onclick = () => toggleXray('restart').catch(err => setText('xrMsg', '操作失败：' + err)); }
}

function setReportDate(offset) {
  var d = new Date();
  d.setDate(d.getDate() + offset);
  var yyyy = d.getFullYear();
  var mm = String(d.getMonth() + 1).padStart(2, "0");
  var dd = String(d.getDate()).padStart(2, "0");
  var dateStr = yyyy + "-" + mm + "-" + dd;
  var el = document.getElementById("reportDate");
  if (el) { el.value = dateStr; }
}

async function sendReport(){
  var btn = document.getElementById("sendReportBtn");
  var msg = document.getElementById("reportMsg");
  var date = document.getElementById("reportDate").value;
  btn.disabled = true;
  btn.textContent = "发送中...";
  msg.textContent = "";
  msg.style.color = "";
  try {
    var r = await fetch("/api/report", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({date:date})});
    var data = await r.json();
    if (r.ok) {
      msg.style.color = data.record && data.record.status === "成功" ? "#52c41a" : "#faad14";
      msg.textContent = "已提交 " + (date || "当天") + " | " + (data.record ? data.record.status : "");
      loadReportHistory();
    } else {
      msg.style.color = "#ff4d4f";
      msg.textContent = (data.message || "未知错误");
    }
  } catch(e) {
    msg.style.color = "#ff4d4f";
    msg.textContent = "网络错误: " + e.message;
  }
  btn.disabled = false;
  btn.textContent = "发送日报";
}

async function loadReportHistory(){
  try {
    var r = await fetch("/api/report-history", {cache:"no-store"});
    if (r.status !== 200) { return; }
    var data = await r.json();
    var records = data.records || [];
    var tbody = document.getElementById("reportHistoryBody");
    if (!records.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#667989">暂无发送记录</td></tr>';
      return;
    }
    tbody.innerHTML = records.slice().reverse().map(function(rec) {
      var d = document.createElement("div");
      d.textContent = rec.content || "";
      var ct = d.innerHTML;
      d.textContent = rec.date || "";
      var dt = d.innerHTML;
      d.textContent = rec.time || "";
      var tm = d.innerHTML;
      d.textContent = rec.type || "";
      var tp = d.innerHTML;
      d.textContent = rec.status || "";
      var st = d.innerHTML;
      d.textContent = rec.detail || "";
      var detail = d.innerHTML;
      var sc = rec.status === "成功" ? "#52c41a" : rec.status === "失败" ? "#ff4d4f" : "#faad14";
      var contentDisplay = ct || "";
      if (contentDisplay.length > 50) { contentDisplay = contentDisplay.substring(0, 50) + "..."; }
      var detailDisplay = detail.replace(/\\n/g, "<br>") || (rec.status === "失败" ? "未知错误" : "-");
      return `<tr><td>${tm}</td><td>${tp}</td><td>${dt}</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${contentDisplay || "(自动提取)"}</td><td style="color:${sc};font-weight:700">${st}</td><td style="font-size:12px;color:#667989;line-height:1.5;max-width:300px;word-break:break-word">${detailDisplay}</td></tr>`;
    }).join("");
  } catch(e) {}
}
</script>
</body>
</html>
"""

def save_report_record(record):
    with RECLOCK:
        records = load_report_records()
        record.setdefault("id", secrets.token_hex(8))
        records.append(record)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(REPORT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        return record["id"]

def load_report_records():
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(REPORT_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def parse_report_source_from_output(text):
    for line in str(text or "").splitlines():
        if line.startswith("Report source:"):
            source = line.split(":", 1)[1].strip()
            if source:
                return source
    return None


def parse_smart_doc_meta_from_output(text):
    meta = {"smart_doc_status": None, "smart_doc_error": None}
    for line in str(text or "").splitlines():
        if line.startswith("Smart doc status:"):
            meta["smart_doc_status"] = line.split(":", 1)[1].strip() or None
        elif line.startswith("Smart doc error:"):
            meta["smart_doc_error"] = line.split(":", 1)[1].strip() or None
    return meta


def send_report_failure_fallback(report_date, detail, report_source=None, smart_doc_status=None, smart_doc_error=None):
    """Best-effort email if the submit subprocess fails before sending its own notice."""
    code = r"""
import sys
sys.path.insert(0, "/home/ubuntu/daily_report")
from src.config import load_config
from src.email_notifier import notify_report_failure

date = sys.argv[1] or None
detail = sys.argv[2]
source = sys.argv[3] or None
smart_status = sys.argv[4] or None
smart_error = sys.argv[5] or None
cfg = load_config("/home/ubuntu/daily_report/config.yaml")
notify_report_failure(
    cfg,
    "状态页提交兜底提醒：日报未成功提交。\n\n失败原因/报错信息：\n" + detail,
    report_date=date,
    report_source=source,
    smart_doc_status=smart_status,
    smart_doc_error=smart_error,
)
"""
    try:
        subprocess.run(
            [
                "/home/ubuntu/daily_report/venv/bin/python",
                "-c",
                code,
                report_date or "",
                str(detail)[-3000:],
                report_source or "",
                smart_doc_status or "",
                smart_doc_error or "",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/home/ubuntu/daily_report",
            check=False,
        )
    except Exception as exc:
        sys.stderr.write(f"fallback email failed: {exc}\n")


def page(role=None):
    if not role:
        return LOGIN_PAGE_TEMPLATE

    access_label = "完整版" if role == "full" else "只读版"
    mode_hint = "可查看状态、管理文件、修改密码，并控制 Tailscale / Xray 服务。" if role == "full" else "仅查看系统状态，不提供文件管理与设置操作。"
    return (
        DASHBOARD_TEMPLATE.replace("@@ACCESS_LABEL@@", access_label)
        .replace("@@MODE_HINT@@", mode_hint)
        .replace("@@ROLE_JSON@@", json.dumps(role))
        .replace("@@REFRESH@@", str(REFRESH))
        .replace("@@FILE_NAV@@", FULL_FILE_NAV if role == "full" else "")
        .replace("@@SETTINGS_NAV@@", FULL_SETTINGS_NAV if role == "full" else "")
        .replace("@@REPORT_NAV@@", FULL_REPORT_NAV if role == "full" else "")
        .replace("@@REPORT_SECTION@@", FULL_REPORT_SECTION if role == "full" else "")
        .replace("@@FILE_SECTION@@", FULL_FILE_SECTION if role == "full" else "")
        .replace("@@SETTINGS_SECTION@@", FULL_SETTINGS_SECTION if role == "full" else "")
        .replace("@@TAILSCALE_ACTIONS@@", FULL_TAILSCALE_ACTIONS if role == "full" else "")
        .replace("@@XRAY_ACTIONS@@", FULL_XRAY_ACTIONS if role == "full" else "")
    )


class Handler(BaseHTTPRequestHandler):
    def client_ip(self):
        headers = getattr(self, "headers", None)
        forwarded = (headers.get("X-Forwarded-For", "") if headers else "").split(",")[0].strip()
        return forwarded or getattr(self, "client_address", ["-"])[0]

    def is_local_request(self):
        return self.client_ip() in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    def send_json(self, code, data, cookie_value=None, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie_value:
            self.send_header("Set-Cookie", cookie_value)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, code, data, cookie_value=None):
        body = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie_value:
            self.send_header("Set-Cookie", cookie_value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path_obj, download=False):
        size = path_obj.stat().st_size
        content_type = mimetypes.guess_type(path_obj.name)[0] or "application/octet-stream"
        fallback_name = ascii_download_name(path_obj.name)
        quoted_name = quote(path_obj.name)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        disp = "attachment" if download else "inline"
        self.send_header("Content-Disposition", f"{disp}; filename=\"{fallback_name}\"; filename*=UTF-8''{quoted_name}")
        self.end_headers()
        with open(path_obj, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            raise ValueError("请求格式不正确。")

    def session(self):
        return session_from_header(self.headers.get("Cookie"))

    def need(self, roles):
        sess = self.session()
        if not sess or sess["role"] not in roles:
            self.send_json(401, {"message": "请先完成验证后再访问。"})
            return None
        return sess

    def auth_cookie(self, sid):
        return f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={COOKIE_AGE}"

    def clear_cookie(self):
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            sess = self.session()
            self.send_html(200, page(sess["role"] if sess else None))
            return

        if path == "/healthz":
            self.send_html(200, "ok")
            return

        if path == "/api/status":
            if not self.need({"readonly", "full"}):
                return
            self.send_json(200, status_payload())
            return

        if path == "/api/files":
            if not self.need({"full"}):
                return
            try:
                rel = query.get("path", [""])[0]
                search_text = query.get("q", [""])[0]
                self.send_json(200, list_files(rel, search_text))
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/preview":
            if not self.need({"full"}):
                return
            try:
                self.send_json(200, preview_payload(query.get("path", [""])[0]))
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/password-info":
            if not self.need({"full"}):
                return
            self.send_json(200, password_info())
            return

        if path == "/download":
            if not self.need({"full"}):
                return
            try:
                target = safe_path(query.get("path", [""])[0])
            except Exception:
                self.send_json(400, {"message": "非法路径。"})
                return
            if not target.exists() or not target.is_file():
                self.send_json(404, {"message": "文件不存在。"})
                return
            self.send_file(target, download=True)
            return

        if path == "/view":
            if not self.need({"full"}):
                return
            try:
                target = safe_path(query.get("path", [""])[0])
            except Exception:
                self.send_json(400, {"message": "非法路径。"})
                return
            if not target.exists() or not target.is_file():
                self.send_json(404, {"message": "文件不存在。"})
                return
            if preview_kind(target) != "image":
                self.send_json(400, {"message": "当前文件不支持图片预览。"})
                return
            self.send_file(target, download=False)
            return

        if path == "/api/report-history":
            if not self.need({"full"}):
                return
            records = load_report_records()
            self.send_json(200, {"records": records})
            return

        self.send_json(404, {"message": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/login":
            allowed, retry_after = consume_login_slot(self.client_ip())
            if not allowed:
                self.send_json(429, {"message": f"请求过于频繁，请 {retry_after} 秒后再试。", "retry_after": retry_after})
                return
            try:
                data = self.read_json()
                password = str(data.get("password", "")).strip()
            except Exception as exc:
                record_login_result(self.client_ip(), False)
                self.send_json(400, {"message": str(exc)})
                return

            role = password_role(password)
            if not role:
                record_login_result(self.client_ip(), False)
                self.send_json(401, {"message": "访问密码错误。"})
                return

            record_login_result(self.client_ip(), True)
            sid = create_session(role)
            self.send_json(200, {"message": "验证成功。", "role": role}, self.auth_cookie(sid))
            return

        if path == "/api/logout":
            sess = self.session()
            if sess:
                with LOCK:
                    SESSIONS.pop(sess["id"], None)
            self.send_json(200, {"message": "已退出登录。"}, self.clear_cookie())
            return

        if path == "/api/passwords":
            if not self.need({"full"}):
                return
            try:
                data = self.read_json()
                result = update_passwords(data.get("current_password", ""), data.get("full_password", ""), data.get("readonly_password", ""))
                self.send_json(200, result)
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/mkdir":
            if not self.need({"full"}):
                return
            try:
                data = self.read_json()
                name = str(data.get("name", "")).strip()
                folder = safe_path(data.get("path", ""))
                if not name or "/" in name or "\\" in name or name in {".", ".."}:
                    raise ValueError("文件夹名称不合法。")
                target = folder / name
                if target.exists():
                    raise ValueError("同名文件或目录已存在。")
                target.mkdir()
                self.send_json(200, {"message": f"已创建文件夹：{name}"})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/delete":
            if not self.need({"full"}):
                return
            try:
                data = self.read_json()
                paths = data.get("paths")
                deleted = delete_targets(paths) if isinstance(paths, list) else delete_targets([data.get("path", "")])
                self.send_json(200, {"message": "已删除：" + "、".join(deleted)})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/upload":
            if not self.need({"full"}):
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > MAX_UPLOAD:
                self.send_json(413, {"message": f"上传过大，最大支持 {fmt_bytes(MAX_UPLOAD)}。"})
                return
            try:
                folder = safe_path(query.get("path", [""])[0])
                env = {
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                }
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env, keep_blank_values=True)
                names = []
                for item in form.list or []:
                    if not getattr(item, "filename", None):
                        continue
                    name = os.path.basename(item.filename)
                    if not name:
                        continue
                    target = unique_target(folder, name)
                    with open(target, "wb") as f:
                        shutil.copyfileobj(item.file, f)
                    names.append(target.name)
                if not names:
                    raise ValueError("没有检测到可上传的文件。")
                self.send_json(200, {"message": "上传成功：" + "、".join(names)})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/tailscale":
            if not self.need({"full"}):
                return
            try:
                data = self.read_json()
                action = str(data.get("action", "")).strip().lower()
                tailscale = set_tailscale_service(action)
                verb = "启动" if action == "start" else "停止"
                self.send_json(200, {"message": f"Tailscale 已{verb}。", "tailscale": tailscale})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return


        if path == "/api/xray":
            if not self.need({"full"}):
                return
            try:
                data = self.read_json()
                action = str(data.get("action", "")).strip().lower()
                xray = set_xray_service(action)
                labels = {"start": "启动", "stop": "停止", "restart": "重启"}
                verb = labels.get(action, action)
                self.send_json(200, {"message": f"Xray 已{verb}。", "xray": xray})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        if path == "/api/report":
            if not self.need({"full"}):
                return
            try:
                data = self.read_json()
                report_date = str(data.get("date", "")).strip() or None
                record = {
                    "id": secrets.token_hex(8),
                    "time": fmt_dt(cn_now()),
                    "type": "手动",
                    "date": report_date or cn_now().strftime("%Y-%m-%d"),
                    "content": "",
                    "status": "pending",
                    "detail": "",
                }
                record_id = save_report_record(record)
                import subprocess as _sp
                try:
                    args = ["/home/ubuntu/daily_report/venv/bin/python", "/home/ubuntu/daily_report/submit_for_date.py"]
                    if report_date:
                        args.append(report_date)
                    proc = _sp.run(args, capture_output=True, text=True, timeout=300, cwd="/home/ubuntu/daily_report")
                    ok = proc.returncode == 0
                    combined_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
                    report_source = parse_report_source_from_output(combined_output)
                    smart_meta = parse_smart_doc_meta_from_output(combined_output)
                    detail_text = (combined_output.strip()[-1500:] or "submit_for_date.py failed")
                    record["status"] = "成功" if ok else "失败"
                    record["detail"] = detail_text
                    if not ok and "FAILURE_EMAIL_SENT" not in combined_output:
                        send_report_failure_fallback(
                            report_date,
                            detail_text or combined_output or "submit_for_date.py failed",
                            report_source,
                            smart_meta.get("smart_doc_status"),
                            smart_meta.get("smart_doc_error"),
                        )
                    for line in proc.stdout.split("\n") + proc.stderr.split("\n"):
                        if "Extracted:" in line:
                            record["content"] = line.split("Extracted:", 1)[1].strip().rstrip("...")
                            break
                except Exception as e:
                    record["status"] = "失败"
                    record["detail"] = str(e)
                    send_report_failure_fallback(report_date, str(e), "unknown", "unknown", None)
                records = load_report_records()
                replaced = False
                for idx, item in enumerate(records):
                    if item.get("id") == record_id:
                        records[idx] = record
                        replaced = True
                        break
                if not replaced:
                    records.append(record)
                with RECLOCK:
                    os.makedirs(STATE_DIR, exist_ok=True)
                    with open(REPORT_HISTORY_FILE, "w", encoding="utf-8") as f:
                        json.dump(records, f, ensure_ascii=False, indent=2)
                self.send_json(200, {"message": "请求已接收", "record": record})
            except Exception as exc:
                self.send_json(400, {"message": str(exc)})
            return

        self.send_json(404, {"message": "not found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s %s\n" % (now_local(), self.client_ip(), fmt % args))


def main():
    ensure_dirs()
    load_auth_config()
    capture_metric_sample(force=True)
    threading.Thread(target=traffic_worker, daemon=True).start()
    threading.Thread(target=metrics_worker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"listening on http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
