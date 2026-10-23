"""久期基线与指数加权 Ridge。系数是统计敞口，不是当日真实持仓。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math

from src.fund_estimate_sources import total_return_pct
from src.fund_estimate_store import iso_now, parse_time
from src.fund_estimate_types import (
    CONFIDENCE_LABELS,
    FACTOR_NAMES,
    FACTOR_UNITS,
    TARGET,
    EstimateInputError,
    FactorObservation,
    FundIdentity,
    MissingDividendCoverage,
    ModelVersion,
    NavObservation,
    ProfileVersion,
    TrainingSet,
    EstimateSnapshot,
    decimal_text,
    pct_text,
)


def duration_price_return_pct(duration_nav, delta_y_bp) -> str:
    """1bp = 0.01 个百分点。久期 3 年、收益率上行 1bp，价格贡献为 -0.03%。"""
    duration = Decimal(decimal_text(duration_nav))
    change = Decimal(decimal_text(delta_y_bp))
    return pct_text(-duration * change / Decimal(100))


def build_training_set(identity: FundIdentity, cutoff_at: str, store, source_family: str = "daily_close") -> TrainingSet:
    navs = store.nav_as_of(identity.code, cutoff_at)
    factors = {
        item.as_of: item
        for item in store.factors_as_of(cutoff_at, source_family=source_family)
        if parse_time(_market_available_at(item)) <= parse_time(cutoff_at)
    }
    profile = _visible_profile(store.profile_as_of(identity.code, identity.share_class, cutoff_at), cutoff_at)
    rows = []
    gaps = []
    ordered = sorted(navs, key=lambda item: item.nav_date)
    for previous, current in zip(ordered, ordered[1:]):
        if current.event_status != "ordinary" or current.dividend_coverage == "special_event":
            gaps.append({"nav_date": current.nav_date, "reason": "special_share_event"})
            continue
        if current.cash_dividend_per_share is None or current.dividend_coverage == "missing":
            gaps.append({"nav_date": current.nav_date, "reason": "dividend_missing"})
            continue
        factor = factors.get(current.nav_date)
        if factor is None:
            gaps.append({"nav_date": current.nav_date, "reason": "factor_missing"})
            continue
        if factor.basis_status == "unverified" or factor.basis_date != previous.nav_date:
            gaps.append({"nav_date": current.nav_date, "reason": "basis_unverified"})
            continue
        if any(factor.values_bp.get(name) is None for name in FACTOR_NAMES[:4]):
            gaps.append({"nav_date": current.nav_date, "reason": "factor_missing"})
            continue
        try:
            label = total_return_pct(previous.unit_nav, current.unit_nav, current.cash_dividend_per_share)
        except MissingDividendCoverage:
            gaps.append({"nav_date": current.nav_date, "reason": "dividend_missing"})
            continue
        calendar_days = str((date.fromisoformat(current.nav_date) - date.fromisoformat(previous.nav_date)).days)
        features = {name: factor.values_bp.get(name) for name in FACTOR_NAMES[:4]}
        features["calendar_days"] = calendar_days
        market_available_at = _market_available_at(factor)
        nav_available_at = max((previous.available_at, current.available_at), key=parse_time)
        economic_at = factor.market_as_of or f"{factor.as_of}T17:00:00+08:00"
        # 回补抓取只能证明当前版本于抓取时可见；行情日期不是版本发布日期。
        availability_verified = bool(
            factor.market_as_of
            and parse_time(market_available_at).date().isoformat() == factor.as_of
            and parse_time(previous.available_at) <= parse_time(market_available_at)
            and parse_time(market_available_at) < parse_time(current.available_at)
        )
        rows.append(
            {
                "nav_date": current.nav_date,
                "available_at": max((nav_available_at, market_available_at), key=parse_time),
                "market_available_at": market_available_at,
                "nav_available_at": nav_available_at,
                "economic_at": economic_at,
                "availability_verified": availability_verified,
                "y": label,
                "features": features,
                "source_version": factor.source_version,
                "nav_source_versions": [previous.source_version, current.source_version],
                "prediction_at": market_available_at if availability_verified else economic_at,
            }
        )
    return TrainingSet(
        identity=identity,
        cutoff_at=cutoff_at,
        rows=rows,
        feature_names=FACTOR_NAMES,
        source_versions=sorted({row["source_version"] for row in rows}),
        profile_version=None if profile is None else profile.profile_version,
        gaps=gaps,
        source_family=source_family,
    )


def fit_baseline(training: TrainingSet, profile: ProfileVersion | None, config: dict | None = None) -> ModelVersion:
    config = config or {}
    profile = _visible_profile(profile, training.cutoff_at)
    duration = _profile_duration(profile)
    rows = training.rows
    if duration is None:
        duration, carry = _estimate_duration_and_carry(rows)
        estimated = True
    else:
        carry = _fit_carry(rows, duration)
        estimated = False
    coefficients = {"duration_nav": decimal_text(duration), "carry_per_day": pct_text(carry), "fees_applied": "0"}
    validation = {"estimated_duration": estimated, "sample_count": len(rows)}
    return _model(
        training,
        "duration_baseline",
        ("gov_3y_bp", "calendar_days"),
        coefficients,
        {"standardize": False},
        validation,
        profile,
    )


def fit_ridge(training: TrainingSet, config: dict | None = None) -> ModelVersion:
    config = config or {}
    alpha = float(config.get("ridge_alpha", 1.0))
    half_life = float(config.get("half_life_days", 30))
    names = list(FACTOR_NAMES)
    rows = training.rows
    if len(rows) < 2:
        raise EstimateInputError("Ridge 样本不足")
    means, scales, beta = _weighted_ridge(rows, names, alpha, half_life)
    coefficients = {"intercept": _float_text(beta[0])}
    for index, name in enumerate(names):
        coefficients[name] = _float_text(beta[index + 1])
    preprocessing = {
        "mean": {name: _float_text(means[name]) for name in names},
        "scale": {name: _float_text(scales[name]) for name in names},
        "alpha": _float_text(alpha),
        "half_life_days": _float_text(half_life),
        "standardize": True,
    }
    return _model(training, "weighted_ridge_net", tuple(names), coefficients, preprocessing, {"sample_count": len(rows)}, None)


def walk_forward_validate(training: TrainingSet, config: dict | None = None) -> dict:
    config = config or {}
    rows = sorted(training.rows, key=lambda row: row["nav_date"])
    windows = _training_windows(config)
    viable = [window for window in windows if window < len(rows)]
    default_window = min(min(windows), len(rows))
    if not viable:
        result = _empty_validation(len(rows), list(training.gaps), "没有足够的窗口完成样本外比较")
        result.update(window=default_window, selected_model_kind="duration_baseline", models={})
        return result

    # 严格时点验证与当前历史版本探索使用不同的输入可见性规则，不能混报。
    modes = ("point_in_time", "retrospective_current_versions")
    common_start = max(viable)
    split = common_start + max(1, (len(rows) - common_start) // 2)
    predictions, dates = {}, {}
    for mode in modes:
        predictions[mode] = {
            window: _rolling_predictions(training, rows, window, config, availability_mode=mode)
            for window in viable
        }
        common = set.intersection(*(set(predictions[mode][window]) for window in viable))
        dates[mode] = {
            "selection": [row["nav_date"] for row in rows[common_start:split] if row["nav_date"] in common],
            "evaluation": [row["nav_date"] for row in rows[split:] if row["nav_date"] in common],
        }
    strict = dates["point_in_time"]
    mode = "point_in_time" if (
        len(strict["selection"]) >= max(1, int(config.get("min_selection_predictions", 1)))
        and len(strict["evaluation"]) >= max(1, int(config.get("min_validation_predictions", 1)))
    ) else "retrospective_current_versions"
    selection_dates = dates[mode]["selection"]

    def metrics_for(kind, window, availability_mode, stage):
        days = dates[availability_mode][stage]
        pairs = [predictions[availability_mode][window][day][kind] for day in days]
        return _metrics_from_pairs(pairs, [], [], len(rows), config, days,
                                   training.source_family, availability_mode)

    candidates = [
        {"window": window, "prediction_dates": selection_dates,
         "models": {kind: metrics_for(kind, window, mode, "selection")
                    for kind in ("duration_baseline", "weighted_ridge_net")}}
        for window in viable
    ]
    ranked = [
        (_mae_value(candidate["models"][kind]), candidate["window"], kind)
        for candidate in candidates for kind in ("duration_baseline", "weighted_ridge_net")
    ]
    _, window, selected_kind = min(ranked)
    model_metrics = {}
    scope = ("chronological_holdout_after_selection" if mode == "point_in_time"
             else "retrospective_current_versions_chronological_holdout")
    for kind in ("duration_baseline", "weighted_ridge_net"):
        reports = {item: metrics_for(kind, window, item, "evaluation") for item in modes}
        if mode != "point_in_time":
            # 严格输入重放也不能洗掉使用回补版本挑参数的限制。
            reports["point_in_time"].update(
                selection_verified=False, availability_verified=False,
                interval_coverage=None, interval_width_pct=None,
                residual_low_pct=None, residual_high_pct=None,
                interval_calibration_count=0, interval_evaluation_count=0,
                interval_calibration_dates=[], interval_evaluation_dates=[],
            )
        metrics = dict(reports[mode])
        metrics.update(window=window, evaluation_scope=scope,
                       point_in_time=reports["point_in_time"],
                       retrospective=reports["retrospective_current_versions"],
                       point_in_time_oos_count=reports["point_in_time"]["oos_count"],
                       retrospective_oos_count=reports["retrospective_current_versions"]["oos_count"])
        model_metrics[kind] = metrics
    result = dict(model_metrics[selected_kind])
    result.update(
        window=window, selected_model_kind=selected_kind, models=model_metrics,
        baseline_mae_bp=model_metrics["duration_baseline"]["mae_bp"],
        ridge_mae_bp=model_metrics["weighted_ridge_net"]["mae_bp"],
        selection_prediction_dates=selection_dates, window_candidates=candidates,
        gaps=list(training.gaps), baseline_profile_policy="statistical_duration_training_rows_only",
    )
    return result


def predict(model: ModelVersion, factors: FactorObservation, base_nav: NavObservation, profile: ProfileVersion | None, now: datetime, config: dict | None = None, market_reference: dict | None = None) -> EstimateSnapshot:
    config = config or {}
    generated_at = iso_now(now)
    trade_date = factors.as_of or generated_at[:10]
    identity = model.identity
    _require_feature_contract(model)
    profile = _visible_profile(profile, generated_at)
    if parse_time(model.available_at) > parse_time(generated_at):
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["insufficient_history"], market_reference, base_nav, profile, None)
    trained_family = str(model.preprocessing.get("source_family") or "")
    cross_family = factors.source_family != trained_family
    if not trained_family or (cross_family and not _mapping_verified(model, factors, generated_at)):
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["basis_unverified"], market_reference, base_nav, profile, model)
    observed_times = (factors.published_at, factors.fetched_at, factors.market_as_of, base_nav.available_at)
    if any(parse_time(value) > parse_time(generated_at) for value in observed_times if value):
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["market_missing"], market_reference, base_nav, profile, model)
    if factors.basis_status == "unverified" or factors.source_family == "unverified_mix":
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["basis_unverified"], market_reference, base_nav, profile, model)
    if factors.basis_date is None or factors.basis_date != base_nav.nav_date:
        return _status_snapshot(identity, trade_date, generated_at, "pending", ["nav_delayed"], market_reference, base_nav, profile, model)
    missing = [name for name in model.feature_names if factors.values_bp.get(name) in (None, "") and name != "calendar_days"]
    if missing:
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["market_missing"], market_reference, base_nav, profile, model, missing=missing)
    features = dict(factors.values_bp)
    if cross_family:
        mappings = factors.mapping_evidence["per_feature"]
        for name in FACTOR_NAMES[:4]:
            mapping = mappings[name]
            features[name] = decimal_text(
                Decimal(decimal_text(mapping["slope"])) * Decimal(decimal_text(features[name]))
                + Decimal(decimal_text(mapping["intercept_bp"]))
            )
    if "calendar_days" in model.feature_names:
        if factors.calendar_days is None:
            return _status_snapshot(identity, trade_date, generated_at, "reference", ["market_missing"], market_reference, base_nav, profile, model)
        features["calendar_days"] = str(factors.calendar_days)
    raw = _predict_number(model, features)
    # 拟合目标已是正式净值的净收益，不再扣管理费。
    point = pct_text(raw)
    validation = model.validation or {}
    oos = int(validation.get("oos_count") or 0)
    min_oos = int(config.get("min_oos_residuals", 20))
    interval_kind = "none"
    lower = upper = None
    compatible_interval = _interval_matches_observation(validation, factors, trained_family, config)
    if (
        compatible_interval
        and oos >= min_oos
        and int(validation.get("interval_calibration_count") or 0) >= min_oos
        and int(validation.get("interval_evaluation_count") or 0) >= min_oos
        and validation.get("residual_definition") == "actual_minus_predicted"
        and validation.get("residual_low_pct") is not None
        and validation.get("residual_high_pct") is not None
    ):
        interval_kind = "empirical"
        lower = pct_text(Decimal(point) + Decimal(str(validation["residual_low_pct"])))
        upper = pct_text(Decimal(point) + Decimal(str(validation["residual_high_pct"])))
    elif _profile_duration(profile) is not None:
        interval_kind = "scenario"
        band = abs(Decimal(str(_profile_duration(profile))) / Decimal(100))
        lower = pct_text(Decimal(point) - band)
        upper = pct_text(Decimal(point) + band)
    status = "ready" if interval_kind == "empirical" and compatible_interval and _quality_ready(validation, config) else "experimental"
    reasons = []
    confidence_label = _label(status, interval_kind)
    if interval_kind == "empirical" and validation.get("validation_scope") == "daily_close":
        confidence_label += "（日终口径）"
    return EstimateSnapshot(
        product_id=identity.product_id,
        code=identity.code,
        trade_date=trade_date,
        target=TARGET,
        status=status,
        reason_codes=reasons,
        base_nav_date=base_nav.nav_date,
        base_nav=base_nav.unit_nav,
        market_as_of=factors.market_as_of,
        profile_as_of=None if profile is None else profile.report_as_of,
        report_published_at=None if profile is None else profile.published_at,
        trained_through=model.trained_through,
        model_version=model.model_version,
        return_pct=point,
        lower_pct=lower,
        upper_pct=upper,
        interval_kind=interval_kind,
        confidence_label=confidence_label,
        market_reference=market_reference,
        generated_at=generated_at,
    )


def train_models(identity: FundIdentity, store, now: datetime, config: dict | None = None, source_family: str = "daily_close") -> dict:
    config = config or {}
    cutoff = iso_now(now)
    training = build_training_set(identity, cutoff, store, source_family=source_family)
    store.set_watermark(
        f"training:{identity.code}:{identity.share_class}",
        json.dumps({"rows": len(training.rows), "gaps": len(training.gaps)}, ensure_ascii=False),
        cutoff,
    )
    if len(training.rows) < int(config.get("min_fit_samples", 60)):
        return {"trained": False, "rows": len(training.rows), "gaps": training.gaps}
    validation = walk_forward_validate(training, config)
    window = int(validation["window"])
    selected_training = _training_slice(training, sorted(training.rows, key=lambda row: row["nav_date"])[-window:])
    # 回测基线自行统计估计久期，最终拟合保持完全相同的方法。
    baseline = fit_baseline(selected_training, None, config)
    ridge = fit_ridge(selected_training, config)
    versions = []
    for model in (baseline, ridge):
        own = dict(validation.get("models", {}).get(model.model_kind) or _empty_validation(len(training.rows), training.gaps, "无独立评估样本"))
        own.update(
            sample_count=len(selected_training.rows),
            history_sample_count=len(training.rows),
            window=window,
            selected_model_kind=validation["selected_model_kind"],
            selection_prediction_dates=validation.get("selection_prediction_dates", []),
            evaluation_scope=validation.get("evaluation_scope", "unvalidated_fit"),
            baseline_profile_policy="statistical_duration_training_rows_only",
        )
        preprocessing = dict(model.preprocessing, training_window=window)
        versions.append(_with_validation(replace(model, preprocessing=preprocessing), own))
    baseline, ridge = versions
    use_ridge = validation["selected_model_kind"] == "weighted_ridge_net"
    store.save_model(baseline, active=not use_ridge)
    store.save_model(ridge, active=use_ridge)
    return {"trained": True, "rows": len(training.rows), "validation": validation, "selected": ridge.model_kind if use_ridge else baseline.model_kind}


def project_snapshot(identity, store, factors, base_nav, now, config, market_reference=None, source_family="daily_close") -> EstimateSnapshot:
    generated_at = iso_now(now)
    reason = _support_block(identity)
    if reason:
        return _status_snapshot(identity, factors.as_of if factors else generated_at[:10], generated_at, "unsupported", [reason], market_reference, base_nav, None, None)
    model = store.active_model(identity.code, identity.share_class)
    if model is not None and parse_time(model.available_at) > parse_time(generated_at):
        model = None
    profile = _visible_profile(store.profile_as_of(identity.code, identity.share_class, generated_at), generated_at)
    training_rows = store.get_watermark(f"training:{identity.code}:{identity.share_class}")
    row_count = 0
    if training_rows:
        try:
            row_count = int(json.loads(training_rows).get("rows") or 0)
        except (TypeError, ValueError):
            row_count = 0
    if model is None or row_count < int(config.get("min_fit_samples", 60)):
        return _status_snapshot(
            identity,
            factors.as_of if factors else generated_at[:10],
            generated_at,
            "reference" if factors is not None else "pending",
            ["insufficient_history"] if row_count else ["history_backfill_pending"],
            market_reference,
            base_nav,
            profile,
            model,
        )
    if factors is None:
        return _status_snapshot(identity, generated_at[:10], generated_at, "reference", ["market_missing"], market_reference, base_nav, profile, model)
    if base_nav is None:
        return _status_snapshot(identity, factors.as_of, generated_at, "pending", ["nav_delayed"], market_reference, None, profile, model)
    return predict(model, factors, base_nav, profile, now, config, market_reference)


def _support_block(identity: FundIdentity) -> str | None:
    from src.fund_estimate_profiles import support_reason

    return support_reason(identity.fund_type, "")


def present_snapshot(payload: dict, now: datetime, config: dict, bond_session: bool) -> dict:
    """读路径上的陈旧降级，不写库。"""
    item = dict(payload)
    market_as_of = item.get("market_as_of")
    if not bond_session or not market_as_of or item.get("return_pct") is None:
        return item
    age = (parse_time(iso_now(now)) - parse_time(market_as_of)).total_seconds()
    if age > int(config.get("stale_minutes", 5)) * 60:
        item["status"] = "stale"
        item["return_pct"] = None
        item["lower_pct"] = None
        item["upper_pct"] = None
        item["interval_kind"] = "none"
        item["confidence_label"] = CONFIDENCE_LABELS["stale"]
        reasons = list(item.get("reason_codes") or [])
        if "market_stale" not in reasons:
            reasons.append("market_stale")
        item["reason_codes"] = reasons
    return item


def _quality_ready(validation: dict, config: dict) -> bool:
    if not config.get("enable_ready", False):
        return False
    if validation.get("validation_availability") != "point_in_time" or not validation.get("availability_verified"):
        return False
    thresholds = ("max_mae_bp", "min_direction_accuracy", "min_interval_coverage")
    if any(config.get(name) is None for name in thresholds):
        return False
    if int(validation.get("intraday_sample_count") or 0) < int(config.get("min_intraday_samples", 20)):
        return False
    if int(validation.get("oos_count") or 0) < int(config.get("min_oos_residuals", 20)):
        return False
    mae = validation.get("mae_bp")
    direction = validation.get("direction_accuracy")
    coverage = validation.get("interval_coverage")
    if mae is None or direction is None or coverage is None:
        return False
    return (
        Decimal(str(mae)) <= Decimal(str(config["max_mae_bp"]))
        and Decimal(str(direction)) >= Decimal(str(config["min_direction_accuracy"]))
        and Decimal(str(coverage)) >= Decimal(str(config["min_interval_coverage"]))
    )


def _visible_profile(profile: ProfileVersion | None, cutoff_at: str):
    if profile is None or not profile.published_at:
        return None
    cutoff = parse_time(cutoff_at)
    if parse_time(profile.published_at) > cutoff or parse_time(profile.fetched_at) > cutoff:
        return None
    return profile


def _market_available_at(factor):
    return max((value for value in (factor.published_at, factor.fetched_at) if value), key=parse_time)


def _mapping_verified(model, factors, generated_at):
    evidence = getattr(factors, "mapping_evidence", None)
    if factors.basis_status != "verified_mapping" or not isinstance(evidence, dict):
        return False
    try:
        if (
            not evidence.get("mapping_id")
            or evidence.get("from_source_family") != factors.source_family
            or evidence.get("to_source_family") != model.preprocessing.get("source_family")
            or parse_time(evidence["calibrated_at"]) > parse_time(generated_at)
            or int(evidence.get("training_sample_count") or 0) < 20
            or int(evidence.get("validation_sample_count") or 0) < 10
        ):
            return False
        per_feature = evidence.get("per_feature")
        if not isinstance(per_feature, dict):
            return False
        for name in FACTOR_NAMES[:4]:
            mapping = per_feature.get(name, {})
            if not isinstance(mapping, dict) or mapping.get("unit") != "bp" or factors.values_bp.get(name) in (None, ""):
                return False
            decimal_text(mapping["slope"])
            decimal_text(mapping["intercept_bp"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _interval_matches_observation(validation, factors, trained_family, config):
    if validation.get("validation_availability") != "point_in_time" or not validation.get("availability_verified"):
        return False
    if factors.source_family != trained_family or validation.get("source_family") != trained_family:
        return False
    if validation.get("validation_scope") == "intraday":
        return int(validation.get("intraday_sample_count") or 0) >= int(config.get("min_intraday_samples", 20))
    if validation.get("validation_scope") != "daily_close" or not factors.market_as_of:
        return False
    # 当前日盘中同源观测仍不是日终输入；日终残差不能证明盘中覆盖率。
    # 收盘参考（非 17:00 定格）也不是日终输入，即使它的时点已过 17:00。
    if factors.basis_status == "close_reference":
        return False
    observed = parse_time(factors.market_as_of)
    return observed.date().isoformat() == factors.as_of and (observed.hour, observed.minute) >= (17, 0)


def _predict_number(model: ModelVersion, features: dict) -> Decimal:
    if model.model_kind == "duration_baseline":
        duration = Decimal(model.coefficients["duration_nav"])
        carry = Decimal(model.coefficients["carry_per_day"])
        change = Decimal(decimal_text(features["gov_3y_bp"]))
        days = Decimal(decimal_text(features.get("calendar_days") or "0"))
        return -duration * change / Decimal(100) + carry * days
    total = Decimal(model.coefficients["intercept"])
    for name in model.feature_names:
        mean = Decimal(model.preprocessing["mean"][name])
        scale = Decimal(model.preprocessing["scale"][name])
        value = Decimal(decimal_text(features[name]))
        total += Decimal(model.coefficients[name]) * ((value - mean) / scale)
    return total


def _require_feature_contract(model: ModelVersion) -> None:
    names = tuple(model.feature_names)
    if model.model_kind == "duration_baseline":
        expected = ("gov_3y_bp", "calendar_days")
    elif model.model_kind == "weighted_ridge_net":
        expected = FACTOR_NAMES
    else:
        raise EstimateInputError("未知模型")
    if names != expected:
        raise EstimateInputError("因子名称或顺序与模型不一致")
    for name in names:
        unit = model.feature_units.get(name)
        if unit != FACTOR_UNITS[name]:
            raise EstimateInputError("因子单位与模型不一致")
        if name not in model.coefficients and model.model_kind != "duration_baseline":
            raise EstimateInputError("模型系数缺失")
    if model.model_kind == "weighted_ridge_net":
        if "intercept" not in model.coefficients:
            raise EstimateInputError("模型系数缺失")
        for name in names:
            if name not in model.preprocessing.get("mean", {}) or name not in model.preprocessing.get("scale", {}):
                raise EstimateInputError("预处理与训练特征不一致")


def _profile_duration(profile: ProfileVersion | None):
    if profile is None:
        return None
    value = (profile.fields or {}).get("duration_nav")
    if value in (None, ""):
        return None
    return Decimal(decimal_text(value))


def _estimate_duration_and_carry(rows: list[dict]):
    if not rows:
        return Decimal("0"), Decimal("0")
    # y = -D * gov / 100 + carry * days. 两参数最小二乘，只使用训练行。
    sxx = syy = sxy = sx = sy = n = 0.0
    # 使用正态方程的简化：以 gov 和 days 为列。
    xtx = [[0.0, 0.0], [0.0, 0.0]]
    xty = [0.0, 0.0]
    for row in rows:
        gov = float(row["features"]["gov_3y_bp"])
        days = float(row["features"]["calendar_days"])
        y = float(row["y"])
        x0 = -gov / 100.0
        x1 = days
        xtx[0][0] += x0 * x0
        xtx[0][1] += x0 * x1
        xtx[1][0] += x1 * x0
        xtx[1][1] += x1 * x1
        xty[0] += x0 * y
        xty[1] += x1 * y
        n += 1
    try:
        beta = _solve(xtx, xty)
    except ValueError:
        return Decimal("0"), Decimal("0")
    return Decimal(str(beta[0])), Decimal(str(beta[1]))


def _fit_carry(rows: list[dict], duration: Decimal) -> Decimal:
    numerator = Decimal("0")
    denominator = Decimal("0")
    for row in rows:
        gov = Decimal(decimal_text(row["features"]["gov_3y_bp"]))
        days = Decimal(decimal_text(row["features"]["calendar_days"]))
        y = Decimal(row["y"])
        residual = y - (-duration * gov / Decimal(100))
        numerator += days * residual
        denominator += days * days
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator


def _weighted_ridge(rows, names, alpha, half_life):
    weights = []
    count = len(rows)
    for index in range(count):
        age = count - 1 - index
        weights.append(0.5 ** (age / half_life if half_life else 0))
    means = {}
    scales = {}
    weight_sum = sum(weights) or 1.0
    for name in names:
        values = [float(row["features"][name]) for row in rows]
        mean = sum(weight * value for weight, value in zip(weights, values)) / weight_sum
        var = sum(weight * (value - mean) ** 2 for weight, value in zip(weights, values)) / weight_sum
        means[name] = mean
        scales[name] = math.sqrt(var) or 1.0
    width = len(names) + 1
    xtwx = [[0.0 for _ in range(width)] for _ in range(width)]
    xtwy = [0.0 for _ in range(width)]
    for row, weight in zip(rows, weights):
        point = [1.0]
        for name in names:
            point.append((float(row["features"][name]) - means[name]) / scales[name])
        target = float(row["y"])
        for i in range(width):
            xtwy[i] += weight * point[i] * target
            for j in range(width):
                xtwx[i][j] += weight * point[i] * point[j]
    for index in range(1, width):
        xtwx[index][index] += alpha
    beta = _solve(xtwx, xtwy)
    return means, scales, beta


def _solve(matrix, vector):
    size = len(vector)
    cloned = [list(row) + [vector[index]] for index, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(cloned[row][col]))
        if abs(cloned[pivot][col]) < 1e-12:
            raise ValueError("奇异矩阵")
        cloned[col], cloned[pivot] = cloned[pivot], cloned[col]
        divisor = cloned[col][col]
        for column in range(col, size + 1):
            cloned[col][column] /= divisor
        for row in range(size):
            if row == col:
                continue
            factor = cloned[row][col]
            for column in range(col, size + 1):
                cloned[row][column] -= factor * cloned[col][column]
    return [cloned[index][size] for index in range(size)]


def _training_windows(config):
    minimum = max(2, int(config.get("min_fit_samples", 60)))
    return sorted({max(minimum, int(window)) for window in config.get("training_windows", (60, 120, 250))}) or [minimum]


def _training_slice(training, rows, cutoff_at=None):
    return replace(
        training,
        rows=list(rows),
        cutoff_at=cutoff_at or training.cutoff_at,
        source_versions=sorted({row["source_version"] for row in rows}),
        profile_version=None,
    )


def _rolling_predictions(training, rows, window, config, availability_mode="point_in_time"):
    predictions = {}
    for end in range(window, len(rows)):
        row = rows[end]
        prediction_at = row.get("prediction_at") or row["available_at"]
        if availability_mode == "point_in_time":
            if not row.get("availability_verified"):
                continue
            if parse_time(row.get("market_available_at") or row["available_at"]) > parse_time(prediction_at):
                continue
            # 每行可见时间包括行情版本及两个净值版本；不把后来的修订带回过去。
            visible = [item for item in rows[:end] if parse_time(item["available_at"]) <= parse_time(prediction_at)]
        else:
            # 仅在显式标记的探索报告中使用当前历史版本，不作真实时点评估。
            prediction_at = row.get("economic_at") or prediction_at
            visible = rows[:end]
        sample = visible[-window:]
        if len(sample) < window:
            continue
        fitted = _training_slice(training, sample, prediction_at)
        try:
            baseline = fit_baseline(fitted, None, config)
            ridge = fit_ridge(fitted, config)
        except (EstimateInputError, ValueError):
            continue
        actual = Decimal(row["y"])
        predictions[row["nav_date"]] = {
            "duration_baseline": (_predict_number(baseline, row["features"]), actual),
            "weighted_ridge_net": (_predict_number(ridge, row["features"]), actual),
        }
    return predictions


def _metrics_from_pairs(pairs, baseline_errors, ridge_errors, sample_count, config=None, prediction_dates=None, source_family="daily_close", availability_mode="point_in_time"):
    config = config or {}
    if not pairs:
        result = _empty_validation(sample_count, [], "样本外预测没有生成")
        result["prediction_dates"] = list(prediction_dates or [])
        result.update(validation_availability=availability_mode, availability_verified=False)
        return result
    # 偏差保留 predicted-actual 定义；构造区间用 actual-predicted。
    errors = [hat - actual for hat, actual in pairs]
    residuals = [-error for error in errors]
    mae = sum(abs(item) for item in errors) / Decimal(len(errors))
    bias = sum(errors) / Decimal(len(errors))
    hits = compared = 0
    for hat, actual in pairs:
        if hat == 0 or actual == 0:
            continue
        compared += 1
        if (hat > 0) == (actual > 0):
            hits += 1
    calibration_count = len(pairs) // 2 if availability_mode == "point_in_time" else 0
    evaluation_count = len(pairs) - calibration_count if availability_mode == "point_in_time" else 0
    minimum = max(1, int(config.get("min_oos_residuals", 20)))
    low = high = coverage = width = None
    if availability_mode == "point_in_time" and calibration_count >= minimum and evaluation_count >= minimum:
        ordered = sorted(residuals[:calibration_count])
        low = ordered[max(0, math.ceil(len(ordered) * 0.1) - 1)]
        high = ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.9) - 1)]
        held_out = pairs[calibration_count:]
        covered = sum(hat + low <= actual <= hat + high for hat, actual in held_out)
        coverage = pct_text(Decimal(covered) / Decimal(evaluation_count))
        width = pct_text(high - low)
    return {
        "sample_count": sample_count,
        "oos_count": len(pairs),
        "prediction_dates": list(prediction_dates or []),
        "mae_bp": pct_text(mae * Decimal(100)),
        "bias_bp": pct_text(bias * Decimal(100)),
        "bias_definition": "predicted_minus_actual",
        "direction_accuracy": None if compared == 0 else pct_text(Decimal(hits) / Decimal(compared)),
        "interval_coverage": coverage,
        "interval_width_pct": width,
        "residual_low_pct": None if low is None else pct_text(low),
        "residual_high_pct": None if high is None else pct_text(high),
        "residual_definition": "actual_minus_predicted",
        "residuals_pct": [pct_text(residual) for residual in residuals],
        "interval_calibration_count": calibration_count,
        "interval_evaluation_count": evaluation_count,
        "interval_calibration_dates": list((prediction_dates or [])[:calibration_count]),
        "interval_evaluation_dates": list((prediction_dates or [])[calibration_count:]) if evaluation_count else [],
        "interval_method": "chronological_prefix_calibration_fixed_holdout" if availability_mode == "point_in_time" else "not_calibrated_retrospective_versions",
        "validation_scope": "daily_close",
        "source_family": source_family,
        "validation_availability": availability_mode,
        "availability_verified": availability_mode == "point_in_time",
        "version_limitation": None if availability_mode == "point_in_time" else "当前回补历史版本，未核验其在历史预测时点可见；仅供回顾探索",
        "intraday_sample_count": 0,
        "ready": False,
    }


def _empty_validation(sample_count, gaps, note):
    return {
        "sample_count": sample_count,
        "oos_count": 0,
        "mae_bp": None,
        "bias_bp": None,
        "direction_accuracy": None,
        "interval_coverage": None,
        "interval_width_pct": None,
        "baseline_mae_bp": None,
        "ridge_mae_bp": None,
        "prediction_dates": [],
        "residual_low_pct": None,
        "residual_high_pct": None,
        "residual_definition": "actual_minus_predicted",
        "residuals_pct": [],
        "bias_definition": "predicted_minus_actual",
        "interval_calibration_count": 0,
        "interval_evaluation_count": 0,
        "interval_calibration_dates": [],
        "interval_evaluation_dates": [],
        "validation_scope": "daily_close",
        "validation_availability": "unverified",
        "availability_verified": False,
        "point_in_time_oos_count": 0,
        "retrospective_oos_count": 0,
        "gaps": gaps,
        "note": note,
        "intraday_sample_count": 0,
        "ready": False,
    }


def _mae_value(metrics: dict) -> Decimal:
    value = metrics.get("mae_bp") if "mae_bp" in metrics else metrics.get("ridge_mae_bp")
    if value is None:
        return Decimal("999999")
    return Decimal(str(value))


def _model(training, kind, names, coefficients, preprocessing, validation, profile) -> ModelVersion:
    payload = {
        "kind": kind,
        "names": list(names),
        "coefficients": coefficients,
        "preprocessing": preprocessing,
    }
    preprocessing = dict(preprocessing)
    preprocessing["source_family"] = training.source_family
    preprocessing["training_window"] = len(training.rows)
    preprocessing["training_sample_count"] = len(training.rows)
    preprocessing["training_start"] = training.rows[0]["nav_date"] if training.rows else None
    preprocessing["training_data_hash"] = hashlib.sha256(json.dumps(training.rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    payload["preprocessing"] = preprocessing
    payload.update(
        identity=training.identity.to_dict(),
        # cutoff 精度到日：秒级时间戳会让同数据同日的重复训练产生新版本、模型表单调膨胀。
        cutoff_at=training.cutoff_at[:10],
        training_rows=training.rows,
        source_versions=training.source_versions,
        profile_version=None if profile is None else profile.profile_version,
    )
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    through = training.rows[-1]["nav_date"] if training.rows else training.cutoff_at[:10]
    return ModelVersion(
        model_version=f"{kind}:{digest}",
        identity=training.identity,
        model_kind=kind,
        trained_through=through,
        available_at=training.cutoff_at,
        feature_names=tuple(names),
        feature_units={name: FACTOR_UNITS[name] for name in names},
        coefficients=coefficients,
        preprocessing=preprocessing,
        source_versions=list(training.source_versions),
        profile_version=None if profile is None else profile.profile_version,
        validation=validation,
        artifact_hash=digest,
    )


def _with_validation(model: ModelVersion, validation: dict) -> ModelVersion:
    validation = dict(model.validation, **validation)
    payload = model.to_dict()
    payload.update(validation=validation)
    payload.pop("model_version")
    payload.pop("artifact_hash")
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    return ModelVersion(
        model_version=f"{model.model_kind}:{digest}",
        identity=model.identity,
        model_kind=model.model_kind,
        trained_through=model.trained_through,
        available_at=model.available_at,
        feature_names=model.feature_names,
        feature_units=model.feature_units,
        coefficients=model.coefficients,
        preprocessing=model.preprocessing,
        source_versions=model.source_versions,
        profile_version=model.profile_version,
        validation=validation,
        artifact_hash=digest,
    )


def _status_snapshot(identity, trade_date, generated_at, status, reasons, market_reference, base_nav, profile, model, missing=None) -> EstimateSnapshot:
    return EstimateSnapshot(
        product_id=identity.product_id,
        code=identity.code,
        trade_date=trade_date or generated_at[:10],
        target=TARGET,
        status=status,
        reason_codes=list(reasons),
        base_nav_date=None if base_nav is None else base_nav.nav_date,
        base_nav=None if base_nav is None else base_nav.unit_nav,
        market_as_of=None,
        profile_as_of=None if profile is None else profile.report_as_of,
        report_published_at=None if profile is None else profile.published_at,
        trained_through=None if model is None else model.trained_through,
        model_version=None if model is None else model.model_version,
        return_pct=None,
        lower_pct=None,
        upper_pct=None,
        interval_kind="none",
        confidence_label=_label(status, "none"),
        market_reference=market_reference,
        generated_at=generated_at,
    )


def _label(status: str, interval_kind: str) -> str:
    label = CONFIDENCE_LABELS.get(status, status)
    if interval_kind == "scenario":
        return label + "，情景范围不是概率置信区间"
    if interval_kind == "empirical":
        return label + "，经验区间"
    return label


def _float_text(value: float) -> str:
    if not math.isfinite(value):
        raise EstimateInputError("模型系数不是有限数")
    return decimal_text(f"{value:.8f}")
