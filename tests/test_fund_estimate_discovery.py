from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import threading

from src.beijing_time import TZ_CN
from src.fund_estimate_service import discover_funds
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import FundIdentity, ProfileVersion
from src.portfolio_db import SCHEMA_VERSION, PortfolioDatabase
from src.portfolio_models import (
    Position,
    Product,
    ProductType,
    Transaction,
    TransactionStatus,
    TransactionType,
)
from src.portfolio_repository import PortfolioRepository


def ledger(tmp_path):
    database = PortfolioDatabase(tmp_path / "portfolio.db")
    database.initialize()
    return PortfolioRepository(database)


def dump_ledger(repository):
    with repository.database.connection() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1"
            )
        ]
        payload = {}
        for name in tables:
            payload[name] = [tuple(row) for row in conn.execute(f"SELECT * FROM {name}")]
    return payload


def add_fund(repository, product_id, code, name, shares="0", status=None, fund_type="债券型"):
    repository.add_product(
        Product(
            product_id,
            "eastmoney_fund",
            code,
            name,
            ProductType.PUBLIC_FUND,
            metadata_json=json.dumps({"fund_type": fund_type}, ensure_ascii=False),
        )
    )
    if Decimal(shares) > 0:
        repository.replace_position(
            Position(product_id, Decimal(shares), Decimal("0"), Decimal(shares), Decimal("1"))
        )
    if status is not None:
        repository.create_transaction(
            Transaction(
                f"tx-{product_id}-{status.value}",
                product_id,
                TransactionType.MANUAL_PURCHASE,
                status,
                date(2026, 10, 23),
                f"idem-{product_id}-{status.value}",
            )
        )


def profile(version, published_at, duration):
    identity = FundIdentity("p-new", "918273", "eastmoney_fund", "A", None, "债券型")
    return ProfileVersion(
        profile_version=version,
        identity=identity,
        report_as_of="2026-06-30",
        published_at=published_at,
        fetched_at=published_at,
        source_url="https://example.test/report",
        document_hash=version,
        fields={"duration_nav": duration, "fee_schedule": []},
        evidence=[{"field": "duration_nav", "quote": f"净值口径久期：{duration}年", "unit": "年", "page": 1}],
        status="ok",
    )


def test_arbitrary_new_code_is_discovered_without_whitelist(tmp_path):
    repository = ledger(tmp_path)
    add_fund(repository, "p1", "918273", "合成纯债A", shares="10")
    add_fund(repository, "p2", "564738", "另一只中短债C", shares="2")
    repository.add_product(Product("wealth", "bank", "AF233276B", "理财", ProductType.WEALTH_NAV))
    repository.replace_position(Position("wealth", Decimal("1"), Decimal("0"), Decimal("1"), Decimal("1")))

    found = {(item.code, item.share_class) for item in discover_funds(repository, datetime(2026, 10, 23, tzinfo=TZ_CN))}
    source = (Path(__file__).resolve().parents[1] / "src" / "fund_estimate_service.py").read_text(encoding="utf-8")

    assert found == {("918273", "A"), ("564738", "C")}
    assert "918273" not in source


def test_pending_purchase_is_discovered_but_cancelled_is_not(tmp_path):
    repository = ledger(tmp_path)
    add_fund(repository, "pending", "102938", "待确认纯债A", status=TransactionStatus.PENDING_CONFIRMATION)
    add_fund(repository, "quote", "564710", "报价中纯债C", status=TransactionStatus.PENDING_QUOTE)
    add_fund(repository, "cancelled", "675849", "已取消纯债A", status=TransactionStatus.CANCELLED)
    add_fund(repository, "failed", "786950", "失败纯债A", status=TransactionStatus.FAILED)
    add_fund(repository, "cleared", "897061", "已清仓纯债A", shares="0")

    codes = {item.code for item in discover_funds(repository, datetime(2026, 10, 23, tzinfo=TZ_CN))}

    assert codes == {"102938", "564710"}


def test_estimate_storage_does_not_mutate_ledger(tmp_path):
    repository = ledger(tmp_path)
    add_fund(repository, "p1", "918273", "合成纯债A", shares="3")
    before = dump_ledger(repository)
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    discover_funds(repository, datetime(2026, 10, 23, tzinfo=TZ_CN))
    store.publish_profile(profile("v1", "2026-08-28T08:00:00+08:00", "3"))
    after = dump_ledger(repository)

    assert SCHEMA_VERSION == 7
    assert after == before
    assert store.path.name == "fund_estimation.db"


def test_newer_version_survives_overlapping_publish(tmp_path):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    newer = profile("new", "2026-10-01T08:00:00+08:00", "3")
    older = profile("old", "2026-08-01T08:00:00+08:00", "9")
    barrier = threading.Barrier(2)

    def publish(item):
        barrier.wait(timeout=5)
        store.publish_profile(item)

    threads = [threading.Thread(target=publish, args=(item,)) for item in (older, newer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    current = store.current_profile("918273", "A")
    assert current.profile_version == "new"
    assert current.fields["duration_nav"] == "3"
    store.publish_profile(older)
    assert store.current_profile("918273", "A").profile_version == "new"
    assert len(store.list_profile_versions("918273", "A")) == 2
    store.record_profile_failure("918273", "A", "ai_extraction_failed", "2026-10-02T00:00:00+08:00")
    assert store.current_profile("918273", "A").fields["duration_nav"] == "3"
