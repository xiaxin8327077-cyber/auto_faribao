from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import threading
import time
from uuid import NAMESPACE_URL, uuid5

from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException

from src.beijing_time import now as beijing_now
from src.nav_monitor import ProviderError
from src.portfolio_market import validate_market_quote
from src.portfolio_confirmation import (
    confirmation_schedule,
    normalize_market_datetime,
)
from src.portfolio_models import (
    MarketProduct,
    Product,
    ProductStatus,
    ProductType,
    SipPlan,
    SipPlanStatus,
    TransactionType,
    decimal_text,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_profit import calculate_holding_profit, calculate_latest_profit
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService


_WRITE_LIMIT = 30
_WRITE_WINDOW_SECONDS = 60.0
_PRODUCT_QUOTE_LOOKBACK_DAYS = 14
_SENSITIVE_KEYS = {
    "authorization",
    "idempotency-key",
    "password",
    "private_token",
    "secret",
    "token",
    "x-nav-dashboard-key",
}


def _json_value(value):
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, (date,)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            key: _json_value(item)
            for key, item in asdict(value).items()
        }
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _safe_audit_value(value):
    if isinstance(value, dict):
        return {
            str(key): _safe_audit_value(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _SENSITIVE_KEYS
            and "token" not in str(key).strip().lower()
            and "secret" not in str(key).strip().lower()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_audit_value(item) for item in value]
    return _json_value(value)


def _error(code, message, status, **extra):
    payload = {"error": code, "message": message}
    payload.update(extra)
    return jsonify(payload), status


def _required_text(value, name):
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _parse_date(value, name, default=None):
    if value in (None, "") and default is not None:
        return default
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _parse_datetime(value, name):
    try:
        parsed = normalize_market_datetime(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date and time") from exc
    return parsed


def _decimal(value, name, *, positive=False, nonnegative=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    if positive and number <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _runtime_reason(runtime):
    if runtime is None:
        return "portfolio_runtime_unavailable"
    return (
        getattr(runtime, "migration_error", "")
        or "portfolio_runtime_unavailable"
    )


def _infer_provider(code, product_type):
    normalized = code.strip().upper()
    if product_type == ProductType.PUBLIC_FUND.value:
        return "eastmoney_fund"
    if normalized.startswith(("AF", "AM")):
        return "citic_wealth"
    if normalized.startswith("NY"):
        return "nanyin_wealth"
    raise ValueError("provider is required")


def _identity(product: MarketProduct):
    return {
        "provider": product.provider,
        "code": product.code,
        "name": product.name,
        "product_type": product.product_type.value,
        "registration_code": product.registration_code,
    }


def _identity_fingerprint(identity):
    return hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _today():
    return beijing_now().date()


def create_portfolio_blueprint(runtime, provider_factory) -> Blueprint:
    blueprint = Blueprint("portfolio_api", __name__)
    rate_buckets = defaultdict(deque)
    rate_lock = threading.Lock()

    @blueprint.errorhandler(Exception)
    def handle_portfolio_error(exc):
        if isinstance(exc, HTTPException):
            return _error(
                "http_error",
                exc.description,
                exc.code or 500,
            )
        return _error(
            "internal_error",
            "portfolio API request failed",
            500,
        )

    def repository():
        return getattr(runtime, "repository", None) if runtime is not None else None

    def services():
        repo = repository()
        if repo is None:
            raise RuntimeError("portfolio runtime is unavailable")
        projector = PositionProjector(repo)
        transactions = PortfolioTransactionService(repo, projector)
        sip = SipService(repo, projector, transactions)
        return repo, projector, transactions, sip

    def read_guard():
        from src.nav_dashboard import request_is_authorized

        if not request_is_authorized(request):
            return _error(
                "private_link_invalid",
                "private link is invalid",
                403,
            )
        if repository() is None:
            return _error(
                "portfolio_unavailable",
                "portfolio runtime is unavailable",
                503,
                reason=_runtime_reason(runtime),
            )
        return None

    def write_guard():
        from src.nav_dashboard import request_is_authorized

        if not request_is_authorized(request):
            return None, _error(
                "private_link_invalid",
                "private link is invalid",
                403,
            )
        if (
            runtime is None
            or not getattr(runtime, "write_enabled", False)
            or repository() is None
        ):
            return None, _error(
                "write_disabled",
                "portfolio writes are disabled",
                503,
                reason=_runtime_reason(runtime),
            )
        if not request.is_json:
            return None, _error(
                "json_required",
                "application/json is required",
                400,
            )
        if request.headers.get("X-Portfolio-Request") != "1":
            return None, _error(
                "portfolio_request_required",
                "X-Portfolio-Request must be 1",
                400,
            )
        idempotency_key = request.headers.get("Idempotency-Key", "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return None, _error(
                "idempotency_key_invalid",
                "Idempotency-Key must contain 1 to 128 characters",
                400,
            )

        supplied_token = request.headers.get("X-Nav-Dashboard-Key", "")
        token_hash = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
        source = request.remote_addr or ""
        now = time.monotonic()
        with rate_lock:
            bucket = rate_buckets[(token_hash, source)]
            cutoff = now - _WRITE_WINDOW_SECONDS
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= _WRITE_LIMIT:
                return None, _error(
                    "rate_limit_exceeded",
                    "write rate limit exceeded",
                    429,
                )
            bucket.append(now)
        return idempotency_key, None

    def json_body():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body

    def audit(
        action,
        object_type,
        object_id,
        body,
        result,
        idem,
        *,
        conn=None,
    ):
        repo = repository()
        if repo is None:
            raise RuntimeError("portfolio runtime is unavailable")
        audit_id = str(
            uuid5(
                NAMESPACE_URL,
                (
                    f"portfolio-api-business:{action}:{idem}"
                    if result == "success"
                    else (
                        f"portfolio-api-business:{action}:{idem}:"
                        f"{result}:{request_fingerprint(body)}"
                    )
                ),
            )
        )
        after = json.dumps(
            _safe_audit_value(body),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        before = "{}"

        def append(target):
            existing = target.execute(
                """SELECT action, object_type, object_id, before_json,
                          after_json, result, source
                   FROM audit_logs WHERE id = ?""",
                (audit_id,),
            ).fetchone()
            expected = (
                action,
                object_type,
                object_id or "",
                before,
                after,
                result,
                "web",
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise RuntimeError("audit record conflicts with request")
                return
            repo.append_audit(
                audit_id,
                action,
                object_type,
                object_id or "",
                before,
                after,
                result,
                "web",
                conn=target,
            )

        if conn is not None:
            append(conn)
            return
        with repo.database.transaction() as owned:
            append(owned)

    def request_fingerprint(body):
        return hashlib.sha256(
            json.dumps(
                _safe_audit_value(body),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def operation_id(idem):
        return str(
            uuid5(NAMESPACE_URL, f"portfolio-api-idempotency:{idem}")
        )

    def operation_record(action, idem, body, conn=None):
        repo = repository()
        if repo is None:
            return None
        query = (
            "SELECT object_type, before_json, after_json, result, source "
            "FROM audit_logs WHERE id = ? AND action = 'api_idempotency'"
        )
        if conn is None:
            with repo.database.connection() as owned:
                row = owned.execute(query, (operation_id(idem),)).fetchone()
        else:
            row = conn.execute(query, (operation_id(idem),)).fetchone()
        if row is None:
            return None
        try:
            before = json.loads(row["before_json"])
            stored = json.loads(row["after_json"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("idempotency record is corrupt") from exc
        if (
            row["object_type"] != action
            or row["source"] != "web"
            or before.get("fingerprint") != request_fingerprint(body)
        ):
            raise ValueError("idempotency key conflicts with existing request")
        if (
            row["result"] not in {"success", "rejected"}
            or not isinstance(stored, dict)
            or not isinstance(stored.get("status"), int)
            or not isinstance(stored.get("payload"), dict)
        ):
            raise RuntimeError("idempotency record is incomplete")
        return stored["payload"], stored["status"]

    def replay(action, idem, body, conn=None):
        stored = operation_record(action, idem, body, conn=conn)
        if stored is None:
            return None
        payload, status = stored
        return jsonify(payload), status

    def remember(action, idem, body, payload, status, *, result="success", conn=None):
        repo = repository()
        if repo is None:
            raise RuntimeError("portfolio runtime is unavailable")

        def persist(target):
            stored = operation_record(action, idem, body, conn=target)
            if stored is not None:
                stored_payload, stored_status = stored
                if stored_payload != payload or stored_status != status:
                    raise RuntimeError(
                        "idempotency response conflicts with stored response"
                    )
                return
            repo.append_audit(
                operation_id(idem),
                "api_idempotency",
                action,
                hashlib.sha256(idem.encode("utf-8")).hexdigest(),
                before_json=json.dumps(
                    {"fingerprint": request_fingerprint(body)},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                after_json=json.dumps(
                    {"payload": payload, "status": status},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                result=result,
                source="web",
                conn=target,
            )

        if conn is not None:
            persist(conn)
            return
        with repo.database.transaction() as owned:
            persist(owned)

    def business_error(
        action,
        object_type,
        object_id,
        body,
        exc,
        idem,
        *,
        code="validation_error",
        status=400,
        persist=True,
        operation_action=None,
    ):
        safe_body = {
            **_safe_audit_value(body),
            "error": str(exc)[:240],
        }
        payload = {"error": code, "message": str(exc)}
        try:
            if persist:
                operation_action = operation_action or action
                try:
                    cached = replay(operation_action, idem, body)
                except ValueError as replay_error:
                    if "idempotency key conflicts" not in str(replay_error):
                        raise
                    audit(
                        action,
                        object_type,
                        object_id,
                        safe_body,
                        "rejected",
                        idem,
                    )
                    return jsonify(payload), status
                if cached:
                    return cached
                with repository().database.transaction() as conn:
                    audit(
                        action,
                        object_type,
                        object_id,
                        safe_body,
                        "rejected",
                        idem,
                        conn=conn,
                    )
                    remember(
                        operation_action,
                        idem,
                        body,
                        payload,
                        status,
                        result="rejected",
                        conn=conn,
                    )
            else:
                audit(
                    action,
                    object_type,
                    object_id,
                    safe_body,
                    "rejected",
                    idem,
                )
        except Exception:
            return _error(
                "audit_failed",
                "failed to persist portfolio audit",
                500,
            )
        return jsonify(payload), status

    def product_preview(body, *, require_identity=False):
        operation = str(body.get("operation") or "").strip().lower()
        if operation == "disable":
            product_id = _required_text(body.get("product_id"), "product_id")
            product = repository().require_product(product_id)
            identity = {
                "provider": product.provider,
                "code": product.code,
                "name": product.name,
                "product_type": product.product_type.value,
                "registration_code": product.registration_code,
            }
            return {
                "operation": "disable",
                "product": identity,
                "identity_fingerprint": _identity_fingerprint(identity),
                "message": "product will be disabled",
            }, None, None
        code = _required_text(body.get("code"), "code")
        declared_type = (
            body.get("product_type") or body.get("wealth_type") or ""
        )
        if str(declared_type) == ProductType.CASH_MANAGEMENT.value:
            raise ValueError("系统仅支持钱包Plus现金产品")
        provider_name = (
            body.get("provider")
            or _infer_provider(code, str(declared_type))
        )
        provider = provider_factory(_required_text(provider_name, "provider"))
        resolved = provider.resolve_product(code)
        if not isinstance(resolved, MarketProduct):
            raise ValueError("provider returned an invalid product")
        if resolved.provider != provider_name:
            raise ValueError("provider returned a different provider")
        if resolved.code.strip().upper() != code.upper():
            raise ValueError("provider returned a different product code")
        # 用户填写的名称仅作参考，统一以行情机构解析的官方名称为准，
        # 由确认对话框向用户展示核对，不再强制逐字匹配。
        if declared_type and str(declared_type) != resolved.product_type.value:
            raise ValueError("resolved product type does not match request")
        identity = _identity(resolved)
        fingerprint = _identity_fingerprint(identity)
        if require_identity:
            supplied_identity = body.get("identity")
            supplied_fingerprint = _required_text(
                body.get("identity_fingerprint"),
                "identity_fingerprint",
            )
            supplied_registration = _required_text(
                body.get("registration_code")
                or (
                    supplied_identity.get("registration_code")
                    if isinstance(supplied_identity, dict)
                    else ""
                ),
                "registration_code",
            )
            if (
                not isinstance(supplied_identity, dict)
                or supplied_identity != identity
                or supplied_fingerprint != fingerprint
                or supplied_registration != resolved.registration_code
            ):
                raise ValueError("resolved product identity changed")

        end_date = _today()
        start_date = end_date - timedelta(days=_PRODUCT_QUOTE_LOOKBACK_DAYS)
        quotes = provider.fetch_quotes(resolved, start_date, end_date)
        valid_quotes = []
        for quote in quotes or []:
            if quote.product_code.strip().upper() != resolved.code.upper():
                raise ValueError("provider returned a quote for another product")
            try:
                validate_market_quote(resolved.product_type, quote)
            except ValueError:
                continue
            if start_date <= quote.quote_date <= end_date:
                valid_quotes.append(quote)
        if not valid_quotes:
            raise ValueError("provider returned no valid quote")
        quote = max(valid_quotes, key=lambda item: item.quote_date)
        return {
            "product": identity,
            "identity_fingerprint": fingerprint,
            "quote": _json_value(quote),
            "message": "product identity and latest quote verified",
        }, resolved, quote

    def transaction_preview(body):
        repo, projector, _, _ = services()
        operation = str(body.get("operation") or "").strip().lower()
        if operation in {"cancel", "reverse"}:
            transaction_id = _required_text(
                body.get("transaction_id"), "transaction_id"
            )
            transaction = repo.get_transaction_by_id(transaction_id)
            if transaction is None:
                raise ValueError("transaction not found")
            reason = str(body.get("reason") or body.get("note") or "").strip()
            if operation == "reverse":
                reason = _required_text(reason, "reason")
            return {
                "normalized_input": {
                    "operation": operation,
                    "transaction_id": transaction_id,
                    "reason": reason,
                },
                "source_impact": "existing_transaction",
                "destination_impact": operation,
                "fee": "0",
                "status_prediction": (
                    "cancelled" if operation == "cancel" else "reversed"
                ),
                "warning": "This operation changes immutable ledger state.",
            }

        kind = str(
            body.get("kind") or body.get("transaction_type") or ""
        ).strip().lower()
        if kind not in {"purchase", "redemption"}:
            raise ValueError("kind must be purchase or redemption")
        product_id = _required_text(body.get("product_id"), "product_id")
        product = repo.require_product(product_id)
        trade_time_value = str(body.get("trade_time") or "").strip()
        settlement_date = _parse_date(body.get("settlement_date"), "settlement_date", None)
        transaction_type = (
            TransactionType.MANUAL_PURCHASE
            if kind == "purchase"
            else TransactionType.MANUAL_REDEMPTION
        )
        schedule = None
        if trade_time_value:
            submitted_at = _parse_datetime(trade_time_value, "trade_time")
            schedule = confirmation_schedule(
                product,
                transaction_type,
                submitted_at,
            )
            trade_date = schedule.trade_date
            normalized_trade_time = submitted_at.isoformat(timespec="seconds")
        else:
            trade_date = _parse_date(
                body.get("trade_date"), "trade_date", date.today()
            )
            normalized_trade_time = ""
        quote = repo.get_quote(product.id, trade_date)
        nav = (
            Decimal("1")
            if product.product_type is ProductType.CASH_MANAGEMENT
            else (quote.unit_nav if quote is not None else None)
        )
        status = (
            "confirmed"
            if nav is not None
            and (
                schedule is None
                or product.product_type is ProductType.CASH_MANAGEMENT
            )
            else "pending_confirmation"
            if nav is not None
            else "pending_quote"
        )

        if kind == "purchase":
            amount = _decimal(body.get("amount"), "amount", positive=True)
            fee_rate = _decimal(
                body.get("fee_rate", "0"),
                "fee_rate",
                nonnegative=True,
            )
            if fee_rate >= 1:
                raise ValueError("fee_rate must be between 0 and 1")
            source_id = str(
                body.get("source_cash_product_id")
                or body.get("source")
                or ""
            ).strip()
            if source_id:
                source = repo.require_product(source_id)
                if source.product_type is not ProductType.CASH_MANAGEMENT:
                    raise ValueError("source must be cash_management")
                if projector.calculate(source_id).available_shares < amount:
                    raise ValueError("insufficient available shares")
            fee = amount * fee_rate if nav is not None else None
            shares = None
            if nav is not None:
                shares = (
                    amount
                    if product.product_type is ProductType.CASH_MANAGEMENT
                    else (amount - fee) / nav
                )
            normalized = {
                "amount": decimal_text(amount),
                "fee_rate": decimal_text(fee_rate),
                "kind": kind,
                "product_id": product_id,
                "source_cash_product_id": source_id,
                "trade_date": trade_date.isoformat(),
                "note": str(body.get("note") or ""),
            }
            if schedule is not None:
                normalized.update(
                    {
                        "trade_time": normalized_trade_time,
                        "expected_confirmation_date": (
                            schedule.confirmation_date.isoformat()
                        ),
                    }
                )
            if settlement_date is not None:
                normalized["settlement_date"] = settlement_date.isoformat()
            return {
                "normalized_input": normalized,
                "source_impact": (
                    f"{source_id}:-{decimal_text(amount)}"
                    if source_id
                    else f"external_cash:-{decimal_text(amount)}"
                ),
                "destination_impact": (
                    f"{product_id}:+{decimal_text(shares)}"
                    if shares is not None
                    else f"{product_id}:pending_quote"
                ),
                "fee": decimal_text(fee) if fee is not None else None,
                "status_prediction": status,
                "warning": (
                    ""
                    if status == "confirmed"
                    else "The trade will remain pending until a quote arrives."
                    if status == "pending_quote"
                    else (
                        "The trade will confirm on "
                        f"{schedule.confirmation_date.isoformat()}."
                    )
                ),
            }

        shares = _decimal(
            body.get("shares", body.get("amount")),
            "shares",
            positive=True,
        )
        position = projector.calculate(product_id)
        if position.available_shares < shares:
            raise ValueError("insufficient available shares")
        destination_id = str(
            body.get("destination_cash_product_id")
            or body.get("destination")
            or ""
        ).strip()
        if destination_id:
            destination = repo.require_product(destination_id)
            if destination.product_type is not ProductType.CASH_MANAGEMENT:
                raise ValueError("destination must be cash_management")
        amount = shares * nav if nav is not None else None
        normalized = {
            "destination_cash_product_id": destination_id,
            "kind": kind,
            "product_id": product_id,
            "shares": decimal_text(shares),
            "trade_date": trade_date.isoformat(),
            "note": str(body.get("note") or ""),
        }
        if schedule is not None:
            normalized.update(
                {
                    "trade_time": normalized_trade_time,
                    "expected_confirmation_date": (
                        schedule.confirmation_date.isoformat()
                    ),
                }
            )
        return {
            "normalized_input": normalized,
            "source_impact": f"{product_id}:-{decimal_text(shares)}",
            "destination_impact": (
                f"{destination_id or 'external_cash'}:+{decimal_text(amount)}"
                if amount is not None
                else f"{destination_id or 'external_cash'}:pending_quote"
            ),
            "fee": "0",
            "status_prediction": status,
            "warning": (
                ""
                if status == "confirmed"
                else "The trade will remain pending until a quote arrives."
                if status == "pending_quote"
                else (
                    "The trade will confirm on "
                    f"{schedule.confirmation_date.isoformat()}."
                )
            ),
        }

    def require_executable_sip_products(
        repo,
        product_id,
        source_id,
        *,
        conn=None,
    ):
        target = repo.require_product(product_id, conn=conn)
        if target.product_type is not ProductType.PUBLIC_FUND:
            raise ValueError("target must be public_fund")
        if target.status is not ProductStatus.ACTIVE:
            raise ValueError("target product is not active")
        if not source_id:
            raise ValueError(
                "active SIP plan requires a cash management source"
            )
        source = repo.require_product(source_id, conn=conn)
        if source.product_type is not ProductType.CASH_MANAGEMENT:
            raise ValueError("source must be cash_management")
        if source.status is not ProductStatus.ACTIVE:
            raise ValueError("source product is not active")

    def sip_preview(body):
        repo, _, _, _ = services()
        operation = str(body.get("operation") or "").strip().lower()
        if operation in {"pause", "resume", "activate", "delete"}:
            plan_id = _required_text(
                body.get("sip_id") or body.get("plan_id"), "sip_id"
            )
            plan = repo.get_plan(plan_id)
            if plan is None:
                raise ValueError("plan not found")
            if operation in {"resume", "activate"}:
                require_executable_sip_products(
                    repo,
                    plan.product_id,
                    plan.source_cash_product_id,
                )
            return {
                "operation": operation,
                "sip_id": plan_id,
                "message": f"SIP plan will be {operation}d",
            }
        product_id = str(body.get("product_id") or "").strip()
        code = str(body.get("code") or "").strip().upper()
        if not product_id and code:
            from src.portfolio_providers import FUND_IDENTITIES, ChangshengFundProvider
            if code not in FUND_IDENTITIES:
                raise ValueError("unsupported fund code: " + code + ", must be one of: " + ", ".join(sorted(FUND_IDENTITIES)))
            existing = repo.get_product_by_provider_code("changsheng_fund", code)
            if existing is not None:
                product_id = existing.id
            else:
                from uuid import uuid5, NAMESPACE_URL
                provider = ChangshengFundProvider()
                resolved = provider.resolve_product(code)
                pid = str(uuid5(NAMESPACE_URL, "portfolio-product:auto-" + code))
                import json
                product_rec = Product(
                    id=pid, provider=resolved.provider, code=resolved.code,
                    name=resolved.name, product_type=resolved.product_type,
                    registration_code=resolved.registration_code or "",
                    metadata_json=json.dumps(dict(resolved.metadata or {}), ensure_ascii=False),
                )
                with repo.database.transaction() as conn:
                    if repo.get_product(pid, conn=conn) is None:
                        conn.execute(
                            "INSERT INTO products (id, provider, code, name, product_type, registration_code, status, metadata_json, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                            (product_rec.id, product_rec.provider, product_rec.code, product_rec.name,
                             product_rec.product_type.value, product_rec.registration_code, "active", product_rec.metadata_json),
                        )
                product_id = pid
        if not product_id:
            raise ValueError("product_id or code is required")
        product = repo.require_product(product_id)
        if product.product_type is not ProductType.PUBLIC_FUND:
            raise ValueError("target must be public_fund")
        amount = _decimal(
            body.get("daily_amount", body.get("amount")),
            "daily_amount",
            positive=True,
        )
        stored_fee_rate = body.get("purchase_fee_rate")
        if stored_fee_rate not in (None, ""):
            fee_rate = _decimal(
                stored_fee_rate,
                "purchase_fee_rate",
                nonnegative=True,
            )
        else:
            fee_rate_percent = _decimal(
                body.get("fee_rate", "0"),
                "fee_rate",
                nonnegative=True,
            )
            fee_rate = fee_rate_percent / Decimal("100")
        if fee_rate >= 1:
            raise ValueError("purchase_fee_rate must be between 0 and 1")
        source_id = str(
            body.get("source_cash_product_id") or body.get("source") or ""
        ).strip()
        if source_id:
            source = repo.require_product(source_id)
            if source.product_type is not ProductType.CASH_MANAGEMENT:
                raise ValueError("source must be cash_management")
        start_date = _parse_date(
            body.get("start_date"), "start_date", date.today()
        )
        if "activate" in body and not isinstance(body["activate"], bool):
            raise ValueError("activate must be a boolean")
        activate = body.get("activate") is True
        sip_id = str(body.get("sip_id") or body.get("plan_id") or "").strip()
        existing = None
        if sip_id:
            existing = repo.get_plan(sip_id)
            if existing is None:
                raise ValueError("plan not found")
        resulting_status = (
            existing.status
            if existing is not None
            else (
                SipPlanStatus.ACTIVE
                if activate
                else SipPlanStatus.DRAFT
            )
        )
        if resulting_status is SipPlanStatus.ACTIVE:
            require_executable_sip_products(
                repo,
                product_id,
                source_id,
            )
        return {
            "sip_id": sip_id,
            "product_id": product_id,
            "daily_amount": decimal_text(amount),
            "purchase_fee_rate": decimal_text(fee_rate),
            "purchase_fee_rate_percent": decimal_text(
                fee_rate * Decimal("100")
            ),
            "source_cash_product_id": source_id,
            "start_date": start_date.isoformat(),
            "activate": activate,
            "message": (
                "SIP plan will be updated"
                if sip_id
                else "SIP plan will be activated"
                if activate
                else "SIP plan will be saved as draft"
            ),
        }

    def adjustment_preview(body, product_id=None):
        repo, projector, _, _ = services()
        product_id = product_id or _required_text(
            body.get("product_id"), "product_id"
        )
        product = repo.require_product(product_id)
        actual = _decimal(
            body.get("actual_shares", body.get("shares")),
            "actual_shares",
            nonnegative=True,
        )
        current = projector.calculate(product_id)
        effective = _parse_date(
            body.get("effective_date"), "effective_date", date.today()
        )
        reason = _required_text(
            body.get("reason") or body.get("note"), "reason"
        )
        preview = {
            "product_id": product_id,
            "actual_shares": decimal_text(actual),
            "current_shares": decimal_text(current.total_shares),
            "difference": decimal_text(actual - current.total_shares),
            "effective_date": effective.isoformat(),
            "reason": reason,
            "message": "holding will be calibrated to the confirmed share count",
        }
        profit_value = body.get("actual_profit", body.get("profit"))
        if profit_value not in (None, ""):
            actual_profit = _decimal(profit_value, "actual_profit")
            current_profit = calculate_holding_profit(
                repo,
                product,
                effective,
            )
            preview.update(
                {
                    "actual_profit": decimal_text(actual_profit),
                    "current_profit": decimal_text(current_profit),
                    "profit_difference": decimal_text(
                        actual_profit - current_profit
                    ),
                }
            )
        return preview

    def latest_profit_adjustment_preview(body):
        repo, _, _, _ = services()
        product_id = _required_text(body.get("product_id"), "product_id")
        product = repo.require_product(product_id)
        actual_profit = _decimal(
            body.get("latest_profit", body.get("profit")),
            "latest_profit",
        )
        reason = _required_text(
            body.get("reason") or body.get("note"), "reason"
        )
        profit_date, current_profit = calculate_latest_profit(
            repo,
            product,
            _today(),
        )
        if profit_date is None or current_profit is None:
            raise ValueError("product has no latest disclosed profit")
        requested_date = body.get("effective_date") or body.get(
            "latest_profit_date"
        )
        if requested_date not in (None, ""):
            requested_date = _parse_date(
                requested_date,
                "latest_profit_date",
            )
            if requested_date != profit_date:
                raise ValueError(
                    "latest profit can only be adjusted on the latest disclosed date"
                )
        return {
            "product_id": product_id,
            "latest_profit_date": profit_date.isoformat(),
            "actual_latest_profit": decimal_text(actual_profit),
            "current_latest_profit": decimal_text(current_profit),
            "profit_difference": decimal_text(actual_profit - current_profit),
            "reason": reason,
            "message": "latest disclosed profit will be calibrated",
        }

    @blueprint.get("/api/portfolio")
    def portfolio():
        guard = read_guard()
        if guard:
            return guard
        repo = repository()
        try:
            from src.portfolio_view import build_portfolio_payload

            payload = build_portfolio_payload(repo)
        except ImportError:
            payload = {
                "products": [_json_value(row) for row in repo.list_products()],
                "transactions": [
                    _json_value(row) for row in repo.list_transactions()
                ],
                "sip_plans": [_json_value(row) for row in repo.list_plans()],
            }
        payload["write_enabled"] = bool(
            getattr(runtime, "write_enabled", False)
        )
        if not payload["write_enabled"]:
            payload["write_disabled_reason"] = _runtime_reason(runtime)
        return jsonify(payload)

    @blueprint.get("/api/portfolio/products")
    def products():
        guard = read_guard()
        if guard:
            return guard
        return jsonify(
            {"products": [_json_value(row) for row in repository().list_products()]}
        )

    @blueprint.get("/api/portfolio/products/<product_id>/history")
    def product_history(product_id):
        guard = read_guard()
        if guard:
            return guard
        repo = repository()
        if repo.get_product(product_id) is None:
            return _error(
                "product_not_found",
                "product not found",
                404,
            )
        from src.portfolio_view import build_product_history_payload

        return jsonify(
            build_product_history_payload(repo, product_id, as_of=_today())
        )

    @blueprint.get("/api/portfolio/products/search")
    def search_products():
        guard = read_guard()
        if guard:
            return guard
        query = str(request.args.get("q") or "").strip()
        if len(query) < 2:
            return _error("validation_error", "请至少输入2个字符", 400)
        if len(query) > 80:
            return _error("validation_error", "搜索内容不能超过80个字符", 400)
        product_type = str(
            request.args.get("product_type") or ""
        ).strip()
        providers = {
            ProductType.PUBLIC_FUND.value: ("eastmoney_fund",),
            ProductType.WEALTH_NAV.value: (
                "citic_wealth",
                "nanyin_wealth",
            ),
        }.get(product_type)
        if not providers:
            return _error("validation_error", "不支持该产品类型", 400)
        candidates = []
        errors = []
        for provider_name in providers:
            try:
                provider = provider_factory(provider_name)
                search = getattr(provider, "search_products", None)
                if not callable(search):
                    raise ProviderError("行情机构暂不支持产品搜索")
                candidates.extend(search(query, limit=10))
            except ProviderError as exc:
                errors.append(str(exc))
        unique = []
        seen = set()
        for candidate in candidates:
            if (
                not isinstance(candidate, MarketProduct)
                or candidate.product_type.value != product_type
            ):
                continue
            key = (candidate.provider, candidate.code.upper())
            if key in seen:
                continue
            seen.add(key)
            unique.append(_identity(candidate))
            if len(unique) >= 10:
                break
        if not unique and errors and len(errors) == len(providers):
            return _error(
                "provider_error",
                "；".join(dict.fromkeys(errors)),
                502,
            )
        return jsonify({"candidates": unique})

    @blueprint.get("/api/portfolio/transactions")
    def transactions():
        guard = read_guard()
        if guard:
            return guard
        rows = repository().list_transactions(
            product_id=request.args.get("product_id") or None
        )
        return jsonify({"transactions": [_json_value(row) for row in rows]})

    @blueprint.get("/api/portfolio/sip-plans")
    def sip_plans():
        guard = read_guard()
        if guard:
            return guard
        return jsonify(
            {"sip_plans": [_json_value(row) for row in repository().list_plans()]}
        )

    @blueprint.post("/api/portfolio/products/preview")
    def preview_product():
        idem, guard = write_guard()
        if guard:
            return guard
        try:
            body = json_body()
            preview, _, _ = product_preview(body)
            return jsonify({"preview": preview})
        except (ProviderError, ValueError) as exc:
            return business_error(
                "portfolio_product_preview",
                "product",
                "",
                request.get_json(silent=True) or {},
                exc,
                idem,
                code=(
                    "provider_error"
                    if isinstance(exc, ProviderError)
                    else "validation_error"
                ),
                status=502 if isinstance(exc, ProviderError) else 400,
                persist=False,
            )

    @blueprint.post("/api/portfolio/products")
    def create_product():
        idem, guard = write_guard()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return business_error(
                "portfolio_product_create",
                "product",
                "",
                {},
                ValueError("JSON body must be an object"),
                idem,
            )
        try:
            cached = replay("product", idem, body)
            if cached:
                return cached
            repo = repository()
            if str(body.get("operation") or "").strip().lower() == "disable":
                product_id = _required_text(
                    body.get("product_id"), "product_id"
                )
                with repo.database.transaction() as conn:
                    cached = replay("product", idem, body, conn=conn)
                    if cached:
                        return cached
                    product = repo.require_product(product_id, conn=conn)
                    conn.execute(
                        "UPDATE products SET status = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (ProductStatus.INACTIVE.value, product_id),
                    )
                    product = replace(
                        product, status=ProductStatus.INACTIVE
                    )
                    payload = {"product": _json_value(product)}
                    audit(
                        "portfolio_product_disable",
                        "product",
                        product_id,
                        payload,
                        "success",
                        idem,
                        conn=conn,
                    )
                    remember(
                        "product",
                        idem,
                        body,
                        payload,
                        200,
                        conn=conn,
                    )
                return jsonify(payload), 200

            preview, resolved, quote = product_preview(
                body, require_identity=True
            )
            product = Product(
                id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"portfolio-product:{idem}",
                    )
                ),
                provider=resolved.provider,
                code=resolved.code,
                name=resolved.name,
                product_type=resolved.product_type,
                registration_code=resolved.registration_code,
                metadata_json=json.dumps(
                    dict(resolved.metadata),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            payload = {"product": _json_value(product), "preview": preview}
            with repo.database.transaction() as conn:
                cached = replay("product", idem, body, conn=conn)
                if cached:
                    return cached
                repo.add_product(product, conn=conn)
                repo.upsert_quote(
                    product.id,
                    quote,
                    beijing_now().isoformat(timespec="seconds"),
                    conn=conn,
                )
                audit(
                    "portfolio_product_create",
                    "product",
                    product.id,
                    payload,
                    "success",
                    idem,
                    conn=conn,
                )
                remember(
                    "product",
                    idem,
                    body,
                    payload,
                    201,
                    conn=conn,
                )
            return jsonify(payload), 201
        except (ProviderError, ValueError) as exc:
            return business_error(
                "portfolio_product_create",
                "product",
                str(body.get("product_id") or ""),
                body,
                exc,
                idem,
                code=(
                    "provider_error"
                    if isinstance(exc, ProviderError)
                    else "validation_error"
                ),
                status=502 if isinstance(exc, ProviderError) else 400,
                operation_action="product",
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                "portfolio product write failed",
                500,
            )

    @blueprint.post("/api/portfolio/transactions/preview")
    def preview_transaction():
        idem, guard = write_guard()
        if guard:
            return guard
        try:
            body = json_body()
            return jsonify({"preview": transaction_preview(body)})
        except ValueError as exc:
            return business_error(
                "portfolio_transaction_preview",
                "transaction",
                "",
                request.get_json(silent=True) or {},
                exc,
                idem,
                persist=False,
            )

    @blueprint.post("/api/portfolio/transactions")
    def create_transaction():
        idem, guard = write_guard()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return business_error(
                "portfolio_transaction_create",
                "transaction",
                "",
                {},
                ValueError("JSON body must be an object"),
                idem,
            )
        try:
            cached = replay("transaction", idem, body)
            if cached:
                return cached
            preview = transaction_preview(body)
            normalized = preview["normalized_input"]
            _, _, service, _ = services()
            operation = normalized.get("operation")
            if operation == "cancel":
                transaction = service.cancel_pending(
                    normalized["transaction_id"], idem
                )
                status = 200
            elif operation == "reverse":
                transaction = service.reverse_confirmed(
                    normalized["transaction_id"],
                    _required_text(normalized.get("reason"), "reason"),
                    idem,
                )
                status = 200
            elif normalized["kind"] == "purchase":
                transaction = service.record_purchase(
                    normalized["product_id"],
                    normalized["amount"],
                    _parse_date(normalized["trade_date"], "trade_date"),
                    idem,
                    source_cash_product_id=normalized[
                        "source_cash_product_id"
                    ],
                    fee_rate=normalized["fee_rate"],
                    note=normalized["note"],
                    trade_time=normalized.get("trade_time", ""),
                    audit_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"portfolio-api-business:"
                            f"portfolio_transaction_create:{idem}",
                        )
                    ),
                )
                status = 201
            else:
                transaction = service.record_redemption(
                    normalized["product_id"],
                    normalized["shares"],
                    _parse_date(normalized["trade_date"], "trade_date"),
                    idem,
                    destination_cash_product_id=normalized[
                        "destination_cash_product_id"
                    ],
                    note=normalized["note"],
                    trade_time=normalized.get("trade_time", ""),
                    settlement_date=(
                        _parse_date(normalized["settlement_date"], "settlement_date")
                        if normalized.get("settlement_date")
                        else None
                    ),
                    audit_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"portfolio-api-business:"
                            f"portfolio_transaction_create:{idem}",
                        )
                    ),
                )
                status = 201
            payload = {
                "transaction": _json_value(transaction),
                "preview": preview,
            }
            remember("transaction", idem, body, payload, status)
            return jsonify(payload), status
        except ValueError as exc:
            return business_error(
                "portfolio_transaction_create",
                "transaction",
                str(body.get("transaction_id") or ""),
                body,
                exc,
                idem,
                operation_action="transaction",
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                "portfolio transaction write failed",
                500,
            )

    def transaction_operation(transaction_id, operation):
        idem, guard = write_guard()
        if guard:
            return guard
        action = f"transaction:{operation}:{transaction_id}"
        try:
            body = json_body()
            cached = replay(action, idem, body)
            if cached:
                return cached
            _, _, service, _ = services()
            if operation == "cancel":
                transaction = service.cancel_pending(transaction_id, idem)
            else:
                transaction = service.reverse_confirmed(
                    transaction_id,
                    _required_text(
                        body.get("reason") or body.get("note"), "reason"
                    ),
                    idem,
                )
            payload = {"transaction": _json_value(transaction)}
            remember(action, idem, body, payload, 200)
            return jsonify(payload)
        except ValueError as exc:
            return business_error(
                f"portfolio_transaction_{operation}",
                "transaction",
                transaction_id,
                request.get_json(silent=True) or {},
                exc,
                idem,
                operation_action=action,
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                f"portfolio transaction {operation} failed",
                500,
            )

    @blueprint.post("/api/portfolio/transactions/<transaction_id>/cancel")
    def cancel_transaction(transaction_id):
        return transaction_operation(transaction_id, "cancel")

    @blueprint.post("/api/portfolio/transactions/<transaction_id>/reverse")
    def reverse_transaction(transaction_id):
        return transaction_operation(transaction_id, "reverse")

    def adjust_position(body_product_id=None):
        idem, guard = write_guard()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return business_error(
                "portfolio_position_adjust",
                "product",
                body_product_id or "",
                {},
                ValueError("JSON body must be an object"),
                idem,
            )
        try:
            preview = adjustment_preview(body, body_product_id)
            action = f"adjust:{preview['product_id']}"
            cached = replay(action, idem, body)
            if cached:
                return cached
            _, _, service, _ = services()
            transaction = service.adjust_holding(
                preview["product_id"],
                preview["actual_shares"],
                _parse_date(preview["effective_date"], "effective_date"),
                preview["reason"],
                (
                    f"{idem}:shares"
                    if "actual_profit" in preview
                    else idem
                ),
            )
            payload = {
                "transaction": _json_value(transaction),
                "preview": preview,
            }
            if "actual_profit" in preview:
                profit_transaction = service.adjust_holding_profit(
                    preview["product_id"],
                    preview["actual_profit"],
                    _parse_date(
                        preview["effective_date"],
                        "effective_date",
                    ),
                    preview["reason"],
                    f"{idem}:profit",
                )
                payload["profit_transaction"] = _json_value(
                    profit_transaction
                )
            remember(action, idem, body, payload, 200)
            return jsonify(payload)
        except ValueError as exc:
            return business_error(
                "portfolio_position_adjust",
                "product",
                body_product_id or str(body.get("product_id") or ""),
                body,
                exc,
                idem,
                operation_action=(
                    f"adjust:{body_product_id or str(body.get('product_id') or '')}"
                ),
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                "portfolio position adjustment failed",
                500,
            )

    @blueprint.post("/api/portfolio/positions/<product_id>/adjust")
    def adjust_named_position(product_id):
        return adjust_position(product_id)

    @blueprint.post("/api/portfolio/positions/adjustments/preview")
    def preview_mobile_adjustment():
        idem, guard = write_guard()
        if guard:
            return guard
        try:
            body = json_body()
            return jsonify({"preview": adjustment_preview(body)})
        except ValueError as exc:
            return business_error(
                "portfolio_position_adjust_preview",
                "product",
                "",
                request.get_json(silent=True) or {},
                exc,
                idem,
                persist=False,
            )

    @blueprint.post("/api/portfolio/positions/adjustments")
    def adjust_mobile_position():
        return adjust_position()

    @blueprint.post("/api/portfolio/latest-profit-adjustments/preview")
    def preview_latest_profit_adjustment():
        idem, guard = write_guard()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error(
                "invalid_request",
                "JSON body must be an object",
                400,
            )
        try:
            return jsonify(
                {"preview": latest_profit_adjustment_preview(body)}
            )
        except ValueError as exc:
            return business_error(
                "portfolio_latest_profit_adjustment_preview",
                "product",
                str(body.get("product_id") or ""),
                body,
                exc,
                idem,
            )

    @blueprint.post("/api/portfolio/latest-profit-adjustments")
    def adjust_latest_profit():
        idem, guard = write_guard()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return _error(
                "invalid_request",
                "JSON body must be an object",
                400,
            )
        product_id = str(body.get("product_id") or "")
        try:
            preview = latest_profit_adjustment_preview(body)
            action = f"adjust-latest-profit:{preview['product_id']}"
            cached = replay(action, idem, body)
            if cached:
                return cached
            _, _, service, _ = services()
            transaction = service.adjust_latest_profit(
                preview["product_id"],
                preview["actual_latest_profit"],
                _parse_date(
                    preview["latest_profit_date"],
                    "latest_profit_date",
                ),
                preview["reason"],
                idem,
                as_of=_today(),
            )
            payload = {
                "preview": preview,
                "transaction": _json_value(transaction),
            }
            remember(action, idem, body, payload, 200)
            return jsonify(payload)
        except ValueError as exc:
            return business_error(
                "portfolio_latest_profit_adjustment",
                "product",
                product_id,
                body,
                exc,
                idem,
                operation_action=f"adjust-latest-profit:{product_id}",
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                "portfolio latest profit adjustment failed",
                500,
            )

    @blueprint.post("/api/portfolio/sip-plans/preview")
    def preview_sip_plan():
        idem, guard = write_guard()
        if guard:
            return guard
        try:
            body = json_body()
            return jsonify({"preview": sip_preview(body)})
        except ValueError as exc:
            return business_error(
                "portfolio_sip_preview",
                "sip_plan",
                "",
                request.get_json(silent=True) or {},
                exc,
                idem,
                persist=False,
            )

    @blueprint.post("/api/portfolio/sip-plans")
    def create_sip_plan():
        idem, guard = write_guard()
        if guard:
            return guard
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return business_error(
                "portfolio_sip_write",
                "sip_plan",
                "",
                {},
                ValueError("JSON body must be an object"),
                idem,
            )
        try:
            action = "sip"
            cached = replay(action, idem, body)
            if cached:
                return cached
            preview = sip_preview(body)
            operation = preview.get("operation")
            repo = repository()
            with repo.database.transaction() as conn:
                cached = replay(action, idem, body, conn=conn)
                if cached:
                    return cached
                if operation in {"pause", "resume", "activate", "delete"}:
                    plan = repo.get_plan(preview["sip_id"], conn=conn)
                    if plan is None:
                        raise ValueError("plan not found")
                    if operation == "delete":
                        repo.delete_plan(plan.id, conn=conn)
                    elif operation == "activate":
                        if plan.status is not SipPlanStatus.DRAFT:
                            raise ValueError("only draft plans can be activated")
                        require_executable_sip_products(
                            repo,
                            plan.product_id,
                            plan.source_cash_product_id,
                            conn=conn,
                        )
                        plan = replace(plan, status=SipPlanStatus.ACTIVE)
                    else:
                        if plan.status is SipPlanStatus.DRAFT:
                            raise ValueError(
                                "plan is not active"
                                if operation == "pause"
                                else "plan is not paused"
                            )
                        if operation == "resume":
                            require_executable_sip_products(
                                repo,
                                plan.product_id,
                                plan.source_cash_product_id,
                                conn=conn,
                            )
                        plan = replace(
                            plan,
                            status=(
                                SipPlanStatus.PAUSED
                                if operation == "pause"
                                else SipPlanStatus.ACTIVE
                            ),
                        )
                    status = 200
                else:
                    existing = (
                        repo.get_plan(preview["sip_id"], conn=conn)
                        if preview["sip_id"]
                        else None
                    )
                    plan = SipPlan(
                        id=(
                            existing.id
                            if existing is not None
                            else str(
                                uuid5(
                                    NAMESPACE_URL,
                                    f"portfolio-sip:{idem}",
                                )
                            )
                        ),
                        product_id=preview["product_id"],
                        daily_amount=Decimal(preview["daily_amount"]),
                        purchase_fee_rate=Decimal(
                            preview["purchase_fee_rate"]
                        ),
                        source_cash_product_id=preview[
                            "source_cash_product_id"
                        ],
                        status=(
                            existing.status
                            if existing is not None
                            else (
                                SipPlanStatus.ACTIVE
                                if preview["activate"]
                                else SipPlanStatus.DRAFT
                            )
                        ),
                        start_date=_parse_date(
                            preview["start_date"], "start_date"
                        ),
                    )
                    if plan.status is SipPlanStatus.ACTIVE:
                        require_executable_sip_products(
                            repo,
                            plan.product_id,
                            plan.source_cash_product_id,
                            conn=conn,
                        )
                    status = 200 if existing is not None else 201
                if operation != "delete":
                    repo.save_plan(plan, conn=conn)
                payload = {
                    "sip_plan": _json_value(plan),
                    "preview": preview,
                }
                if operation == "delete":
                    payload["deleted"] = True
                audit(
                    "portfolio_sip_write",
                    "sip_plan",
                    plan.id,
                    payload,
                    "success",
                    idem,
                    conn=conn,
                )
                remember(
                    action,
                    idem,
                    body,
                    payload,
                    status,
                    conn=conn,
                )
            if (
                operation != "delete"
                and plan.status is SipPlanStatus.ACTIVE
            ):
                _, _, _, sip = services()
                sip.backfill_plan(
                    plan.id,
                    beijing_now().date(),
                )
            return jsonify(payload), status
        except ValueError as exc:
            return business_error(
                "portfolio_sip_write",
                "sip_plan",
                str(body.get("sip_id") or body.get("plan_id") or ""),
                body,
                exc,
                idem,
                operation_action="sip",
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                "portfolio SIP write failed",
                500,
            )

    def sip_operation(plan_id, operation):
        idem, guard = write_guard()
        if guard:
            return guard
        action = f"sip:{operation}:{plan_id}"
        try:
            body = json_body()
            cached = replay(action, idem, body)
            if cached:
                return cached
            repo = repository()
            with repo.database.transaction() as conn:
                cached = replay(action, idem, body, conn=conn)
                if cached:
                    return cached
                plan = repo.get_plan(plan_id, conn=conn)
                if plan is None:
                    raise ValueError("plan not found")
                if plan.status is SipPlanStatus.DRAFT:
                    raise ValueError(
                        "plan is not active"
                        if operation == "pause"
                        else "plan is not paused"
                    )
                if operation == "resume":
                    require_executable_sip_products(
                        repo,
                        plan.product_id,
                        plan.source_cash_product_id,
                        conn=conn,
                    )
                plan = replace(
                    plan,
                    status=(
                        SipPlanStatus.PAUSED
                        if operation == "pause"
                        else SipPlanStatus.ACTIVE
                    ),
                )
                repo.save_plan(plan, conn=conn)
                payload = {"sip_plan": _json_value(plan)}
                audit(
                    f"portfolio_sip_{operation}",
                    "sip_plan",
                    plan_id,
                    payload,
                    "success",
                    idem,
                    conn=conn,
                )
                remember(
                    action,
                    idem,
                    body,
                    payload,
                    200,
                    conn=conn,
                )
            return jsonify(payload)
        except ValueError as exc:
            return business_error(
                f"portfolio_sip_{operation}",
                "sip_plan",
                plan_id,
                request.get_json(silent=True) or {},
                exc,
                idem,
                operation_action=action,
            )
        except Exception:
            return _error(
                "portfolio_write_failed",
                f"portfolio SIP {operation} failed",
                500,
            )

    @blueprint.post("/api/portfolio/sip-plans/<plan_id>/pause")
    def pause_sip_plan(plan_id):
        return sip_operation(plan_id, "pause")

    @blueprint.post("/api/portfolio/sip-plans/<plan_id>/resume")
    def resume_sip_plan(plan_id):
        return sip_operation(plan_id, "resume")

    return blueprint
