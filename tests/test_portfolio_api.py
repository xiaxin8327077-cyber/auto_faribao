from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import Flask

from src.portfolio_api import create_portfolio_blueprint
from src.portfolio_db import PortfolioDatabase
from src.portfolio_models import (
    MarketProduct,
    MarketQuote,
    Product,
    ProductType,
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

    def resolve_product(self, code):
        self.resolve_calls += 1
        if code.strip() != self.resolved.code:
            raise ValueError("product not found")
        return self.resolved

    def fetch_quotes(self, product, start_date, end_date):
        return [
            MarketQuote(
                product_code=product.code,
                quote_date=TRADE_DATE,
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


def test_transaction_preview_normalizes_server_side_values(client):
    response = client.post(
        "/api/portfolio/transactions/preview",
        headers=write_headers(idem="preview-purchase"),
        json={
            "kind": "purchase",
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
        "product_id": "cash",
        "source_cash_product_id": "",
        "trade_date": "2026-07-30",
    }
    assert preview["fee"] == "1"
    assert preview["status_prediction"] == "confirmed"
    assert preview["source_impact"] == "external_cash:-100"
    assert preview["destination_impact"] == "cash:+100"
    assert "warning" in preview


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
    assert provider.resolve_calls == 1


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
    assert (
        client.post(
            "/api/portfolio/products/preview",
            headers=write_headers(idem="product-preview"),
            json=body,
        ).status_code
        == 200
    )
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
        json=body,
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
    assert repository.get_plan(plan_id) is not None
    assert (
        client.post(
            f"/api/portfolio/sip-plans/{plan_id}/resume",
            headers=write_headers(idem="sip-resume"),
            json={},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/portfolio/sip-plans/{plan_id}/pause",
            headers=write_headers(idem="sip-pause"),
            json={},
        ).status_code
        == 200
    )


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
