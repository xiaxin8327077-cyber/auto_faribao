from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from src.beijing_time import TZ_CN
from src.fund_estimate_models import (
    build_training_set,
    duration_price_return_pct,
    fit_baseline,
    fit_ridge,
    predict,
    project_snapshot,
    train_models,
)
from src.fund_estimate_store import FundEstimateStore
from src.fund_estimate_types import (
    FACTOR_NAMES,
    EstimateInputError,
    FactorObservation,
    FundIdentity,
    NavObservation,
    ProfileVersion,
)


NOW = datetime(2026, 10, 23, 10, 0, tzinfo=TZ_CN)
IDENTITY = FundIdentity("p-918273", "918273", "eastmoney_fund", "A", None, "债券型")


def store_of(tmp_path):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    return store


def nav(day, value, available, dividend="0"):
    return NavObservation("918273", day, value, dividend, available, "fixture", available, "ordinary", "verified_none")


def factor(day, basis, gov, available, family="daily_close", **extra):
    values = {name: extra.get(name, "0") for name in FACTOR_NAMES[:4]}
    values["gov_3y_bp"] = gov
    values["calendar_days"] = None
    return FactorObservation(
        "bond_factor_v1", family, day, available, available, available,
        values, 1, "native", [], basis, f"{family}-{day}",
    )


def profile(version, published, duration="3"):
    return ProfileVersion(
        version, IDENTITY, "2026-06-30", published, published, "https://example.test",
        version, {"duration_nav": duration, "management_fee_annual_pct": "0.80", "fee_schedule": []},
        [{"field": "duration_nav", "quote": "净值口径久期：3年", "unit": "年"}], "ok",
    )


def test_synthetic_duration_sign_and_scale(tmp_path):
    assert duration_price_return_pct("3", "1") == "-0.03"
    assert duration_price_return_pct("3", "-1") == "0.03"
    store = store_of(tmp_path)
    store.save_nav(nav("2026-10-21", "1.0000", "2026-10-21T20:00:00+08:00"))
    store.save_nav(nav("2026-10-22", "0.9997", "2026-10-22T20:00:00+08:00"))
    store.save_factor(factor("2026-10-22", "2026-10-21", "1", "2026-10-22T20:00:00+08:00"))
    store.publish_profile(profile("v", "2026-08-28T00:00:00+08:00"))
    training = build_training_set(IDENTITY, "2026-10-23T00:00:00+08:00", store)
    model = fit_baseline(training, store.profile_as_of("918273", "A", "2026-10-23T00:00:00+08:00"))
    live = factor("2026-10-23", "2026-10-22", "1", "2026-10-23T10:00:00+08:00")
    live = replace(live, calendar_days=0, market_as_of="2026-10-23T10:00:00+08:00")
    snapshot = predict(model, live, nav("2026-10-22", "0.9997", "2026-10-22T20:00:00+08:00"), store.current_profile("918273", "A"), NOW)
    assert snapshot.return_pct == "-0.03"
    assert Decimal(snapshot.return_pct) < 0


def test_southern_intraday_keeps_experimental_return(tmp_path):
    store = store_of(tmp_path)
    store.save_nav(nav("2026-10-21", "1.0000", "2026-10-21T20:00:00+08:00"))
    store.save_nav(nav("2026-10-22", "0.9997", "2026-10-22T20:00:00+08:00"))
    store.save_factor(factor("2026-10-22", "2026-10-21", "1", "2026-10-22T20:00:00+08:00"))
    store.publish_profile(profile("v", "2026-08-28T00:00:00+08:00"))
    training = build_training_set(IDENTITY, "2026-10-23T00:00:00+08:00", store)
    model = fit_baseline(training, store.profile_as_of("918273", "A", "2026-10-23T00:00:00+08:00"))
    live = replace(
        factor("2026-10-23", "2026-10-22", "1", "2026-10-23T12:31:00+08:00", family="southern_intraday"),
        calendar_days=1,
        market_as_of="2026-10-23T12:31:00+08:00",
    )
    snapshot = predict(model, live, nav("2026-10-22", "0.9997", "2026-10-22T20:00:00+08:00"), store.current_profile("918273", "A"), NOW)
    assert snapshot.return_pct == "-0.03"
    assert snapshot.status == "experimental"
    assert "basis_unverified" in snapshot.reason_codes


def test_preprocessing_fits_training_only(tmp_path):
    store = store_of(tmp_path)
    previous = "2026-01-01"
    store.save_nav(nav(previous, "1.0000", "2026-01-01T20:00:00+08:00"))
    for index in range(1, 6):
        day = f"2026-01-{index + 1:02d}"
        basis = previous
        store.save_nav(nav(day, "1.0000", f"{day}T20:00:00+08:00"))
        store.save_factor(factor(day, basis, "1", f"{day}T20:00:00+08:00"))
        previous = day
    store.save_factor(factor("2026-02-01", "2026-01-31", "100", "2026-02-02T20:00:00+08:00"))
    training = build_training_set(IDENTITY, "2026-01-31T00:00:00+08:00", store)
    model = fit_ridge(training, {"ridge_alpha": 1e-6, "half_life_days": 30})
    assert model.preprocessing["mean"]["gov_3y_bp"] == "1"
    assert Decimal(model.preprocessing["mean"]["gov_3y_bp"]) < Decimal("10")


def test_post_cutoff_report_is_not_used(tmp_path):
    store = store_of(tmp_path)
    store.save_nav(nav("2026-10-21", "1", "2026-10-21T20:00:00+08:00"))
    store.save_nav(nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"))
    store.save_factor(factor("2026-10-22", "2026-10-21", "1", "2026-10-22T20:00:00+08:00"))
    store.publish_profile(profile("early", "2026-08-01T00:00:00+08:00", "2"))
    store.publish_profile(profile("late", "2026-10-23T00:00:00+08:00", "9"))
    training = build_training_set(IDENTITY, "2026-10-01T00:00:00+08:00", store)
    assert training.profile_version == "early"


def test_missing_or_reordered_factor_is_rejected(tmp_path):
    store = store_of(tmp_path)
    store.save_nav(nav("2026-10-21", "1", "2026-10-21T20:00:00+08:00"))
    store.save_nav(nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"))
    store.save_factor(factor("2026-10-22", "2026-10-21", "1", "2026-10-22T20:00:00+08:00"))
    training = build_training_set(IDENTITY, "2026-10-23T00:00:00+08:00", store)
    model = fit_baseline(training, None)
    broken = replace(model, feature_names=("calendar_days", "gov_3y_bp"))
    with pytest.raises(EstimateInputError):
        predict(broken, factor("2026-10-23", "2026-10-22", "1", "2026-10-23T10:00:00+08:00"), nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"), None, NOW)
    missing = factor("2026-10-23", "2026-10-22", None, "2026-10-23T10:00:00+08:00")
    snapshot = predict(model, missing, nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"), None, NOW)
    assert snapshot.return_pct is None
    assert "market_missing" in snapshot.reason_codes


def test_delayed_nav_is_not_paired_with_single_day_bp(tmp_path):
    store = store_of(tmp_path)
    store.save_nav(nav("2026-10-20", "1", "2026-10-20T20:00:00+08:00"))
    store.save_nav(nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"))
    store.save_factor(factor("2026-10-22", "2026-10-21", "1", "2026-10-22T20:00:00+08:00"))
    training = build_training_set(IDENTITY, "2026-10-23T00:00:00+08:00", store)
    assert training.rows == []
    model = fit_baseline(
        replace(training, rows=[{
            "nav_date": "2026-10-22",
            "available_at": "2026-10-22T20:00:00+08:00",
            "y": "0",
            "features": {name: "0" for name in FACTOR_NAMES},
            "source_version": "x",
        }]),
        profile("v", "2026-08-01T00:00:00+08:00"),
    )
    live = replace(factor("2026-10-23", "2026-10-22", "1", "2026-10-23T10:00:00+08:00"), calendar_days=1)
    snapshot = predict(model, live, nav("2026-10-20", "1", "2026-10-20T20:00:00+08:00"), None, NOW)
    assert snapshot.return_pct is None
    assert "nav_delayed" in snapshot.reason_codes


def test_trained_net_return_does_not_deduct_fees_twice(tmp_path):
    store = store_of(tmp_path)
    previous = "2026-01-01"
    store.save_nav(nav(previous, "1.0000", "2026-01-01T20:00:00+08:00"))
    for index in range(1, 8):
        day = f"2026-01-{index + 1:02d}"
        store.save_nav(nav(day, "1.0005", f"{day}T20:00:00+08:00"))
        store.save_factor(factor(day, previous, "0", f"{day}T20:00:00+08:00"))
        previous = day
    training = build_training_set(IDENTITY, "2026-02-01T00:00:00+08:00", store)
    model = fit_ridge(training, {"ridge_alpha": 1e-8, "half_life_days": 30})
    priced = store.current_profile("918273", "A")
    store.publish_profile(profile("fee", "2026-08-01T00:00:00+08:00", "3"))
    loaded = store.current_profile("918273", "A")
    assert loaded.fields["management_fee_annual_pct"] == "0.80"
    live = replace(factor("2026-01-08", "2026-01-07", "0", "2026-01-08T20:00:00+08:00"), calendar_days=1)
    snapshot = predict(model, live, nav("2026-01-07", "1.0005", "2026-01-07T20:00:00+08:00"), loaded, NOW, {"enable_ready": False})
    from src.fund_estimate_models import _predict_number
    from src.fund_estimate_types import pct_text

    features = {name: "0" for name in FACTOR_NAMES}
    features["calendar_days"] = "1"
    assert snapshot.return_pct == pct_text(_predict_number(model, features))
    assert loaded.fields["management_fee_annual_pct"] == "0.80"


def test_uncalibrated_interval_is_not_labelled_empirical(tmp_path):
    store = store_of(tmp_path)
    store.save_nav(nav("2026-10-21", "1", "2026-10-21T20:00:00+08:00"))
    store.save_nav(nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"))
    store.save_factor(factor("2026-10-22", "2026-10-21", "1", "2026-10-22T20:00:00+08:00"))
    store.publish_profile(profile("v", "2026-08-01T00:00:00+08:00"))
    training = build_training_set(IDENTITY, "2026-10-23T00:00:00+08:00", store)
    model = fit_baseline(training, store.current_profile("918273", "A"))
    live = replace(factor("2026-10-23", "2026-10-22", "1", "2026-10-23T10:00:00+08:00"), calendar_days=0)
    snapshot = predict(model, live, nav("2026-10-22", "1", "2026-10-22T20:00:00+08:00"), store.current_profile("918273", "A"), NOW, {"enable_ready": False, "min_oos_residuals": 20})
    assert snapshot.interval_kind == "scenario"
    assert snapshot.status == "experimental"
    assert "90%" not in snapshot.confidence_label
    assert snapshot.status != "ready"


def test_insufficient_history_returns_reference(tmp_path):
    store = store_of(tmp_path)
    previous = "2026-01-01"
    store.save_nav(nav(previous, "1", "2026-01-01T20:00:00+08:00"))
    for index in range(1, 5):
        day = f"2026-01-{index + 1:02d}"
        store.save_nav(nav(day, "1", f"{day}T20:00:00+08:00"))
        store.save_factor(factor(day, previous, "1", f"{day}T20:00:00+08:00"))
        previous = day
    result = train_models(IDENTITY, store, NOW, {"min_fit_samples": 60})
    assert result["trained"] is False
    reference = {"kind": "category_range", "note": "四类债基收蛋区间是市场参考，不是本基金收益率", "source_updated_at": "2026-10-23T10:00:00+08:00"}
    snapshot = project_snapshot(IDENTITY, store, factor("2026-01-05", "2026-01-04", "1", "2026-01-05T10:00:00+08:00"), nav("2026-01-04", "1", "2026-01-04T20:00:00+08:00"), NOW, {"min_fit_samples": 60}, reference)
    assert snapshot.status == "reference"
    assert snapshot.return_pct is None
    assert snapshot.market_reference["note"].startswith("四类")
