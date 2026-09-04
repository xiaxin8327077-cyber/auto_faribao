from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import Flask

from src.nav_monitor import ProviderError
from src.portfolio_api import create_portfolio_blueprint
from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketProduct,
    MarketQuote,
    Product,
    ProductType,
    SipFrequency,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_positions import PositionProjector
from src.portfolio_repository import PortfolioRepository


TRADE_DATE = date(2026, 7, 30)


def write_headers(key="secret", idem="request-1"):
    return {
        "X-Nav-Dashboard-Key": key,
        "X-Portfolio-Request": "1",
        "Idempotency-Key": idem,
        "Content-Type": "application/json",
    }


class FakeProvider:
    provider = "test"

    def __init__(self):
        self.resolved = MarketProduct(
            provider="test",
            code="003103",
            name="长盛盛裕纯债A",
            product_type=ProductType.PUBLIC_FUND,
            registration_code="fund-registration",
        )
        self.resolve_calls = 0
        self.fetch_calls = []
        self.failure = None
        self.quote_date = date.today()

    def resolve_product(self, code):
        self.resolve_calls += 1
        if self.failure:
            raise self.failure
        if code.strip() != self.resolved.code:
            raise ValueError("product not found")
        return self.resolved

    def fetch_quotes(self, product, start_date, end_date):
        self.fetch_calls.append((start_date, end_date))
        if self.failure:
            raise self.failure
        return [
            MarketQuote(
                product_code=product.code,
                quote_date=self.quote_date,
                source="official",
                raw_hash="quote-hash",
                unit_nav=Decimal("1.0321"),
            )
        ]


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "nav-dashboard-token"
    path.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(
        "src.nav_dashboard.get_access_token",
        lambda: path.read_text(encoding="utf-8").strip(),
    )
    return path


@pytest.fixture
def api_setup(tmp_path, token_file):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    repository = PortfolioRepository(database)
    cash = Product(
        id="cash",
        provider="test",
        code="cash",
        name="现金",
        product_type=ProductType.CASH_MANAGEMENT,
    )
    fund = Product(
        id="fund",
        provider="test",
        code="fund",
        name="基金",
        product_type=ProductType.PUBLIC_FUND,
    )
    repository.add_product(cash)
    repository.add_product(fund)
    repository.create_transaction(
        Transaction(
            id="opening:cash",
            product_id="cash",
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 1),
            idempotency_key="opening:cash",
            amount=Decimal("1000"),
            shares=Decimal("1000"),
            confirmation_nav=Decimal("1"),
            confirmation_date=date(2026, 7, 1),
        )
    )
    PositionProjector(repository).rebuild()
    runtime = SimpleNamespace(
        database=database,
        repository=repository,
        write_enabled=True,
        migration_error="",
    )
    provider = FakeProvider()
    app = Flask(__name__)
    app.register_blueprint(
        create_portfolio_blueprint(runtime, lambda _name: provider)
    )
    return app.test_client(), runtime, repository, provider


@pytest.fixture
def client(api_setup):
    return api_setup[0]


def test_read_routes_require_private_token_and_return_json(client):
    assert client.get("/api/portfolio").status_code == 403

    for path, key in (
        ("/api/portfolio", "products"),
        ("/api/portfolio/products", "products"),
        ("/api/portfolio/transactions", "transactions"),
        ("/api/portfolio/sip-plans", "sip_plans"),
    ):
        response = client.get(
            path, headers={"X-Nav-Dashboard-Key": "secret"}
        )
        assert response.status_code == 200
        assert key in response.get_json()


def test_product_history_route_requires_token_and_returns_daily_rows(api_setup):
    client, _, repository, _ = api_setup
    for quote_date, nav in (
        (date(2026, 8, 3), "1.00"),
        (date(2026, 8, 4), "1.01"),
    ):
        repository.upsert_quote(
            "fund",
            MarketQuote(
                "fund",
                quote_date,
                "official",
                f"fund:{quote_date}",
                unit_nav=Decimal(nav),
            ),
            f"{quote_date}T18:00:00+08:00",
        )

    path = "/api/portfolio/products/fund/history"
    assert client.get(path).status_code == 403
    response = client.get(
        path,
        headers={"X-Nav-Dashboard-Key": "secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["history"][0]["date"] == "2026-08-04"


def test_product_history_route_returns_not_found_for_unknown_product(client):
    response = client.get(
        "/api/portfolio/products/missing/history",
        headers={"X-Nav-Dashboard-Key": "secret"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "product_not_found"


def test_product_search_returns_external_candidates_for_partial_input(
    api_setup,
):
    client, _, _, provider = api_setup
    provider.search_products = lambda query, limit=10: [
        MarketProduct(
            provider="eastmoney_fund",
            code="002112",
            name=f"{query}匹配基金",
            product_type=ProductType.PUBLIC_FUND,
            registration_code="002112",
        )
    ][:limit]

    response = client.get(
        "/api/portfolio/products/search",
        headers={"X-Nav-Dashboard-Key": "secret"},
        query_string={"q": "0211", "product_type": "public_fund"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "candidates": [
            {
                "provider": "eastmoney_fund",
                "code": "002112",
                "name": "0211匹配基金",
                "product_type": "public_fund",
                "registration_code": "002112",
            }
        ]
    }


def test_product_search_requires_at_least_two_characters(client):
    response = client.get(
        "/api/portfolio/products/search",
        headers={"X-Nav-Dashboard-Key": "secret"},
        query_string={"q": "0", "product_type": "public_fund"},
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "请至少输入2个字符"


def test_unexpected_api_failure_still_returns_uniform_json(
    api_setup, monkeypatch
):
    client, _, repository, _ = api_setup
    monkeypatch.setattr(
        repository,
        "list_products",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("storage unavailable")
        ),
    )

    response = client.get(
        "/api/portfolio/products",
        headers={"X-Nav-Dashboard-Key": "secret"},
    )

    assert response.status_code == 500
    assert response.is_json
    assert response.get_json()["error"] == "internal_error"


def test_write_requires_token_json_custom_header_and_idempotency(client):
    body = {"kind": "purchase", "product_id": "cash", "amount": "100"}
    assert client.post("/api/portfolio/transactions", json=body).status_code == 403
    assert (
        client.post(
            "/api/portfolio/transactions",
            headers={"X-Nav-Dashboard-Key": "secret"},
            data="not-json",
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/portfolio/transactions",
            headers={
                "X-Nav-Dashboard-Key": "secret",
                "Content-Type": "application/json",
            },
            json=body,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/portfolio/transactions",
            headers={
                "X-Nav-Dashboard-Key": "secret",
                "X-Portfolio-Request": "1",
            },
            json=body,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/portfolio/transactions",
            headers=write_headers(idem="x" * 129),
            json=body,
        ).status_code
        == 400
    )


def test_write_disabled_guard_runs_before_json_validation(api_setup):
    client, runtime, _, _ = api_setup
    runtime.write_enabled = False
    runtime.migration_error = "migration failed"

    response = client.post(
        "/api/portfolio/transactions",
        headers={"X-Nav-Dashboard-Key": "secret"},
        data="not-json",
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "write_disabled",
        "message": "portfolio writes are disabled",
        "reason": "migration failed",
    }


def test_duplicate_write_returns_same_transaction(client):
    body = {
        "kind": "purchase",
        "product_id": "cash",
        "amount": "100",
        "trade_date": "2026-07-30",
    }
    first = client.post(
        "/api/portfolio/transactions", headers=write_headers(), json=body
    )
    second = client.post(
        "/api/portfolio/transactions", headers=write_headers(), json=body
    )
    assert first.status_code == second.status_code == 201
    assert (
        first.get_json()["transaction"]["id"]
        == second.get_json()["transaction"]["id"]
    )


def test_preview_and_submit_may_use_the_same_idempotency_key(client):
    body = {
        "kind": "purchase",
        "product_id": "cash",
        "amount": "25",
        "trade_date": "2026-07-30",
    }
    preview = client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="same-key"),
        json=body,
    )
    submit = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="same-key"),
        json=body,
    )

    assert preview.status_code == 200
    assert submit.status_code == 201


def test_transaction_idempotency_replays_after_blueprint_restart(api_setup):
    client, runtime, _, provider = api_setup
    body = {
        "kind": "purchase",
        "product_id": "cash",
        "amount": "30",
        "trade_date": "2026-07-30",
        "note": "persist this note",
    }
    first = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="restart-key"),
        json=body,
    )
    restarted = Flask("restarted")
    restarted.register_blueprint(
        create_portfolio_blueprint(runtime, lambda _name: provider)
    )
    second = restarted.test_client().post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="restart-key"),
        json=body,
    )

    assert first.status_code == second.status_code == 201
    assert first.get_json() == second.get_json()
    assert first.get_json()["transaction"]["note"] == "persist this note"


def test_restart_rejects_same_key_with_different_request(api_setup):
    client, runtime, _, provider = api_setup
    first = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="restart-conflict"),
        json={
            "kind": "purchase",
            "product_id": "cash",
            "amount": "30",
            "trade_date": "2026-07-30",
        },
    )
    restarted = Flask("restarted-conflict")
    restarted.register_blueprint(
        create_portfolio_blueprint(runtime, lambda _name: provider)
    )
    conflicting = restarted.test_client().post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="restart-conflict"),
        json={
            "kind": "purchase",
            "product_id": "cash",
            "amount": "31",
            "trade_date": "2026-07-30",
        },
    )

    assert first.status_code == 201
    assert conflicting.status_code == 400
    assert conflicting.get_json()["error"] == "validation_error"
    assert "idempotency key conflicts" in conflicting.get_json()["message"]


def test_transaction_audit_failure_rolls_back_business_write(
    api_setup, monkeypatch
):
    client, _, repository, _ = api_setup
    monkeypatch.setattr(
        repository,
        "append_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("audit unavailable")
        ),
    )

    response = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="transaction-audit-failure"),
        json={
            "kind": "purchase",
            "product_id": "cash",
            "amount": "30",
            "trade_date": "2026-07-30",
        },
    )

    assert response.status_code == 500
    assert response.is_json
    assert repository.get_transaction_by_idempotency(
        "transaction-audit-failure"
    ) is None


def test_transaction_preview_normalizes_server_side_values(client):
    response = client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="preview-purchase"),
        json={
            "kind": "purchase",
            "note": "",
            "product_id": "cash",
            "amount": "100.00",
            "fee_rate": "0.01",
            "trade_date": "2026-07-30",
            "shares": "999999",
            "confirmation_nav": "999999",
        },
    )

    assert response.status_code == 200
    preview = response.get_json()["preview"]
    assert preview["normalized_input"] == {
        "amount": "100",
        "fee_rate": "0.01",
        "kind": "purchase",
        "note": "",
        "product_id": "cash",
        "source_cash_product_id": "",
        "trade_date": "2026-07-30",
    }
    assert preview["fee"] == "1"
    assert preview["status_prediction"] == "confirmed"
    assert preview["source_impact"] == "external_cash:-100"
    assert preview["destination_impact"] == "cash:+100"
    assert "warning" in preview


def test_wallet_plus_redemption_preview_confirms_same_day(api_setup):
    from src.portfolio_wallet import WALLET_CODE, WALLET_NAME, WALLET_PROVIDER

    client, _, repository, _ = api_setup
    repository.add_product(
        Product(
            id="wallet",
            provider=WALLET_PROVIDER,
            code=WALLET_CODE,
            name=WALLET_NAME,
            product_type=ProductType.CASH_MANAGEMENT,
        )
    )
    repository.create_transaction(
        Transaction(
            id="opening:wallet",
            product_id="wallet",
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 1),
            idempotency_key="opening:wallet",
            amount=Decimal("1000"),
            shares=Decimal("1000"),
            confirmation_nav=Decimal("1"),
            confirmation_date=date(2026, 7, 1),
        )
    )
    PositionProjector(repository).rebuild("wallet")

    response = client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="preview-wallet-plus-redemption"),
        json={
            "kind": "redemption",
            "product_id": "wallet",
            "shares": "400",
            "trade_time": "2026-08-18T10:54:00",
        },
    )

    assert response.status_code == 200
    preview = response.get_json()["preview"]
    assert preview["status_prediction"] == "confirmed"
    assert preview["normalized_input"]["trade_date"] == "2026-08-18"
    assert preview["normalized_input"]["settlement_date"] == "2026-08-18"
    assert preview["normalized_input"]["expected_confirmation_date"] == "2026-08-18"
    assert preview["warning"] == ""


def test_transaction_preview_uses_entered_time_for_cutoff_and_confirmation(
    client,
):
    response = client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="preview-timed-purchase"),
        json={
            "kind": "purchase",
            "product_id": "fund",
            "amount": "100",
            "fee_rate": "0",
            "trade_time": "2026-07-30T15:01",
        },
    )

    assert response.status_code == 200
    normalized = response.get_json()["preview"]["normalized_input"]
    assert normalized["trade_time"] == "2026-07-30T15:01:00"
    assert normalized["trade_date"] == "2026-07-31"
    assert normalized["expected_confirmation_date"] == "2026-08-03"


def test_create_recomputes_preview_and_ignores_client_share_and_nav(client):
    response = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="server-recompute"),
        json={
            "kind": "purchase",
            "product_id": "cash",
            "amount": "100",
            "fee_rate": "0.01",
            "trade_date": "2026-07-30",
            "shares": "999999",
            "confirmation_nav": "999999",
        },
    )

    assert response.status_code == 201
    transaction = response.get_json()["transaction"]
    assert transaction["shares"] == "100"
    assert transaction["confirmation_nav"] == "1"


def test_write_rate_limit_is_30_per_minute(client):
    for index in range(30):
        response = client.post(
            "/api/portfolio/transactions/preview",
            headers=write_headers(idem=f"preview-{index}"),
            json={"kind": "purchase", "product_id": "cash", "amount": "1"},
        )
        assert response.status_code == 200
    response = client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="preview-31"),
        json={"kind": "purchase", "product_id": "cash", "amount": "1"},
    )
    assert response.status_code == 429
    assert response.get_json()["error"] == "rate_limit_exceeded"


def test_rotating_private_token_immediately_invalidates_old_token(
    client, token_file
):
    assert (
        client.get(
            "/api/portfolio", headers={"X-Nav-Dashboard-Key": "secret"}
        ).status_code
        == 200
    )
    token_file.write_text("rotated-secret", encoding="utf-8")
    assert (
        client.get(
            "/api/portfolio", headers={"X-Nav-Dashboard-Key": "secret"}
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/portfolio",
            headers={"X-Nav-Dashboard-Key": "rotated-secret"},
        ).status_code
        == 200
    )


def test_product_preview_resolves_identity_and_valid_quote(api_setup):
    client, _, _, provider = api_setup
    response = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="product-preview"),
        json={
            "provider": "test",
            "code": "003103",
            "name": "长盛盛裕纯债A",
            "wealth_type": "public_fund",
        },
    )

    assert response.status_code == 200
    preview = response.get_json()["preview"]
    assert preview["product"]["registration_code"] == "fund-registration"
    assert preview["quote"]["unit_nav"] == "1.0321"
    assert preview["identity_fingerprint"]
    assert provider.resolve_calls == 1
    start, end = provider.fetch_calls[0]
    assert (end - start).days >= 14


def test_product_preview_accepts_latest_friday_quote_on_weekend(
    api_setup, monkeypatch
):
    client, _, _, provider = api_setup

    monkeypatch.setattr(
        "src.portfolio_api._today",
        lambda: date(2026, 8, 2),
    )
    provider.quote_date = date(2026, 7, 31)
    response = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="weekend-preview"),
        json={
            "provider": "test",
            "code": "003103",
            "name": "长盛盛裕纯债A",
            "wealth_type": "public_fund",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["preview"]["quote"]["quote_date"] == "2026-07-31"
    assert provider.fetch_calls[-1] == (
        date(2026, 7, 19),
        date(2026, 8, 2),
    )


def test_product_create_requires_previewed_complete_identity(api_setup):
    client, _, repository, _ = api_setup
    body = {
        "provider": "test",
        "code": "003103",
        "name": "长盛盛裕纯债A",
        "wealth_type": "public_fund",
    }
    preview = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="identity-preview"),
        json=body,
    ).get_json()["preview"]

    missing = client.post(
        "/api/portfolio/products",
        headers=write_headers(idem="identity-missing"),
        json=body,
    )
    created = client.post(
        "/api/portfolio/products",
        headers=write_headers(idem="identity-create"),
        json={
            **body,
            "identity": preview["product"],
            "identity_fingerprint": preview["identity_fingerprint"],
            "registration_code": preview["product"]["registration_code"],
        },
    )

    assert missing.status_code == 400
    assert created.status_code == 201
    product = repository.get_product_by_provider_code("test", "003103")
    assert product is not None
    assert repository.latest_quote(product.id).quote_date == date.today()


def test_product_create_resolves_again_rejects_identity_drift_and_audits(
    api_setup
):
    client, _, repository, provider = api_setup
    body = {
        "provider": "test",
        "code": "003103",
        "name": "长盛盛裕纯债A",
        "wealth_type": "public_fund",
    }
    preview = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="product-preview"),
        json=body,
    ).get_json()["preview"]
    provider.resolved = MarketProduct(
        provider="test",
        code="003103",
        name="被替换的产品",
        product_type=ProductType.PUBLIC_FUND,
        registration_code="different-registration",
    )

    response = client.post(
        "/api/portfolio/products",
        headers=write_headers(idem="product-create"),
        json={
            **body,
            "identity": preview["product"],
            "identity_fingerprint": preview["identity_fingerprint"],
            "registration_code": preview["product"]["registration_code"],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "validation_error"
    assert repository.get_product_by_provider_code("test", "003103") is None
    with repository.database.connection() as conn:
        audit = conn.execute(
            "SELECT after_json, result FROM audit_logs "
            "WHERE action = 'portfolio_product_create'"
        ).fetchone()
    assert audit["result"] == "rejected"
    assert "secret" not in audit["after_json"]


def test_product_disable_preview_and_submit_need_only_product_id(api_setup):
    client, _, repository, _ = api_setup
    body = {"operation": "disable", "product_id": "fund"}

    preview = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="disable-same-key"),
        json=body,
    )
    submit = client.post(
        "/api/portfolio/products",
        headers=write_headers(idem="disable-same-key"),
        json=body,
    )

    assert preview.status_code == 200
    assert submit.status_code == 200
    assert repository.require_product("fund").status.value == "inactive"


def test_product_preview_rejects_new_cash_management_products(api_setup):
    client, _, _, _ = api_setup

    response = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="reject-real-cash"),
        json={
            "provider": "test",
            "code": "NEW-CASH",
            "product_type": "cash_management",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "系统仅支持钱包Plus现金产品"


def test_provider_failure_is_json_audited_and_creates_no_partial_product(
    api_setup
):
    client, _, repository, provider = api_setup
    provider.failure = ProviderError("official provider unavailable")
    response = client.post(
        "/api/portfolio/products",
        headers=write_headers(idem="provider-failure"),
        json={
            "provider": "test",
            "code": "003103",
            "name": "长盛盛裕纯债A",
            "wealth_type": "public_fund",
            "identity": {
                "provider": "test",
                "code": "003103",
                "name": "长盛盛裕纯债A",
                "product_type": "public_fund",
                "registration_code": "fund-registration",
            },
            "identity_fingerprint": "stale",
            "registration_code": "fund-registration",
        },
    )

    assert response.status_code == 502
    assert response.is_json
    assert response.get_json()["error"] == "provider_error"
    assert repository.get_product_by_provider_code("test", "003103") is None
    with repository.database.connection() as conn:
        audit = conn.execute(
            "SELECT result, after_json FROM audit_logs "
            "WHERE action = 'portfolio_product_create'"
        ).fetchone()
    assert audit["result"] == "rejected"
    assert "secret" not in audit["after_json"]


def test_product_write_is_concurrently_atomic_and_restart_replayable(api_setup):
    _, runtime, repository, provider = api_setup
    body = {
        "provider": "test",
        "code": "003103",
        "name": "长盛盛裕纯债A",
        "wealth_type": "public_fund",
    }
    preview_app = Flask("preview")
    preview_app.register_blueprint(
        create_portfolio_blueprint(runtime, lambda _name: provider)
    )
    preview = preview_app.test_client().post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="concurrent-preview"),
        json=body,
    ).get_json()["preview"]
    submit_body = {
        **body,
        "identity": preview["product"],
        "identity_fingerprint": preview["identity_fingerprint"],
        "registration_code": preview["product"]["registration_code"],
    }

    def submit():
        app = Flask(str(id(object())))
        app.register_blueprint(
            create_portfolio_blueprint(runtime, lambda _name: provider)
        )
        response = app.test_client().post(
            "/api/portfolio/products",
            headers=write_headers(idem="concurrent-product"),
            json=submit_body,
        )
        return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: submit(), range(2)))

    assert results[0] == results[1]
    assert results[0][0] == 201
    assert len(repository.list_products()) == 3
    with repository.database.connection() as conn:
        operation = conn.execute(
            "SELECT result, after_json FROM audit_logs "
            "WHERE action = 'api_idempotency'"
        ).fetchone()
    assert operation["result"] == "success"
    assert '"status":201' in operation["after_json"]


def test_product_audit_failure_rolls_back_product_and_quote(
    api_setup, monkeypatch
):
    client, _, repository, _ = api_setup
    body = {
        "provider": "test",
        "code": "003103",
        "name": "长盛盛裕纯债A",
        "wealth_type": "public_fund",
    }
    preview = client.post(
        "/api/portfolio/products/preview",
        headers=write_headers(idem="audit-preview"),
        json=body,
    ).get_json()["preview"]
    monkeypatch.setattr(
        repository,
        "append_audit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("audit unavailable")
        ),
    )

    response = client.post(
        "/api/portfolio/products",
        headers=write_headers(idem="audit-failure"),
        json={
            **body,
            "identity": preview["product"],
            "identity_fingerprint": preview["identity_fingerprint"],
            "registration_code": preview["product"]["registration_code"],
        },
    )

    assert response.status_code == 500
    assert response.is_json
    assert repository.get_product_by_provider_code("test", "003103") is None
    with repository.database.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


def test_all_transaction_and_sip_write_routes_are_mapped(api_setup):
    client, _, repository, _ = api_setup
    purchase = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="purchase-pending"),
        json={
            "kind": "purchase",
            "product_id": "fund",
            "amount": "10",
            "trade_date": "2026-07-30",
        },
    ).get_json()["transaction"]
    cancel = client.post(
        f"/api/portfolio/transactions/{purchase['id']}/cancel",
        headers=write_headers(idem="cancel-pending"),
        json={},
    )
    assert cancel.status_code == 200

    confirmed = client.post(
        "/api/portfolio/transactions",
        headers=write_headers(idem="purchase-confirmed"),
        json={
            "kind": "purchase",
            "product_id": "cash",
            "amount": "10",
            "trade_date": "2026-07-30",
        },
    ).get_json()["transaction"]
    reverse = client.post(
        f"/api/portfolio/transactions/{confirmed['id']}/reverse",
        headers=write_headers(idem="reverse-confirmed"),
        json={"reason": "correction"},
    )
    assert reverse.status_code == 200

    adjust = client.post(
        "/api/portfolio/positions/cash/adjust",
        headers=write_headers(idem="adjust-cash"),
        json={
            "actual_shares": "900",
            "effective_date": "2026-07-30",
            "reason": "reconciliation",
        },
    )
    assert adjust.status_code == 200

    plan = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-create"),
        json={
            "product_id": "fund",
            "daily_amount": "10",
            "purchase_fee_rate": "0",
            "source_cash_product_id": "cash",
            "start_date": "2026-07-30",
        },
    )
    assert plan.status_code == 201
    plan_id = plan.get_json()["sip_plan"]["id"]
    assert repository.get_plan(plan_id).status.value == "draft"
    assert client.post(
        f"/api/portfolio/sip-plans/{plan_id}/resume",
        headers=write_headers(idem="sip-resume"),
        json={},
    ).status_code == 400


def test_sip_create_defaults_to_draft_and_edits_existing_plan_in_place(api_setup):
    client, _, repository, _ = api_setup
    create_body = {
        "product_id": "fund",
        "daily_amount": "10",
        "purchase_fee_rate": "0",
        "source_cash_product_id": "cash",
        "start_date": "2026-07-30",
    }
    created = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-draft"),
        json=create_body,
    )
    plan_id = created.get_json()["sip_plan"]["id"]
    edited = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-edit"),
        json={**create_body, "sip_id": plan_id, "daily_amount": "20"},
    )

    assert created.status_code == 201
    assert created.get_json()["sip_plan"]["status"] == "draft"
    assert edited.status_code == 200
    assert edited.get_json()["sip_plan"]["id"] == plan_id
    assert repository.get_plan(plan_id).daily_amount == Decimal("20")

    activated = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-activate"),
        json={**create_body, "activate": True},
    )
    active_id = activated.get_json()["sip_plan"]["id"]
    active_edit = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-active-edit"),
        json={
            **create_body,
            "sip_id": active_id,
            "daily_amount": "30",
            "start_date": "2026-07-31",
        },
    )
    assert activated.get_json()["sip_plan"]["status"] == "active"
    assert active_edit.status_code == 200
    assert active_edit.get_json()["sip_plan"]["status"] == "active"
    assert repository.get_plan(active_id).daily_amount == Decimal("30")
    assert repository.get_plan(active_id).start_date == date(2026, 7, 31)

    clear_source = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-active-clear-source"),
        json={
            **create_body,
            "sip_id": active_id,
            "source_cash_product_id": "",
        },
    )
    assert clear_source.status_code == 400

    paused = client.post(
        f"/api/portfolio/sip-plans/{active_id}/pause",
        headers=write_headers(idem="sip-pause"),
        json={},
    )
    paused_edit = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-paused-edit"),
        json={
            **create_body,
            "sip_id": active_id,
            "daily_amount": "40",
            "activate": True,
        },
    )
    assert paused.status_code == 200
    assert paused_edit.status_code == 200
    assert paused_edit.get_json()["sip_plan"]["status"] == "paused"
    assert repository.get_plan(active_id).daily_amount == Decimal("40")


def test_sip_api_round_trips_weekly_schedule(api_setup):
    client, _, repository, _ = api_setup
    body = {
        "product_id": "fund",
        "daily_amount": "100",
        "purchase_fee_rate": "0",
        "source_cash_product_id": "cash",
        "start_date": "2026-09-01",
        "frequency": "weekly",
        "schedule_day": 5,
    }

    preview = client.post(
        "/api/portfolio/sip-plans/preview",
        headers=write_headers(idem="weekly-preview"),
        json=body,
    )
    created = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="weekly-create"),
        json=body,
    )

    assert preview.status_code == 200
    assert preview.get_json()["preview"]["frequency"] == "weekly"
    assert preview.get_json()["preview"]["schedule_day"] == 5
    assert created.status_code == 201
    payload = created.get_json()["sip_plan"]
    assert payload["frequency"] == "weekly"
    assert payload["schedule_day"] == 5
    plan = repository.get_plan(payload["id"])
    assert plan.frequency is SipFrequency.WEEKLY
    assert plan.schedule_day == 5


def test_sip_api_treats_omitted_frequency_as_legacy_daily(api_setup):
    """旧客户端不发送 frequency/schedule_day 时按每日定投处理。"""
    client, _, repository, _ = api_setup
    body = {
        "product_id": "fund",
        "daily_amount": "100",
        "purchase_fee_rate": "0",
        "source_cash_product_id": "cash",
        "start_date": "2026-09-01",
    }

    preview = client.post(
        "/api/portfolio/sip-plans/preview",
        headers=write_headers(idem="legacy-preview"),
        json=body,
    )
    created = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="legacy-create"),
        json=body,
    )

    assert preview.status_code == 200
    assert preview.get_json()["preview"]["frequency"] == "daily"
    assert preview.get_json()["preview"]["schedule_day"] is None
    assert created.status_code == 201
    payload = created.get_json()["sip_plan"]
    assert payload["frequency"] == "daily"
    assert payload["schedule_day"] is None
    plan = repository.get_plan(payload["id"])
    assert plan.frequency is SipFrequency.DAILY
    assert plan.schedule_day is None


def _sip_purchase_count(repository, plan_id):
    return len([
        t for t in repository.list_transactions()
        if t.plan_id == plan_id
        and t.transaction_type is TransactionType.SIP_PURCHASE
    ])


def _activate_monthly(api_setup, monkeypatch, clock, idem):
    client, runtime, repository, _ = api_setup
    monkeypatch.setattr(
        "src.portfolio_api.beijing_now",
        lambda: datetime.combine(clock, datetime.min.time()),
    )
    monkeypatch.setattr("src.portfolio_sip.beijing_today", lambda: clock)
    resp = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem=idem),
        json={
            "product_id": "fund",
            "daily_amount": "100",
            "purchase_fee_rate": "0",
            "source_cash_product_id": "cash",
            "start_date": "2026-09-01",
            "frequency": "monthly",
            "schedule_day": 15,
            "activate": True,
        },
    )
    assert resp.status_code in (200, 201), resp.get_json()
    return client, runtime, repository, resp.get_json()["sip_plan"]["id"]


def test_active_plan_schedule_change_does_not_replay_history(
    api_setup, monkeypatch
):
    """活动计划改周期：不得按新规则追补历史，只从修改后次个执行日生效。"""
    from src.portfolio_positions import PositionProjector
    from src.portfolio_sip import SipService
    from src.portfolio_transactions import PortfolioTransactionService

    client, runtime, repository, _ = api_setup

    def set_clock(day):
        monkeypatch.setattr(
            "src.portfolio_api.beijing_now",
            lambda: datetime.combine(day, datetime.min.time()),
        )
        monkeypatch.setattr("src.portfolio_sip.beijing_today", lambda: day)

    set_clock(date(2026, 10, 1))
    created = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sch-create"),
        json={
            "product_id": "fund", "daily_amount": "100",
            "purchase_fee_rate": "0", "source_cash_product_id": "cash",
            "start_date": "2026-09-01", "frequency": "monthly",
            "schedule_day": 15, "activate": True,
        },
    )
    assert created.status_code in (200, 201)
    plan_id = created.get_json()["sip_plan"]["id"]
    # 激活补跑：每月15日 → 9-15 一次。
    assert _sip_purchase_count(repository, plan_id) == 1

    # 修改为每月20日（同日 10-01 保存）。
    modified = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sch-modify"),
        json={
            "sip_id": plan_id,
            "product_id": "fund", "daily_amount": "100",
            "purchase_fee_rate": "0", "source_cash_product_id": "cash",
            "start_date": "2026-09-01", "frequency": "monthly",
            "schedule_day": 20,
        },
    )
    assert modified.status_code == 200
    # 关键：不得按新规则追补历史（旧 bug 会补扣 9/21）。
    assert _sip_purchase_count(repository, plan_id) == 1
    plan = repository.get_plan(plan_id)
    from datetime import timedelta
    assert plan.schedule_effective_date == date(2026, 10, 1) + timedelta(days=1)

    # 新规则从"修改后的下一个执行日"起生效：作业推进到 10-21 时，
    # 应产生 10-20（而非更早的 9/20）。
    sip = SipService(
        repository, PositionProjector(repository),
        PortfolioTransactionService(
            repository, PositionProjector(repository)
        ),
    )
    sip.backfill_plan(plan_id, date(2026, 10, 21), settle=False)
    executed = [
        e.intended_trade_date for e in repository.list_plan_executions(plan_id)
        if e.status != "skipped"
    ]
    assert date(2026, 9, 15) in executed
    assert date(2026, 10, 20) in executed
    assert date(2026, 9, 20) not in executed  # 未追补


def test_schedule_change_with_future_start_executes_nothing_before_start(
    api_setup, monkeypatch
):
    """活动计划改周期且开始日期在未来时，开始日前不得产生执行。"""
    from src.portfolio_positions import PositionProjector
    from src.portfolio_sip import SipService
    from src.portfolio_transactions import PortfolioTransactionService

    client, runtime, repository, _ = api_setup
    monkeypatch.setattr(
        "src.portfolio_api.beijing_now",
        lambda: datetime.combine(date(2026, 11, 1), datetime.min.time()),
    )
    monkeypatch.setattr("src.portfolio_sip.beijing_today", lambda: date(2026, 11, 1))

    created = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="fut-create"),
        json={
            "product_id": "fund", "daily_amount": "100",
            "purchase_fee_rate": "0", "source_cash_product_id": "cash",
            "start_date": "2026-12-01", "frequency": "weekly",
            "schedule_day": 1, "activate": True,
        },
    )
    assert created.status_code in (200, 201)
    plan_id = created.get_json()["sip_plan"]["id"]
    # 改成每周三 → schedule_effective=11-02，但仍早于 start_date 12-01。
    client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="fut-modify"),
        json={
            "sip_id": plan_id,
            "product_id": "fund", "daily_amount": "100",
            "purchase_fee_rate": "0", "source_cash_product_id": "cash",
            "start_date": "2026-12-01", "frequency": "weekly",
            "schedule_day": 3,
        },
    )
    plan = repository.get_plan(plan_id)
    assert plan.start_date == date(2026, 12, 1)
    assert plan.schedule_effective_date == date(2026, 11, 2)

    # 作业推进到 11-30（仍在 start_date 之前）→ 不得产生任何执行。
    sip = SipService(
        repository, PositionProjector(repository),
        PortfolioTransactionService(repository, PositionProjector(repository)),
    )
    executions = sip.backfill_plan(plan_id, date(2026, 11, 30), settle=False)
    assert executions == []
    assert _sip_purchase_count(repository, plan_id) == 0


def test_main_write_resume_does_not_catch_up_pause_window(
    api_setup, monkeypatch
):
    client, runtime, repository, plan_id = _activate_monthly(
        api_setup, monkeypatch, date(2026, 9, 16), "mw-resume-create"
    )
    # 激活时补跑已产生 9-15 一次扣款。
    assert _sip_purchase_count(repository, plan_id) == 1

    # 暂停，并把暂停起点设到北京时间 10-01；暂停期间作业一次都没跑。
    client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="mw-resume-pause"),
        json={"operation": "pause", "sip_id": plan_id},
    )
    with runtime.repository.database.transaction() as conn:
        conn.execute(
            "UPDATE sip_plans SET paused_at = ? WHERE id = ?",
            ("2026-09-30 16:00:00", plan_id),
        )

    # 时钟前进到 11-01 后，通过主写接口恢复。
    monkeypatch.setattr(
        "src.portfolio_api.beijing_now",
        lambda: datetime.combine(
            date(2026, 11, 1), datetime.min.time()
        ),
    )
    monkeypatch.setattr("src.portfolio_sip.beijing_today", lambda: date(2026, 11, 1))
    resp = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="mw-resume-do"),
        json={"operation": "resume", "sip_id": plan_id},
    )
    assert resp.status_code == 200
    assert resp.get_json()["sip_plan"]["status"] == "active"

    # 10-15 被 resume 关闭为 plan_paused，且不补扣。
    assert _sip_purchase_count(repository, plan_id) == 1
    exec_by_date = {
        e.intended_trade_date: e
        for e in repository.list_plan_executions(plan_id)
    }
    assert exec_by_date[date(2026, 10, 15)].status == "skipped"
    assert exec_by_date[date(2026, 10, 15)].reason == "plan_paused"


def test_dedicated_resume_endpoint_does_not_catch_up_pause_window(
    api_setup, monkeypatch
):
    client, runtime, repository, plan_id = _activate_monthly(
        api_setup, monkeypatch, date(2026, 9, 16), "ep-resume-create"
    )
    assert _sip_purchase_count(repository, plan_id) == 1

    client.post(
        f"/api/portfolio/sip-plans/{plan_id}/pause",
        headers=write_headers(idem="ep-resume-pause"),
        json={},
    )
    with runtime.repository.database.transaction() as conn:
        conn.execute(
            "UPDATE sip_plans SET paused_at = ? WHERE id = ?",
            ("2026-09-30 16:00:00", plan_id),
        )

    monkeypatch.setattr("src.portfolio_sip.beijing_today", lambda: date(2026, 11, 1))
    resp = client.post(
        f"/api/portfolio/sip-plans/{plan_id}/resume",
        headers=write_headers(idem="ep-resume-do"),
        json={},
    )
    assert resp.status_code == 200
    assert resp.get_json()["sip_plan"]["status"] == "active"
    assert _sip_purchase_count(repository, plan_id) == 1
    exec_by_date = {
        e.intended_trade_date: e
        for e in repository.list_plan_executions(plan_id)
    }
    assert exec_by_date[date(2026, 10, 15)].reason == "plan_paused"


@pytest.mark.parametrize(
    "schedule",
    [
        {"frequency": "weekly", "schedule_day": 6},
        {"frequency": "monthly", "schedule_day": 32},
        {"frequency": "daily", "schedule_day": 1},
    ],
)
def test_sip_api_rejects_invalid_schedule(api_setup, schedule):
    client, _, _repository, _ = api_setup
    response = client.post(
        "/api/portfolio/sip-plans/preview",
        headers=write_headers(
            idem=f"invalid-{schedule['frequency']}-{schedule['schedule_day']}"
        ),
        json={
            "product_id": "fund",
            "daily_amount": "100",
            "purchase_fee_rate": "0",
            "source_cash_product_id": "cash",
            "start_date": "2026-09-01",
            **schedule,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "validation_error"
    assert response.get_json()["message"] == "invalid SIP schedule"


def test_sip_web_fee_rate_is_a_percentage_not_a_decimal_fraction(
    api_setup,
):
    client, _, repository, _ = api_setup

    response = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-percent-fee"),
        json={
            "product_id": "fund",
            "daily_amount": "2000",
            "fee_rate": "0.006",
            "source_cash_product_id": "cash",
            "start_date": "2026-07-30",
        },
    )

    payload = response.get_json()
    plan = repository.get_plan(payload["sip_plan"]["id"])
    assert response.status_code == 201
    assert payload["preview"]["purchase_fee_rate"] == "0.00006"
    assert payload["preview"]["purchase_fee_rate_percent"] == "0.006"
    assert plan.purchase_fee_rate == Decimal("0.00006")


def test_sip_plan_delete_hides_schedule_but_preserves_archived_record(
    api_setup,
):
    client, _, repository, _ = api_setup
    created = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-create-for-delete"),
        json={
            "product_id": "fund",
            "daily_amount": "10",
            "purchase_fee_rate": "0",
            "source_cash_product_id": "cash",
            "start_date": "2026-07-30",
        },
    )
    plan_id = created.get_json()["sip_plan"]["id"]
    delete_body = {"operation": "delete", "sip_id": plan_id}

    preview = client.post(
        "/api/portfolio/sip-plans/preview",
        headers=write_headers(idem="sip-delete-preview"),
        json=delete_body,
    )
    deleted = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-delete"),
        json=delete_body,
    )
    retried = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-delete"),
        json=delete_body,
    )

    assert preview.status_code == 200
    assert deleted.status_code == 200
    assert retried.status_code == 200
    assert repository.get_plan(plan_id) is None
    assert repository.get_plan(plan_id, include_deleted=True) is not None
    assert all(plan.id != plan_id for plan in repository.list_plans())


def test_active_backdated_sip_immediately_updates_confirmed_holdings(
    api_setup,
    monkeypatch,
):
    client, _, repository, _ = api_setup
    repository.upsert_quote(
        "fund",
        MarketQuote(
            "fund",
            date(2026, 7, 30),
            "official",
            "fund:2026-07-30",
            unit_nav=Decimal("1"),
        ),
        "2026-07-30T18:00:00+08:00",
    )
    monkeypatch.setattr(
        "src.portfolio_api.beijing_now",
        lambda: datetime(2026, 7, 31, 12),
    )

    response = client.post(
        "/api/portfolio/sip-plans",
        headers=write_headers(idem="sip-backdated"),
        json={
            "product_id": "fund",
            "daily_amount": "100",
            "purchase_fee_rate": "0",
            "source_cash_product_id": "cash",
            "start_date": "2026-07-30",
            "activate": True,
        },
    )

    assert response.status_code == 201
    assert repository.get_position("fund").total_shares == Decimal("100")


def test_mobile_compatibility_preview_and_adjustment_routes_are_not_404(client):
    cases = (
        (
            "/api/portfolio/sip-plans/preview",
            {
                "product_id": "fund",
                "amount": "10",
                "fee_rate": "0",
                "source": "cash",
            },
        ),
        (
            "/api/portfolio/positions/adjustments/preview",
            {"product_id": "cash", "shares": "900", "note": "count"},
        ),
        (
            "/api/portfolio/positions/adjustments",
            {"product_id": "cash", "shares": "900", "note": "count"},
        ),
    )
    for index, (path, body) in enumerate(cases):
        response = client.post(
            path,
            headers=write_headers(idem=f"mobile-{index}"),
            json=body,
        )
        assert response.status_code != 404


def test_position_adjustment_accepts_shares_and_profit_without_note(api_setup):
    client, _, repository, _ = api_setup

    response = client.post(
        "/api/portfolio/positions/adjustments",
        headers=write_headers(idem="adjust-shares-profit"),
        json={
            "product_id": "cash",
            "shares": "900",
            "profit": "12.34",
            "effective_date": "2026-07-30",
        },
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["preview"]["actual_profit"] == "12.34"
    assert payload["profit_transaction"]["transaction_type"] == (
        "profit_adjustment"
    )
    assert repository.get_position("cash").total_shares == Decimal("900")


def test_latest_profit_adjustment_updates_profits_without_note(api_setup):
    client, _, repository, _ = api_setup
    repository.create_transaction(
        Transaction(
            id="opening:fund",
            product_id="fund",
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 8, 3),
            confirmation_date=date(2026, 8, 3),
            idempotency_key="opening:fund",
            amount=Decimal("100"),
            shares=Decimal("100"),
        )
    )
    for quote_date, nav in (
        (date(2026, 8, 3), "1"),
        (date(2026, 8, 4), "1.1"),
    ):
        repository.upsert_quote(
            "fund",
            MarketQuote(
                "fund",
                quote_date,
                "official",
                f"fund:{quote_date}",
                unit_nav=Decimal(nav),
            ),
            f"{quote_date}T18:00:00+08:00",
        )
    PositionProjector(repository).rebuild()

    response = client.post(
        "/api/portfolio/latest-profit-adjustments",
        headers=write_headers(idem="adjust-latest-profit"),
        json={
            "product_id": "fund",
            "profit": "7.50",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["preview"]["latest_profit_date"] == "2026-08-04"
    assert payload["preview"]["current_latest_profit"] == "10"
    assert payload["preview"]["profit_difference"] == "-2.5"
    assert payload["transaction"]["transaction_type"] == (
        "latest_profit_adjustment"
    )

    portfolio = client.get(
        "/api/portfolio",
        headers={"X-Nav-Dashboard-Key": "secret"},
    ).get_json()
    fund = next(row for row in portfolio["products"] if row["id"] == "fund")
    assert fund["latest_profit"] == "7.5"
    assert fund["holding_profit"] == "7.5"


def test_latest_profit_adjustment_rejects_a_historical_date(api_setup):
    client, _, repository, _ = api_setup
    repository.create_transaction(
        Transaction(
            id="income:cash:latest",
            product_id="cash",
            transaction_type=TransactionType.INCOME_ACCRUAL,
            status=TransactionStatus.CONFIRMED,
            trade_date=date(2026, 7, 30),
            confirmation_date=date(2026, 7, 30),
            idempotency_key="income:cash:latest",
            amount=Decimal("1.25"),
            shares=Decimal("1.25"),
        )
    )
    PositionProjector(repository).rebuild()

    response = client.post(
        "/api/portfolio/latest-profit-adjustments/preview",
        headers=write_headers(idem="historical-latest-profit"),
        json={
            "product_id": "cash",
            "profit": "0",
            "effective_date": "2026-07-29",
            "note": "不允许改历史收益",
        },
    )

    assert response.status_code == 400
    assert "latest disclosed date" in response.get_json()["message"]


def test_latest_profit_adjustment_uses_api_beijing_date_on_submit(
    api_setup, monkeypatch
):
    client, _, repository, _ = api_setup
    opening_date = date(2099, 1, 1)
    latest_date = date(2099, 1, 2)
    repository.create_transaction(
        Transaction(
            id="opening:future-fund",
            product_id="fund",
            transaction_type=TransactionType.OPENING_POSITION,
            status=TransactionStatus.CONFIRMED,
            trade_date=opening_date,
            confirmation_date=opening_date,
            idempotency_key="opening:future-fund",
            amount=Decimal("100"),
            shares=Decimal("100"),
        )
    )
    for quote_date, nav in ((opening_date, "1"), (latest_date, "1.1")):
        repository.upsert_quote(
            "fund",
            MarketQuote(
                "fund",
                quote_date,
                "official",
                f"future:{quote_date}",
                unit_nav=Decimal(nav),
            ),
            f"{quote_date}T18:00:00+08:00",
        )
    PositionProjector(repository).rebuild()
    monkeypatch.setattr("src.portfolio_api._today", lambda: latest_date)

    response = client.post(
        "/api/portfolio/latest-profit-adjustments",
        headers=write_headers(idem="adjust-future-latest-profit"),
        json={
            "product_id": "fund",
            "profit": "8",
            "note": "北京时间日期校准",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["preview"]["latest_profit_date"] == (
        latest_date.isoformat()
    )
