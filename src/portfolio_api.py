from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import threading
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from flask import Blueprint, jsonify, request

from src.portfolio_market import QuoteSyncService, validate_market_quote
from src.portfolio_models import (
    MarketProduct,
    Product,
    ProductStatus,
    ProductType,
    decimal_text,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_sip import SipService
from src.portfolio_transactions import PortfolioTransactionService


_WRITE_LIMIT = 30
_WRITE_WINDOW_SECONDS = 60.0
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
        return "changsheng_fund"
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


def create_portfolio_blueprint(runtime, provider_factory) -> Blueprint:
    blueprint = Blueprint("portfolio_api", __name__)
    rate_buckets = defaultdict(deque)
    rate_lock = threading.Lock()
    replay_cache = {}
    replay_lock = threading.Lock()

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

    def audit(action, object_type, object_id, body, result, idem):
        repo = repository()
        if repo is None:
            return
        audit_id = str(
            uuid5(
                NAMESPACE_URL,
                f"portfolio-api:{action}:{idem}:{result}",
            )
        )
        after = json.dumps(
            _safe_audit_value(body),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            repo.append_audit(
                audit_id,
                action,
                object_type,
                object_id or "",
                "{}",
                after,
                result,
                "web",
            )
        except Exception:
            # An identical retry has already produced the same immutable audit.
            pass

    def replay_key(action, idem):
        return action, idem

    def request_fingerprint(body):
        return hashlib.sha256(
            json.dumps(
                _safe_audit_value(body),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def replay(action, idem, body):
        with replay_lock:
            cached = replay_cache.get(replay_key(action, idem))
        if cached is None:
            return None
        fingerprint, payload, status = cached
        if fingerprint != request_fingerprint(body):
            raise ValueError("idempotency key conflicts with existing request")
        return jsonify(payload), status

    def remember(action, idem, body, payload, status):
        with replay_lock:
            replay_cache[replay_key(action, idem)] = (
                request_fingerprint(body),
                payload,
                status,
            )

    def business_error(action, object_type, object_id, body, exc, idem):
        audit(
            action,
            object_type,
            object_id,
            {**_safe_audit_value(body), "error": str(exc)[:240]},
            "rejected",
            idem,
        )
        return _error("validation_error", str(exc), 400)

    def product_preview(body):
        code = _required_text(body.get("code"), "code")
        declared_type = (
            body.get("product_type") or body.get("wealth_type") or ""
        )
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
        if body.get("name") and body["name"].strip() != resolved.name:
            raise ValueError("resolved product name does not match request")
        if declared_type and str(declared_type) != resolved.product_type.value:
            raise ValueError("resolved product type does not match request")
        expected_registration = body.get("registration_code")
        if (
            expected_registration
            and expected_registration != resolved.registration_code
        ):
            raise ValueError("resolved product identity changed")

        today = date.today()
        quotes = provider.fetch_quotes(resolved, today, today)
        if not quotes:
            raise ValueError("provider returned no valid quote")
        for quote in quotes:
            if quote.product_code.strip().upper() != resolved.code.upper():
                raise ValueError("provider returned a quote for another product")
            validate_market_quote(resolved.product_type, quote)
        quote = sorted(quotes, key=lambda item: item.quote_date, reverse=True)[0]
        return {
            "product": _identity(resolved),
            "quote": _json_value(quote),
            "message": "official identity and quote verified",
        }, resolved

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
            return {
                "normalized_input": {
                    "operation": operation,
                    "transaction_id": transaction_id,
                    "reason": str(body.get("reason") or body.get("note") or ""),
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
        trade_date = _parse_date(
            body.get("trade_date"), "trade_date", date.today()
        )
        quote = repo.get_quote(product.id, trade_date)
        nav = (
            Decimal("1")
            if product.product_type is ProductType.CASH_MANAGEMENT
            else (quote.unit_nav if quote is not None else None)
        )
        status = "confirmed" if nav is not None else "pending_quote"

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
            }
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
        }
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
            ),
        }

    def sip_preview(body):
        repo, _, _, _ = services()
        operation = str(body.get("operation") or "").strip().lower()
        if operation in {"pause", "resume"}:
            plan_id = _required_text(
                body.get("sip_id") or body.get("plan_id"), "sip_id"
            )
            if repo.get_plan(plan_id) is None:
                raise ValueError("plan not found")
            return {
                "operation": operation,
                "sip_id": plan_id,
                "message": f"SIP plan will be {operation}d",
            }
        product_id = _required_text(body.get("product_id"), "product_id")
        product = repo.require_product(product_id)
        if product.product_type is not ProductType.PUBLIC_FUND:
            raise ValueError("target must be public_fund")
        amount = _decimal(
            body.get("daily_amount", body.get("amount")),
            "daily_amount",
            positive=True,
        )
        fee_rate = _decimal(
            body.get("purchase_fee_rate", body.get("fee_rate", "0")),
            "purchase_fee_rate",
            nonnegative=True,
        )
        if fee_rate >= 1:
            raise ValueError("purchase_fee_rate must be between 0 and 1")
        source_id = _required_text(
            body.get("source_cash_product_id") or body.get("source"),
            "source_cash_product_id",
        )
        source = repo.require_product(source_id)
        if source.product_type is not ProductType.CASH_MANAGEMENT:
            raise ValueError("source must be cash_management")
        start_date = _parse_date(
            body.get("start_date"), "start_date", date.today()
        )
        return {
            "product_id": product_id,
            "daily_amount": decimal_text(amount),
            "purchase_fee_rate": decimal_text(fee_rate),
            "source_cash_product_id": source_id,
            "start_date": start_date.isoformat(),
            "message": "SIP plan will be activated",
        }

    def adjustment_preview(body, product_id=None):
        repo, projector, _, _ = services()
        product_id = product_id or _required_text(
            body.get("product_id"), "product_id"
        )
        repo.require_product(product_id)
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
        return {
            "product_id": product_id,
            "actual_shares": decimal_text(actual),
            "current_shares": decimal_text(current.total_shares),
            "difference": decimal_text(actual - current.total_shares),
            "effective_date": effective.isoformat(),
            "reason": reason,
            "message": "holding will be calibrated to the confirmed share count",
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
            preview, _ = product_preview(body)
            return jsonify({"preview": preview})
        except ValueError as exc:
            return business_error(
                "portfolio_product_preview",
                "product",
                "",
                request.get_json(silent=True) or {},
                exc,
                idem,
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
            if str(body.get("operation") or "").strip().lower() == "disable":
                product_id = _required_text(
                    body.get("product_id"), "product_id"
                )
                repo = repository()
                repo.require_product(product_id)
                with repo.database.transaction() as conn:
                    conn.execute(
                        "UPDATE products SET status = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (ProductStatus.INACTIVE.value, product_id),
                    )
                product = repo.require_product(product_id)
                payload = {"product": _json_value(product)}
                audit(
                    "portfolio_product_disable",
                    "product",
                    product_id,
                    payload,
                    "success",
                    idem,
                )
                remember("product", idem, body, payload, 200)
                return jsonify(payload), 200

            preview, resolved = product_preview(body)
            product = Product(
                id=str(uuid4()),
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
            repo = repository()
            repo.add_product(product)
            QuoteSyncService(repo, provider_factory).sync_product(
                resolved, date.today(), date.today()
            )
            payload = {"product": _json_value(product), "preview": preview}
            audit(
                "portfolio_product_create",
                "product",
                product.id,
                payload,
                "success",
                idem,
            )
            remember("product", idem, body, payload, 201)
            return jsonify(payload), 201
        except ValueError as exc:
            return business_error(
                "portfolio_product_create",
                "product",
                str(body.get("product_id") or ""),
                body,
                exc,
                idem,
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
                )
                status = 201
            payload = {
                "transaction": _json_value(transaction),
                "preview": preview,
            }
            audit(
                "portfolio_transaction_create",
                "transaction",
                transaction.id,
                payload,
                "success",
                idem,
            )
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
            )

    def transaction_operation(transaction_id, operation):
        idem, guard = write_guard()
        if guard:
            return guard
        try:
            body = json_body()
            action = f"transaction:{operation}:{transaction_id}"
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
            audit(
                f"portfolio_transaction_{operation}",
                "transaction",
                transaction_id,
                payload,
                "success",
                idem,
            )
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
                idem,
            )
            payload = {
                "transaction": _json_value(transaction),
                "preview": preview,
            }
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
            )

    @blueprint.post("/api/portfolio/positions/adjustments")
    def adjust_mobile_position():
        return adjust_position()

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
            _, _, _, sip = services()
            operation = preview.get("operation")
            if operation == "pause":
                plan = sip.pause(preview["sip_id"])
                status = 200
            elif operation == "resume":
                plan = sip.resume(preview["sip_id"])
                status = 200
            else:
                plan = sip.save_plan(
                    product_id=preview["product_id"],
                    daily_amount=preview["daily_amount"],
                    purchase_fee_rate=preview["purchase_fee_rate"],
                    source_cash_product_id=preview[
                        "source_cash_product_id"
                    ],
                    start_date=_parse_date(
                        preview["start_date"], "start_date"
                    ),
                )
                plan = sip.activate(plan.id)
                status = 201
            payload = {"sip_plan": _json_value(plan), "preview": preview}
            audit(
                "portfolio_sip_write",
                "sip_plan",
                plan.id,
                payload,
                "success",
                idem,
            )
            remember(action, idem, body, payload, status)
            return jsonify(payload), status
        except ValueError as exc:
            return business_error(
                "portfolio_sip_write",
                "sip_plan",
                str(body.get("sip_id") or body.get("plan_id") or ""),
                body,
                exc,
                idem,
            )

    def sip_operation(plan_id, operation):
        idem, guard = write_guard()
        if guard:
            return guard
        try:
            body = json_body()
            action = f"sip:{operation}:{plan_id}"
            cached = replay(action, idem, body)
            if cached:
                return cached
            _, _, _, sip = services()
            plan = sip.pause(plan_id) if operation == "pause" else sip.resume(plan_id)
            payload = {"sip_plan": _json_value(plan)}
            audit(
                f"portfolio_sip_{operation}",
                "sip_plan",
                plan_id,
                payload,
                "success",
                idem,
            )
            remember(action, idem, body, payload, 200)
            return jsonify(payload)
        except ValueError as exc:
            return business_error(
                f"portfolio_sip_{operation}",
                "sip_plan",
                plan_id,
                request.get_json(silent=True) or {},
                exc,
                idem,
            )

    @blueprint.post("/api/portfolio/sip-plans/<plan_id>/pause")
    def pause_sip_plan(plan_id):
        return sip_operation(plan_id, "pause")

    @blueprint.post("/api/portfolio/sip-plans/<plan_id>/resume")
    def resume_sip_plan(plan_id):
        return sip_operation(plan_id, "resume")

    return blueprint
