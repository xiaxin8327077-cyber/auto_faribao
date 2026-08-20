import hmac
import json
import logging
import os
import secrets
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.beijing_time import now as beijing_now
from src.portfolio_view import build_portfolio_payload


logger = logging.getLogger(__name__)

BASE_DATE = date(2026, 7, 1)
ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT_DIR / "data" / "nav_dashboard_state.json"
ACCESS_TOKEN_PATH = ROOT_DIR / "data" / "nav_dashboard_access_token"
PAGE_PATH = Path(__file__).with_name("nav_dashboard_page.html")
MANUAL_COOLDOWN_SECONDS = 300
AUTO_REFRESH_SECONDS = 3600

_STATE_LOCK = threading.RLock()
_REFRESH_LOCK = threading.Lock()
_REFRESHING = False


def _empty_state() -> dict:
    return {
        "version": 1,
        "base_date": BASE_DATE.isoformat(),
        "initialized": False,
        "products": {},
        "events": [],
        "profit_entries": [],
        "manual_period_profits": [],
        "manual_daily_profits": {},
        "snapshots": {},
        "last_attempt_at": "",
        "last_success_at": "",
        "last_error": "",
        "manual_cooldown_until": "",
    }


def _key(provider: str, code: str) -> str:
    return f"{provider}:{code.upper()}"


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value) -> str:
    number = _decimal(value)
    if number is None:
        return ""
    return format(number, "f")


def _dt_text(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse_dt(value: str):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _record_dict(record) -> dict:
    if record is None:
        return {}
    return {
        "provider": record.provider,
        "code": record.code,
        "name": record.name,
        "nav_date": record.nav_date.isoformat(),
        "unit_nav": _decimal_text(record.unit_nav),
        "cumulative_nav": _decimal_text(record.cumulative_nav),
        "source": record.source,
    }


def _product_rows(cfg) -> dict:
    rows = {}
    for item in getattr(getattr(cfg, "nav_monitor", None), "products", []):
        provider = str(getattr(item, "provider", "") or "")
        code = str(getattr(item, "code", "") or "").upper()
        if not provider or not code:
            continue
        rows[_key(provider, code)] = {
            "provider": provider,
            "code": code,
            "name": str(getattr(item, "name", "") or code),
            "shares": _decimal_text(getattr(item, "shares", None)),
        }
    return rows


class NavDashboardStore:
    def __init__(self, state_path=DEFAULT_STATE_PATH):
        self.state_path = Path(state_path)
        self.state = self.load()

    def load(self) -> dict:
        with _STATE_LOCK:
            if not self.state_path.exists():
                return _empty_state()
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("dashboard state is not an object")
            except Exception:
                corrupt = self.state_path.with_name(
                    f"{self.state_path.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}"
                )
                try:
                    os.replace(self.state_path, corrupt)
                except OSError:
                    logger.warning("Failed to preserve corrupt dashboard state", exc_info=True)
                logger.error("NAV dashboard state was corrupt and has been reset", exc_info=True)
                return _empty_state()
            defaults = _empty_state()
            for key, value in defaults.items():
                data.setdefault(key, value)
            return data

    def save(self) -> None:
        with _STATE_LOCK:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            temp_path.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temp_path, self.state_path)
            try:
                os.chmod(self.state_path, 0o600)
            except OSError:
                pass

    def set_manual_monthly_profits(self, items: list, updated_at: datetime = None) -> dict:
        updated_at = updated_at or beijing_now().replace(tzinfo=None)
        with _STATE_LOCK:
            existing = {
                str(item.get("period") or ""): dict(item)
                for item in self.state.get("manual_period_profits", [])
                if item.get("period")
            }
            for item in items:
                period = str(item.get("period") or "")
                try:
                    period_start = date.fromisoformat(f"{period}-01")
                except ValueError as exc:
                    raise ValueError(f"invalid monthly profit period: {period}") from exc
                if len(period) != 7:
                    raise ValueError(f"invalid monthly profit period: {period}")
                amount = _decimal(item.get("amount"))
                if amount is None:
                    raise ValueError(f"invalid monthly profit amount for {period}")
                existing[period] = {
                    "period": period,
                    "amount": _decimal_text(amount),
                    "updated_at": _dt_text(updated_at),
                }
                current_base = date.fromisoformat(
                    self.state.get("base_date", BASE_DATE.isoformat())
                )
                if period_start < current_base:
                    self.state["base_date"] = period_start.isoformat()
            self.state["manual_period_profits"] = [
                existing[period] for period in sorted(existing)
            ]
            self.save()
            return self.state

    def set_manual_daily_profit(self, target_date: date, amount, updated_at: datetime = None) -> dict:
        """Manually override the displayed profit for a specific date (dashboard only)."""
        updated_at = updated_at or beijing_now().replace(tzinfo=None)
        date_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)
        parsed_amount = _decimal(amount)
        if parsed_amount is None:
            raise ValueError(f"invalid daily profit amount: {amount}")
        entries = self.state.get("profit_entries", [])
        max_nav_date = max((str(e.get("nav_date") or "") for e in entries), default="")
        if not max_nav_date:
            raise ValueError("看板尚无收益数据，无法设置")
        if date_str != max_nav_date:
            raise ValueError(
                f"仅可修改看板最新收益日期（{max_nav_date}），"
                f"当前输入日期为 {date_str}"
            )
        with _STATE_LOCK:
            manual_daily = self.state.setdefault("manual_daily_profits", {})
            manual_daily[date_str] = {
                "amount": _decimal_text(parsed_amount),
                "updated_at": _dt_text(updated_at),
            }
            self.save()
            return self.state

    def _append_event(self, event_type: str, product_key: str, changed_at: datetime, **extra):
        self.state["events"].append(
            {
                "type": event_type,
                "product_key": product_key,
                "changed_at": _dt_text(changed_at),
                **extra,
            }
        )

    def _sync_portfolio(self, cfg, changed_at: datetime, initial: bool = False):
        current = _product_rows(cfg)
        stored = self.state["products"]
        effective_date = BASE_DATE if initial else changed_at.date()

        for product_key, item in current.items():
            meta = stored.get(product_key)
            if meta is None:
                meta = {
                    **item,
                    "active": True,
                    "added_at": (
                        datetime.combine(BASE_DATE, datetime.min.time()).isoformat(timespec="seconds")
                        if initial
                        else _dt_text(changed_at)
                    ),
                    "removed_at": "",
                    "share_events": [],
                }
                stored[product_key] = meta
                self._append_event(
                    "product_added",
                    product_key,
                    changed_at,
                    effective_date=effective_date.isoformat(),
                    initial=initial,
                )
                if item["shares"]:
                    meta["share_events"].append(
                        {
                            "changed_at": _dt_text(changed_at),
                            "effective_date": effective_date.isoformat(),
                            "shares": item["shares"],
                            "include_start": initial,
                        }
                    )
            else:
                if not meta.get("active", True):
                    meta["active"] = True
                    meta["removed_at"] = ""
                    self._append_event(
                        "product_added",
                        product_key,
                        changed_at,
                        effective_date=changed_at.date().isoformat(),
                        initial=False,
                    )
                old_shares = str(meta.get("shares") or "")
                if old_shares != item["shares"]:
                    meta.setdefault("share_events", []).append(
                        {
                            "changed_at": _dt_text(changed_at),
                            "effective_date": changed_at.date().isoformat(),
                            "shares": item["shares"],
                            "include_start": False,
                        }
                    )
                    self._append_event(
                        "shares_changed",
                        product_key,
                        changed_at,
                        old_shares=old_shares,
                        new_shares=item["shares"],
                    )
                meta.update(item)

        for product_key, meta in stored.items():
            if product_key not in current and meta.get("active", True):
                meta["active"] = False
                meta["removed_at"] = _dt_text(changed_at)
                self._append_event("product_removed", product_key, changed_at)

    def sync_portfolio(self, cfg, changed_at: datetime = None) -> dict:
        changed_at = changed_at or beijing_now().replace(tzinfo=None)
        with _STATE_LOCK:
            self._sync_portfolio(cfg, changed_at, initial=False)
            self.save()
            return self.state

    def _nav_date_is_eligible(self, meta: dict, nav_date: date) -> bool:
        for event in meta.get("share_events", []):
            try:
                effective = date.fromisoformat(event["effective_date"])
            except (KeyError, ValueError):
                continue
            return nav_date > effective or (
                nav_date == effective and bool(event.get("include_start"))
            )
        return False

    def _shares_for_observation(self, meta: dict, observed_at: datetime):
        selected = None
        selected_at = None
        for event in meta.get("share_events", []):
            changed_at = _parse_dt(str(event.get("changed_at") or ""))
            if changed_at is None:
                logger.warning("Ignoring dashboard share event with invalid changed_at")
                continue
            if changed_at <= observed_at and (
                selected_at is None or changed_at >= selected_at
            ):
                selected = event
                selected_at = changed_at
        return _decimal(selected.get("shares")) if selected else None

    def _reconcile_profit_entries(self) -> int:
        corrected = 0
        for entry in self.state.get("profit_entries", []):
            discovered_at = _parse_dt(str(entry.get("discovered_at") or ""))
            if discovered_at is None:
                logger.warning(
                    "Skipping dashboard profit entry %s with invalid discovered_at",
                    entry.get("id", ""),
                )
                continue
            meta = self.state.get("products", {}).get(entry.get("product_key"))
            if not meta:
                continue
            shares = self._shares_for_observation(meta, discovered_at)
            delta = _decimal(entry.get("delta"))
            if shares is None or delta is None:
                continue
            amount = delta * shares
            if (
                _decimal(entry.get("shares")) == shares
                and _decimal(entry.get("amount")) == amount
            ):
                continue
            entry["shares"] = _decimal_text(shares)
            entry["amount"] = _decimal_text(amount)
            corrected += 1
        return corrected

    def reconcile_profit_entries(self) -> int:
        with _STATE_LOCK:
            corrected = self._reconcile_profit_entries()
            if corrected:
                self.save()
            return corrected

    def _entry_exists(self, product_key: str, nav_date: date) -> bool:
        entry_id = f"{product_key}:{nav_date.isoformat()}"
        return any(item.get("id") == entry_id for item in self.state["profit_entries"])

    def _record_pair(self, product_key: str, previous, latest, discovered_at: datetime):
        meta = self.state["products"].get(product_key)
        if not meta or not previous or not latest or latest.nav_date <= previous.nav_date:
            return
        if not self._nav_date_is_eligible(meta, latest.nav_date):
            return
        if self._entry_exists(product_key, latest.nav_date):
            return
        shares = self._shares_for_observation(meta, discovered_at)
        if shares is None:
            return
        delta = latest.unit_nav - previous.unit_nav
        amount = delta * shares
        self.state["profit_entries"].append(
            {
                "id": f"{product_key}:{latest.nav_date.isoformat()}",
                "product_key": product_key,
                "provider": latest.provider,
                "code": latest.code,
                "name": latest.name,
                "nav_date": latest.nav_date.isoformat(),
                "discovered_at": _dt_text(discovered_at),
                "discovered_date": discovered_at.date().isoformat(),
                "previous_nav": _decimal_text(previous.unit_nav),
                "unit_nav": _decimal_text(latest.unit_nav),
                "delta": _decimal_text(delta),
                "shares": _decimal_text(shares),
                "amount": _decimal_text(amount),
            }
        )

    def _record_product_history(self, product_key: str, records: list, discovered_at: datetime):
        ordered = sorted(
            {item.nav_date: item for item in records if item is not None}.values(),
            key=lambda item: item.nav_date,
        )
        for previous, latest in zip(ordered, ordered[1:]):
            self._record_pair(product_key, previous, latest, discovered_at)
        if ordered:
            self.state["snapshots"][product_key] = {
                "latest": _record_dict(ordered[-1]),
                "previous": _record_dict(ordered[-2] if len(ordered) > 1 else None),
                "error": "",
            }

    def initialize_from_history(self, cfg, histories: dict, discovered_at: datetime = None) -> dict:
        discovered_at = discovered_at or beijing_now().replace(tzinfo=None)
        with _STATE_LOCK:
            if self.state.get("initialized"):
                return self.record_history(cfg, histories, discovered_at)
            self._sync_portfolio(cfg, discovered_at, initial=True)
            for product_key, records in histories.items():
                self._record_product_history(product_key, records, discovered_at)
            self.state["initialized"] = True
            self.state["last_attempt_at"] = _dt_text(discovered_at)
            self.state["last_success_at"] = _dt_text(discovered_at)
            self.state["last_error"] = ""
            self.save()
            return self.state

    def record_history(self, cfg, histories: dict, discovered_at: datetime = None) -> dict:
        discovered_at = discovered_at or beijing_now().replace(tzinfo=None)
        with _STATE_LOCK:
            self._sync_portfolio(cfg, discovered_at, initial=False)
            for product_key, records in histories.items():
                self._record_product_history(product_key, records, discovered_at)
            self._reconcile_profit_entries()
            self.state["last_attempt_at"] = _dt_text(discovered_at)
            if histories:
                self.state["last_success_at"] = _dt_text(discovered_at)
                self.state["last_error"] = ""
            self.save()
            return self.state

    def record_results(self, cfg, results: list, discovered_at: datetime = None) -> dict:
        discovered_at = discovered_at or beijing_now().replace(tzinfo=None)
        with _STATE_LOCK:
            self._sync_portfolio(cfg, discovered_at, initial=not self.state.get("initialized"))
            successes = 0
            errors = []
            for result in results:
                product_key = _key(result.product.provider, result.product.code)
                if result.latest:
                    successes += 1
                    self._record_pair(product_key, result.previous, result.latest, discovered_at)
                    self.state["snapshots"][product_key] = {
                        "latest": _record_dict(result.latest),
                        "previous": _record_dict(result.previous),
                        "error": result.error or "",
                    }
                elif result.error:
                    errors.append(f"{result.product.code}: {result.error}")
                    previous_snapshot = self.state["snapshots"].setdefault(product_key, {})
                    previous_snapshot["error"] = result.error
            self._reconcile_profit_entries()
            self.state["initialized"] = True
            self.state["last_attempt_at"] = _dt_text(discovered_at)
            if successes:
                self.state["last_success_at"] = _dt_text(discovered_at)
            self.state["last_error"] = "; ".join(errors)
            self.save()
            return self.state

    def payload(self, cfg=None, now: datetime = None) -> dict:
        now = now or beijing_now().replace(tzinfo=None)
        with _STATE_LOCK:
            entries = list(self.state.get("profit_entries", []))
            manual_entries = list(self.state.get("manual_period_profits", []))
            total_profit = sum(
                (_decimal(item.get("amount")) or Decimal("0"))
                for item in entries + manual_entries
            )
            daily_totals = {}
            monthly_totals = {}
            yearly_totals = {}
            for item in manual_entries:
                period = str(item.get("period") or "")
                if len(period) != 7:
                    continue
                amount = _decimal(item.get("amount")) or Decimal("0")
                monthly_totals[period] = monthly_totals.get(period, Decimal("0")) + amount
                year = period[:4]
                yearly_totals[year] = yearly_totals.get(year, Decimal("0")) + amount
            for item in entries:
                nav_date = str(item.get("nav_date") or "")
                if not nav_date:
                    continue
                amount = _decimal(item.get("amount")) or Decimal("0")
                daily_totals[nav_date] = daily_totals.get(nav_date, Decimal("0")) + amount
                month = nav_date[:7]
                year = nav_date[:4]
                monthly_totals[month] = monthly_totals.get(month, Decimal("0")) + amount
                yearly_totals[year] = yearly_totals.get(year, Decimal("0")) + amount

            # Apply manual daily profit overrides (dashboard display only)
            manual_daily = self.state.get("manual_daily_profits", {})
            for nav_date, manual_item in manual_daily.items():
                manual_amount = _decimal(manual_item.get("amount") if isinstance(manual_item, dict) else manual_item)
                if manual_amount is None:
                    continue
                old_amount = daily_totals.get(nav_date, Decimal("0"))
                diff = manual_amount - old_amount
                daily_totals[nav_date] = manual_amount
                month = nav_date[:7]
                year = nav_date[:4]
                monthly_totals[month] = monthly_totals.get(month, Decimal("0")) + diff
                yearly_totals[year] = yearly_totals.get(year, Decimal("0")) + diff
                total_profit += diff

            latest_profit_date = max(daily_totals, default="")
            latest_profit = daily_totals.get(latest_profit_date, Decimal("0"))
            today_text = now.date().isoformat()
            today_entries = [item for item in entries if item.get("discovered_date") == today_text]
            today_profit = sum(
                (_decimal(item.get("amount")) or Decimal("0")) for item in today_entries
            )
            latest_entries = {}
            for item in entries:
                product_key = item.get("product_key", "")
                if not product_key:
                    continue
                previous = latest_entries.get(product_key)
                if previous is None or item.get("nav_date", "") > previous.get("nav_date", ""):
                    latest_entries[product_key] = item

            products = []
            market_value = Decimal("0")
            active_items = [
                (product_key, meta)
                for product_key, meta in self.state.get("products", {}).items()
                if meta.get("active", True)
            ]
            for product_key, meta in active_items:
                snapshot = self.state.get("snapshots", {}).get(product_key, {})
                latest = snapshot.get("latest") or {}
                previous = snapshot.get("previous") or {}
                shares = _decimal(meta.get("shares"))
                nav = _decimal(latest.get("unit_nav"))
                if shares is not None and nav is not None:
                    market_value += shares * nav
                delta = None
                change_pct = None
                if nav is not None:
                    previous_nav = _decimal(previous.get("unit_nav"))
                    if previous_nav is not None:
                        delta = nav - previous_nav
                        if previous_nav:
                            change_pct = delta / previous_nav * Decimal("100")
                latest_entry = latest_entries.get(product_key, {})
                products.append(
                    {
                        "provider": meta.get("provider", ""),
                        "code": meta.get("code", ""),
                        "name": meta.get("name", ""),
                        "shares": _decimal_text(shares),
                        "latest_nav": _decimal_text(nav),
                        "nav_date": latest.get("nav_date", ""),
                        "delta": _decimal_text(delta),
                        "change_pct": _decimal_text(change_pct),
                        "latest_profit": latest_entry.get("amount", ""),
                        "error": snapshot.get("error", ""),
                    }
                )

            ranking_source = [
                item for item in entries if item.get("nav_date") == latest_profit_date
            ]
            ranked = sorted(
                ranking_source,
                key=lambda item: _decimal(item.get("amount")) or Decimal("0"),
                reverse=True,
            )
            positive = [item for item in ranked if (_decimal(item.get("amount")) or 0) > 0][:3]
            negative = [item for item in reversed(ranked) if (_decimal(item.get("amount")) or 0) < 0][:3]
            current_month = now.strftime("%Y-%m")
            monthly_product_totals = {}
            monthly_product_rows = {}
            for item in entries:
                if not str(item.get("nav_date") or "").startswith(current_month):
                    continue
                product_key = str(item.get("product_key") or "")
                if not product_key:
                    continue
                monthly_product_totals[product_key] = monthly_product_totals.get(
                    product_key, Decimal("0")
                ) + (_decimal(item.get("amount")) or Decimal("0"))
                previous = monthly_product_rows.get(product_key)
                if previous is None or item.get("nav_date", "") > previous.get("nav_date", ""):
                    monthly_product_rows[product_key] = item
            monthly_ranked = sorted(
                (
                    {
                        **monthly_product_rows[product_key],
                        "amount": _decimal_text(amount),
                    }
                    for product_key, amount in monthly_product_totals.items()
                ),
                key=lambda item: _decimal(item.get("amount")) or Decimal("0"),
                reverse=True,
            )
            monthly_positive = [
                item for item in monthly_ranked if (_decimal(item.get("amount")) or 0) > 0
            ][:3]
            monthly_negative = [
                item
                for item in reversed(monthly_ranked)
                if (_decimal(item.get("amount")) or 0) < 0
            ][:3]
            return {
                "initialized": bool(self.state.get("initialized")),
                "base_date": self.state.get("base_date", BASE_DATE.isoformat()),
                "generated_at": _dt_text(now),
                "last_attempt_at": self.state.get("last_attempt_at", ""),
                "last_success_at": self.state.get("last_success_at", ""),
                "last_error": self.state.get("last_error", ""),
                "refreshing": _REFRESHING,
                "manual_cooldown_until": self.state.get("manual_cooldown_until", ""),
                "cumulative_profit": _decimal_text(total_profit),
                "today_profit": _decimal_text(today_profit),
                "latest_profit_date": latest_profit_date,
                "latest_profit": _decimal_text(latest_profit),
                "daily_profits": [
                    {"date": nav_date, "amount": _decimal_text(amount)}
                    for nav_date, amount in sorted(daily_totals.items(), reverse=True)
                ],
                "monthly_profits": [
                    {"period": period, "amount": _decimal_text(amount)}
                    for period, amount in sorted(monthly_totals.items(), reverse=True)
                ],
                "current_month_profit": _decimal_text(
                    monthly_totals.get(now.strftime("%Y-%m"), Decimal("0"))
                ),
                "yearly_profits": [
                    {"period": period, "amount": _decimal_text(amount)}
                    for period, amount in sorted(yearly_totals.items(), reverse=True)
                ],
                "market_value": _decimal_text(market_value),
                "configured_count": len(products),
                "shares_count": sum(1 for item in products if item["shares"]),
                "today_disclosed_count": len(
                    {item.get("product_key") for item in today_entries if item.get("product_key")}
                ),
                "products": products,
                "positive_rankings": positive,
                "negative_rankings": negative,
                "monthly_positive_rankings": monthly_positive,
                "monthly_negative_rankings": monthly_negative,
            }


def _get_portfolio_runtime():
    try:
        from src.portfolio_runtime import get_portfolio_runtime

        return get_portfolio_runtime()
    except (ImportError, RuntimeError):
        return None


def _merge_overview_products(
    legacy_products,
    ledger_products,
    ledger_positions=None,
):
    merged = []
    index_by_key = {}

    def product_key(row):
        provider = str(row.get("provider") or "").strip().lower()
        code = str(row.get("code") or "").strip().upper()
        return (provider, code) if provider and code else (
            "id",
            str(row.get("id") or row.get("product_id") or ""),
        )

    position_rows = (
        ledger_products
        if ledger_positions is None
        else ledger_positions
    )
    active_position_keys = {
        product_key(row)
        for row in position_rows or []
        if str(row.get("status") or "active").lower() == "active"
    }
    hidden_ledger_keys = {
        product_key(row)
        for row in ledger_products or []
        if (
            str(row.get("status") or "active").lower() != "active"
            or product_key(row) not in active_position_keys
        )
    }
    for row in legacy_products or []:
        item = dict(row)
        key = product_key(item)
        if key in hidden_ledger_keys:
            continue
        index_by_key[key] = len(merged)
        merged.append(item)

    for row in position_rows or []:
        if str(row.get("status") or "active").lower() != "active":
            continue
        quote = row.get("quote") if isinstance(row.get("quote"), dict) else {}
        ledger_item = {
            **row,
            "nav_date": row.get("nav_date") or quote.get("date") or "",
        }
        key = product_key(ledger_item)
        if key not in index_by_key:
            index_by_key[key] = len(merged)
            merged.append(ledger_item)
            continue
        target = merged[index_by_key[key]]
        for field, value in ledger_item.items():
            if field == "latest_profit" or value not in (None, ""):
                target[field] = value
    return merged


def get_dashboard_payload(cfg=None, state_path=DEFAULT_STATE_PATH) -> dict:
    payload = NavDashboardStore(state_path).payload(cfg)
    runtime = _get_portfolio_runtime()
    repository = getattr(runtime, "repository", None)
    if repository is None:
        if runtime is not None and getattr(runtime, "migration_error", ""):
            payload["write_enabled"] = False
            payload["write_disabled_reason"] = "portfolio_migration_failed"
        return payload

    write_enabled = bool(getattr(runtime, "write_enabled", False))
    portfolio = build_portfolio_payload(repository)
    portfolio["write_enabled"] = write_enabled
    if not write_enabled:
        portfolio["write_disabled_reason"] = "portfolio_migration_failed"
    # The overview still consumes the legacy root fields while management
    # tabs read the nested ledger projection. Use the position projection as
    # the authoritative holdings list so fully redeemed products disappear
    # from both views, while the product catalog still suppresses stale legacy
    # rows for known products that no longer have a position.
    payload["portfolio"] = portfolio
    payload["products"] = _merge_overview_products(
        payload.get("products", []),
        portfolio.get("products", []),
        portfolio.get("positions"),
    )
    payload["configured_count"] = len(payload["products"])
    payload["shares_count"] = sum(
        1 for item in payload["products"] if item.get("shares") not in (None, "")
    )
    ledger_market_value = portfolio.get("summary", {}).get("market_value")
    if ledger_market_value not in (None, ""):
        payload["market_value"] = ledger_market_value
    # 最新收益只汇总全局最新一次净值披露日期对应的产品，避免跨日期相加。
    dated_products = []
    for product in payload["products"]:
        quote = product.get("quote") if isinstance(product.get("quote"), dict) else {}
        disclosed_date = str(
            product.get("latest_profit_date")
            or product.get("nav_date")
            or quote.get("date")
            or ""
        )
        if disclosed_date and product.get("latest_profit") not in (None, ""):
            dated_products.append((disclosed_date, product))
    if dated_products:
        latest_profit_date = max(item[0] for item in dated_products)
        latest_profit = sum(
            (
                Decimal(str(product.get("latest_profit") or "0"))
                for disclosed_date, product in dated_products
                if disclosed_date == latest_profit_date
            ),
            Decimal("0"),
        )
        payload["latest_profit_date"] = latest_profit_date
        payload["latest_profit"] = str(latest_profit)
    # 每日/每月收益以组合账本为准：ledger 包含全量产品（财富+债券基金），
    # profit_entries 只包含从官网抓取的财富类产品，因此 ledger 优先覆盖。
    _apply_portfolio_profit_views(payload, repository, portfolio)

    # 累计收益 = daily_profits 总和（从 8 月开始，与理财看板一致）
    payload["cumulative_profit"] = str(sum(
        (Decimal(row["amount"]) for row in payload.get("daily_profits", [])),
        Decimal("0"),
    ))

    payload["write_enabled"] = write_enabled
    if not write_enabled:
        payload["write_disabled_reason"] = "portfolio_migration_failed"
    return payload


def _sum_product_amounts(daily_product_profits, days) -> dict:
    totals = {}
    for day in days:
        for product_id, amount in (daily_product_profits.get(day) or {}).items():
            totals[product_id] = totals.get(product_id, Decimal("0")) + amount
    return totals


def _product_breakdown_rows(amounts_by_id, products_by_id) -> list:
    rows = []
    for product_id, amount in amounts_by_id.items():
        if amount == 0:
            continue
        product = products_by_id.get(product_id)
        if product is None:
            continue
        rows.append(
            {
                "name": product.name,
                "code": product.code,
                "amount": format(amount, "f"),
            }
        )
    rows.sort(key=lambda item: Decimal(item["amount"]), reverse=True)
    return rows


def _portfolio_daily_product_profits(repository, as_of: date) -> dict:
    from collections import defaultdict

    from src.portfolio_profit import calculate_latest_profit

    daily = defaultdict(dict)
    for product in repository.list_products(active_only=True):
        quote_dates = sorted(
            {
                quote.quote_date
                for quote in repository.list_quotes(
                    product.id,
                    on_or_before=as_of,
                )
            }
        )
        for quote_date in quote_dates:
            profit_date, profit = calculate_latest_profit(
                repository,
                product,
                quote_date,
                bundle_non_trading_days=False,
            )
            if profit_date == quote_date and profit is not None:
                day = quote_date.isoformat()
                daily[day][product.id] = (
                    daily[day].get(product.id, Decimal("0")) + profit
                )
    return dict(daily)


def _portfolio_daily_profit_totals(repository, as_of: date) -> dict:
    """Rebuild per-day profits from ledger quotes / income (all products)."""
    return {
        day: sum(amounts.values(), Decimal("0"))
        for day, amounts in _portfolio_daily_product_profits(
            repository, as_of
        ).items()
    }


def _apply_portfolio_profit_views(payload, repository, portfolio) -> None:
    """Use ledger-based daily profits (all products), overriding partial profit_entries."""
    if not hasattr(repository, "list_products") or not hasattr(repository, "list_quotes"):
        return

    as_of_text = str(portfolio.get("summary", {}).get("as_of") or "")
    try:
        as_of = date.fromisoformat(as_of_text) if as_of_text else date.today()
    except ValueError:
        as_of = date.today()

    try:
        ledger_daily_products = _portfolio_daily_product_profits(repository, as_of)
    except Exception:
        logger.exception("Failed to rebuild dashboard daily profits from ledger")
        return

    ledger_daily = {
        day: sum(amounts.values(), Decimal("0"))
        for day, amounts in ledger_daily_products.items()
    }

    if not ledger_daily and not portfolio.get("profit_history"):
        return

    # profit_entries only covers wealth_nav products scraped from the website;
    # ledger covers ALL products. For overlapping dates, ledger wins.
    legacy_daily = {
        str(item.get("date") or ""): Decimal(str(item.get("amount") or "0"))
        for item in payload.get("daily_profits") or []
        if item.get("date")
    }
    merged_daily = dict(legacy_daily)
    for day, amount in ledger_daily.items():
        merged_daily[day] = amount

    # 只展示 8 月及之后的收益明细（历史已清零，从 8 月开始计算）
    CUTOFF_DATE = "2026-08-01"
    merged_daily = {
        day: amount for day, amount in merged_daily.items()
        if day >= CUTOFF_DATE
    }

    products_by_id = {
        product.id: product
        for product in repository.list_products(active_only=True)
    }

    def products_for(days):
        return _product_breakdown_rows(
            _sum_product_amounts(ledger_daily_products, days),
            products_by_id,
        )

    payload["daily_profits"] = [
        {
            "date": day,
            "amount": format(amount, "f"),
            "products": products_for([day]),
        }
        for day, amount in sorted(merged_daily.items(), reverse=True)
    ]

    # 只保留 8 月及之后的月度/年度汇总
    CUTOFF_MONTH = "2026-08"
    monthly_map = {}
    monthly_days = {}
    for day, amount in merged_daily.items():
        month = day[:7]
        if month < CUTOFF_MONTH:
            continue
        monthly_map[month] = monthly_map.get(month, Decimal("0")) + amount
        monthly_days.setdefault(month, []).append(day)

    payload["monthly_profits"] = [
        {
            "period": period,
            "amount": format(amount, "f"),
            "products": products_for(monthly_days[period]),
        }
        for period, amount in sorted(monthly_map.items(), reverse=True)
    ]

    yearly_map = {}
    yearly_days = {}
    for day in merged_daily:
        year = day[:4]
        yearly_days.setdefault(year, []).append(day)
    for period, amount in monthly_map.items():
        year = period[:4]
        yearly_map[year] = yearly_map.get(year, Decimal("0")) + amount
    payload["yearly_profits"] = [
        {
            "period": year,
            "amount": format(amount, "f"),
            "products": products_for(yearly_days[year]),
        }
        for year, amount in sorted(yearly_map.items(), reverse=True)
    ]

    current_month = as_of.strftime("%Y-%m")
    payload["current_month_profit"] = format(
        monthly_map.get(current_month, Decimal("0")), "f"
    )

    # 重写排名数据：使用组合账本计算每月贡献（与 daily_profits 同源）
    _apply_portfolio_rankings(payload, repository, portfolio, as_of)


def _apply_portfolio_rankings(payload, repository, portfolio, as_of) -> None:
    """用组合账本数据重写月度/每日排名，与 daily_profits 保持一致。"""
    from src.portfolio_profit import calculate_latest_profit

    current_month = as_of.strftime("%Y-%m")
    products = portfolio.get("products", [])
    month_row = next(
        (
            row
            for row in payload.get("monthly_profits") or []
            if row.get("period") == current_month
        ),
        None,
    )
    month_products = list(month_row.get("products") or []) if month_row else []
    payload["monthly_positive_rankings"] = [
        item for item in month_products if Decimal(item["amount"]) > 0
    ][:3]
    payload["monthly_negative_rankings"] = [
        item for item in reversed(month_products) if Decimal(item["amount"]) < 0
    ][:3]

    # 每日排名：使用最新一天的各产品收益
    latest_date_str = str(payload.get("latest_profit_date") or "")
    if latest_date_str:
        try:
            latest_date = date.fromisoformat(latest_date_str)
        except ValueError:
            latest_date = None
    else:
        latest_date = None

    if latest_date and latest_date >= date(2026, 8, 1):
        daily_profits_map = {}
        for product_row in products:
            pid = product_row.get("id") or product_row.get("product_id")
            if not pid:
                continue
            repo_product = repository.get_product(pid) if repository else None
            if not repo_product:
                continue
            pd, profit = calculate_latest_profit(repository, repo_product, latest_date)
            if pd == latest_date and profit is not None and profit > 0:
                daily_profits_map[pid] = {
                    "name": product_row.get("name", ""),
                    "code": product_row.get("code", ""),
                    "provider": product_row.get("provider", ""),
                    "amount": profit,
                }
        daily_ranked = sorted(daily_profits_map.values(), key=lambda x: x["amount"], reverse=True)
        payload["positive_rankings"] = daily_ranked[:3]
        payload["negative_rankings"] = []


def sync_dashboard_portfolio(cfg, changed_at=None, state_path=DEFAULT_STATE_PATH) -> dict:
    return NavDashboardStore(state_path).sync_portfolio(cfg, changed_at)


def _scheduled_enterprise_times(cfg, day: date) -> list[datetime]:
    nav = getattr(cfg, "nav_monitor", None)
    if nav is None:
        return []
    try:
        from src.workday_calendar import is_workday

        if not is_workday(day):
            return []
    except Exception:
        if day.weekday() >= 5:
            return []
    values = [
        (getattr(nav, "push_hour", 8), getattr(nav, "push_minute", 0)),
    ]
    if getattr(nav, "evening_push_enabled", True):
        values.append(
            (
                getattr(nav, "evening_push_hour", 23),
                getattr(nav, "evening_push_minute", 30),
            )
        )
    return [datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute) for hour, minute in values]


def should_auto_refresh(state: dict, now: datetime, cfg) -> bool:
    if _REFRESHING:
        return False
    last_success = _parse_dt(state.get("last_success_at", ""))
    if last_success and (now - last_success).total_seconds() < AUTO_REFRESH_SECONDS:
        return False
    for scheduled in _scheduled_enterprise_times(cfg, now.date()):
        seconds_until = (scheduled - now).total_seconds()
        if 0 <= seconds_until <= AUTO_REFRESH_SECONDS:
            return False
    return True


def _refresh_dashboard(cfg, state_path, attempted_at: datetime):
    global _REFRESHING
    store = NavDashboardStore(state_path)
    try:
        from src.nav_monitor import ProviderError, query_nav_histories

        histories, errors = query_nav_histories(
            cfg,
            start_date=BASE_DATE - timedelta(days=14),
            query_source="dashboard",
        )
        if store.state.get("initialized"):
            store.record_history(cfg, histories, attempted_at)
        else:
            store.initialize_from_history(cfg, histories, attempted_at)
        store.state["last_error"] = "; ".join(errors)
        if errors and not histories:
            store.state["last_success_at"] = store.state.get("last_success_at", "")
        store.save()
    except ProviderError as exc:
        store.state["last_attempt_at"] = _dt_text(attempted_at)
        store.state["last_error"] = str(exc)
        store.save()
    except Exception as exc:
        logger.error("NAV dashboard refresh failed: %s", exc, exc_info=True)
        store.state["last_attempt_at"] = _dt_text(attempted_at)
        store.state["last_error"] = str(exc)
        store.save()
    finally:
        with _REFRESH_LOCK:
            _REFRESHING = False


def _launch_refresh_thread(cfg, state_path, attempted_at: datetime) -> bool:
    global _REFRESHING
    with _REFRESH_LOCK:
        if _REFRESHING:
            return False
        _REFRESHING = True
    threading.Thread(
        target=_refresh_dashboard,
        args=(cfg, state_path, attempted_at),
        daemon=True,
        name="nav-dashboard-refresh",
    ).start()
    return True


def request_manual_refresh(cfg, now=None, state_path=DEFAULT_STATE_PATH) -> dict:
    now = now or beijing_now().replace(tzinfo=None)
    store = NavDashboardStore(state_path)
    cooldown_until = _parse_dt(store.state.get("manual_cooldown_until", ""))
    last_success = _parse_dt(store.state.get("last_success_at", ""))
    if last_success:
        recent_success_until = last_success + timedelta(seconds=MANUAL_COOLDOWN_SECONDS)
        if cooldown_until is None or recent_success_until > cooldown_until:
            cooldown_until = recent_success_until
    if cooldown_until and now < cooldown_until:
        return {
            "accepted": False,
            "status": "cooldown",
            "cooldown_until": _dt_text(cooldown_until),
        }

    from src.nav_monitor import nav_query_in_progress

    if nav_query_in_progress():
        return {"accepted": False, "status": "enterprise_query_running"}
    if _REFRESHING:
        return {"accepted": False, "status": "refreshing"}

    cooldown_until = now + timedelta(seconds=MANUAL_COOLDOWN_SECONDS)
    store.state["manual_cooldown_until"] = _dt_text(cooldown_until)
    store.save()
    accepted = _launch_refresh_thread(cfg, Path(state_path), now)
    return {
        "accepted": bool(accepted),
        "status": "refreshing" if accepted else "refreshing",
        "cooldown_until": _dt_text(cooldown_until),
    }


def request_auto_refresh(cfg, now=None, state_path=DEFAULT_STATE_PATH) -> dict:
    now = now or beijing_now().replace(tzinfo=None)
    store = NavDashboardStore(state_path)
    if not should_auto_refresh(store.state, now, cfg):
        return {"accepted": False, "status": "not_due"}
    from src.nav_monitor import nav_query_in_progress

    if nav_query_in_progress():
        return {"accepted": False, "status": "enterprise_query_running"}
    accepted = _launch_refresh_thread(cfg, Path(state_path), now)
    return {"accepted": bool(accepted), "status": "refreshing" if accepted else "refreshing"}


def observe_enterprise_results(
    cfg,
    results,
    observed_at=None,
    state_path=DEFAULT_STATE_PATH,
) -> None:
    state_path = Path(state_path)
    if not state_path.exists():
        return
    store = NavDashboardStore(state_path)
    if not store.state.get("initialized"):
        return
    store.record_results(
        cfg,
        results,
        observed_at or beijing_now().replace(tzinfo=None),
    )


def get_access_token(path=ACCESS_TOKEN_PATH) -> str:
    path = Path(path)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def request_is_authorized(flask_request) -> bool:
    supplied = flask_request.headers.get("X-Nav-Dashboard-Key", "")
    return bool(supplied) and hmac.compare_digest(supplied, get_access_token())


def render_dashboard_page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")
