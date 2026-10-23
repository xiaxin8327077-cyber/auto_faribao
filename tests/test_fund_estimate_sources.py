from datetime import date, datetime
from decimal import Decimal

import pytest

from src.beijing_time import TZ_CN
from src.fund_estimate_sources import (
    CHINABOND_EOD,
    EstimateSources,
    IncompleteNavHistory,
    align_southern_basis,
    combine_factor_families,
    factors_from_chinabond_levels,
    total_return_pct,
    visible_nav,
)
from src.fund_estimate_store import iso_now
from src.fund_estimate_types import FACTOR_NAMES, FactorObservation, FundIdentity, MissingDividendCoverage, NavObservation


IDENTITY = FundIdentity("p", "918273", "eastmoney_fund", "A", None, "债券型")


def _row(index):
    day = date(2020, 1, 1).toordinal() + index
    return {
        "nav_date": date.fromordinal(day).isoformat(),
        "unit_nav": "1.0000",
        "source": "fixture",
        "source_version": f"v{index}",
        "available_at": "2026-10-23T18:00:00+08:00",
    }


def test_nav_backfill_exceeds_first_hundred_rows():
    calls = []

    def fetcher(code, start, end, page, page_size):
        calls.append(page)
        assert code == "918273"
        assert page_size == 100
        if page == 1:
            return {"total": 185, "rows": [_row(index) for index in range(100)]}
        if page == 2:
            return {"total": 185, "rows": [_row(index) for index in range(100, 185)]}
        return {"total": 185, "rows": []}

    sources = EstimateSources(
        nav_fetcher=fetcher,
        dividend_fetcher=lambda *_args: {"complete": True, "events": []},
    )
    rows = sources.fetch_nav_history(IDENTITY, date(2020, 1, 1), date(2021, 12, 31))

    assert calls == [1, 2]
    assert len(rows) == 185
    assert rows[0].nav_date < rows[-1].nav_date
    assert len({item.nav_date for item in rows}) == 185


def test_cash_dividend_total_return():
    assert total_return_pct("1.03", "1.02", "0.01") == "0"


def test_missing_dividend_is_not_silently_zero():
    with pytest.raises(MissingDividendCoverage):
        total_return_pct("1.00", "1.01", None)
    sources = EstimateSources(
        nav_fetcher=lambda *_args: {"total": 1, "rows": [_row(0)]},
        dividend_fetcher=lambda *_args: {"complete": False, "events": []},
    )
    rows = sources.fetch_nav_history(IDENTITY, date(2020, 1, 1), date(2020, 1, 2))
    assert rows[0].cash_dividend_per_share is None
    assert rows[0].dividend_coverage == "missing"


def test_cross_family_factors_require_basis_status():
    fetched = "2026-10-23T10:00:00+08:00"
    eod = FactorObservation(
        "bond_factor_v1", "chinabond_eod", "2026-10-23", fetched, fetched, fetched,
        {"gov_3y_bp": "1"}, 1, "native", [], "2026-10-22", "eod",
    )
    intraday = FactorObservation(
        "bond_factor_v1", "southern_intraday", "2026-10-23", fetched, fetched, fetched,
        {"gov_3y_bp": None}, 1, "native", ["gov_3y_bp"], None, "live",
    )
    combined = combine_factor_families([eod, intraday])
    assert combined.basis_status == "unverified"
    assert combined.values_bp["gov_3y_bp"] is None
    assert "gov_3y_bp" in combined.missing_fields
    assert combined.values_bp["gov_3y_bp"] != "0"


def test_later_nav_revision_does_not_leak_into_old_cutoff():
    early = NavObservation("918273", "2026-10-22", "1.0000", "0", "2026-10-22T20:00:00+08:00", "fixture", "v1", "ordinary", "verified_none")
    revised = NavObservation("918273", "2026-10-22", "1.0100", "0", "2026-10-23T20:00:00+08:00", "fixture", "v2", "ordinary", "verified_none")
    visible = visible_nav([early, revised], "2026-10-23T08:00:00+08:00")
    assert visible[0].unit_nav == "1.0000"
    assert visible_nav([early, revised], "2026-10-23T21:00:00+08:00")[0].unit_nav == "1.0100"


def test_nav_backfill_follows_short_pages_when_total_is_absent():
    calls = []

    def fetcher(code, start, end, page, page_size):
        calls.append(page)
        begin = (page - 1) * 20
        if begin >= 45:
            return {"total": None, "rows": []}
        return {"total": None, "rows": [_row(index) for index in range(begin, min(begin + 20, 45))]}

    sources = EstimateSources(
        nav_fetcher=fetcher,
        dividend_fetcher=lambda *_args: {"complete": True, "events": []},
    )
    rows = sources.fetch_nav_history(IDENTITY, date(2020, 1, 1), date(2021, 12, 31))

    assert calls == [1, 2, 3, 4]
    assert len(rows) == 45


def test_short_page_is_not_treated_as_complete_history():
    def fetcher(code, start, end, page, page_size):
        if page == 1:
            return {"total": 185, "rows": [_row(index) for index in range(40)]}
        raise IncompleteNavHistory("缺页")

    sources = EstimateSources(nav_fetcher=fetcher, dividend_fetcher=lambda *_a: {"complete": True, "events": []})
    with pytest.raises(IncompleteNavHistory):
        sources.fetch_nav_history(IDENTITY, date(2020, 1, 1), date(2021, 12, 31))


def _curve(gov3, gov5, gov10, cdb5, aaa3=None):
    curves = {
        "rate_guozhai": {3.0: Decimal(gov3), 5.0: Decimal(gov5), 10.0: Decimal(gov10)},
        "rate_guokai": {5.0: Decimal(cdb5)},
    }
    if aaa3 is not None:
        curves["credit_aaa"] = {3.0: Decimal(aaa3)}
    return curves


def test_chinabond_close_to_close_is_one_bp_and_not_southern_mapping():
    friday = date(2026, 10, 16)
    monday = date(2026, 10, 19)
    calls = []

    def fetch_day(day):
        calls.append(day)
        if day == friday:
            return _curve("1.50", "1.60", "1.80", "1.70", "2.00")
        if day == monday:
            return _curve("1.51", "1.60", "1.82", "1.73", "2.04")
        return {}

    rows = factors_from_chinabond_levels(date(2026, 10, 16), date(2026, 10, 19), fetch_day)
    monday_row = next(item for item in rows if item.as_of == "2026-10-19")

    assert date(2026, 10, 17) not in calls
    assert date(2026, 10, 18) not in calls
    assert monday_row.source_family == CHINABOND_EOD
    assert monday_row.basis_status == "native"
    assert monday_row.basis_date == "2026-10-16"
    assert monday_row.calendar_days == 3
    assert monday_row.values_bp["gov_3y_bp"] == "1"
    assert monday_row.values_bp["gov_slope_10y_3y_bp"] == "1"
    assert monday_row.values_bp["cdb_spread_5y_bp"] == "3"
    assert monday_row.values_bp["credit_aaa_spread_3y_bp"] == "3"
    assert monday_row.basis_status != "verified_mapping"

    missing = factors_from_chinabond_levels(
        friday,
        monday,
        lambda day: _curve("1.50", "1.60", "1.80", "1.70") if day == friday else _curve("1.51", "1.60", "1.82", "1.73"),
    )
    incomplete = next(item for item in missing if item.as_of == "2026-10-19")
    assert incomplete.values_bp["credit_aaa_spread_3y_bp"] is None
    assert "credit_aaa_spread_3y_bp" in incomplete.missing_fields


def test_southern_uses_previous_nav_day_and_stops_when_nav_skips_a_session():
    def weekday(day):
        return day.weekday() < 5

    friday = FactorObservation(
        "bond_factor_v1", "southern_intraday", "2026-10-23", "2026-10-23T12:00:00+08:00",
        "2026-10-23T12:00:00+08:00", "2026-10-23T12:31:00+08:00",
        {"gov_3y_bp": "1"}, 1, "native", [], None, "live",
    )
    aligned = align_southern_basis(friday, "2026-10-22", weekday)
    assert aligned.basis_date == "2026-10-22"
    assert aligned.calendar_days == 1
    skipped = align_southern_basis(friday, "2026-10-21", weekday)
    assert skipped.basis_date is None
