"""久期基线与指数加权 Ridge。系数是统计敞口，不是当日真实持仓。"""
from __future__ import annotations

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
    }
    profile = store.profile_as_of(identity.code, identity.share_class, cutoff_at)
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
        rows.append(
            {
                "nav_date": current.nav_date,
                "available_at": current.available_at,
                "y": label,
                "features": features,
                "source_version": factor.source_version,
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
    min_fit = int(config.get("min_fit_samples", 60))
    windows = [int(item) for item in config.get("training_windows", (60, 120, 250))]
    rows = training.rows
    gaps = list(training.gaps)
    if len(rows) < min_fit + 1:
        return _empty_validation(len(rows), gaps, "样本少于最短训练窗口")
    best = None
    for window in windows:
        if len(rows) <= window:
            continue
        metrics = _expanding_metrics(rows, window, config)
        metrics["window"] = window
        if best is None or _mae_value(metrics) < _mae_value(best):
            best = metrics
    if best is None:
        return _empty_validation(len(rows), gaps, "没有足够的窗口完成样本外比较")
    best["gaps"] = gaps
    best["intraday_sample_count"] = 0
    best["ready"] = False
    return best


def predict(model: ModelVersion, factors: FactorObservation, base_nav: NavObservation, profile: ProfileVersion | None, now: datetime, config: dict | None = None, market_reference: dict | None = None) -> EstimateSnapshot:
    config = config or {}
    generated_at = iso_now(now)
    trade_date = factors.as_of or generated_at[:10]
    identity = model.identity
    _require_feature_contract(model)
    if factors.basis_status == "unverified" or factors.source_family == "unverified_mix":
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["basis_unverified"], market_reference, base_nav, profile, model)
    if factors.basis_date is None or factors.basis_date != base_nav.nav_date:
        return _status_snapshot(identity, trade_date, generated_at, "pending", ["nav_delayed"], market_reference, base_nav, profile, model)
    missing = [name for name in model.feature_names if factors.values_bp.get(name) in (None, "") and name != "calendar_days"]
    if missing:
        return _status_snapshot(identity, trade_date, generated_at, "reference", ["market_missing"], market_reference, base_nav, profile, model, missing=missing)
    features = dict(factors.values_bp)
    if "calendar_days" in model.feature_names:
        if factors.calendar_days is None:
            return _status_snapshot(identity, trade_date, generated_at, "reference", ["market_missing"], market_reference, base_nav, profile, model)
        features["calendar_days"] = str(factors.calendar_days)
    raw = _predict_number(model, features)
    # 拟合目标已是正式净值的净收益，不再扣管理费。
    # 南方盘中和中债日终不是已验证的同一口径，仍给出试验数字并标明原因。
    trained_family = str(model.preprocessing.get("source_family") or "")
    cross_family = bool(
        trained_family
        and factors.source_family != trained_family
        and factors.basis_status != "verified_mapping"
    )
    point = pct_text(raw)
    validation = model.validation or {}
    oos = int(validation.get("oos_count") or 0)
    min_oos = int(config.get("min_oos_residuals", 20))
    interval_kind = "none"
    lower = upper = None
    if oos >= min_oos and validation.get("residual_low_pct") is not None:
        interval_kind = "empirical"
        lower = pct_text(Decimal(point) + Decimal(str(validation["residual_low_pct"])))
        upper = pct_text(Decimal(point) + Decimal(str(validation["residual_high_pct"])))
    elif _profile_duration(profile) is not None:
        interval_kind = "scenario"
        band = abs(Decimal(str(_profile_duration(profile))) / Decimal(100))
        lower = pct_text(Decimal(point) - band)
        upper = pct_text(Decimal(point) + band)
    status = "experimental" if cross_family or not _quality_ready(validation, config) else "ready"
    reasons = ["basis_unverified"] if cross_family else []
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
        confidence_label=_label(status, interval_kind),
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
    profile = store.profile_as_of(identity.code, identity.share_class, cutoff)
    baseline = fit_baseline(training, profile, config)
    ridge = fit_ridge(training, config)
    validation = walk_forward_validate(training, config)
    baseline = _with_validation(baseline, validation)
    ridge = _with_validation(ridge, validation)
    use_ridge = _mae_value(validation) < _mae_value({"mae_bp": validation.get("baseline_mae_bp")})
    store.save_model(baseline, active=not use_ridge)
    store.save_model(ridge, active=use_ridge)
    return {"trained": True, "rows": len(training.rows), "validation": validation, "selected": ridge.model_kind if use_ridge else baseline.model_kind}


def project_snapshot(identity, store, factors, base_nav, now, config, market_reference=None, source_family="daily_close") -> EstimateSnapshot:
    generated_at = iso_now(now)
    reason = _support_block(identity)
    if reason:
        return _status_snapshot(identity, factors.as_of if factors else generated_at[:10], generated_at, "unsupported", [reason], market_reference, base_nav, None, None)
    model = store.active_model(identity.code, identity.share_class)
    profile = store.current_profile(identity.code, identity.share_class)
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


def _expanding_metrics(rows, window, config):
    residual_rows = []
    baseline_errors = []
    ridge_errors = []
    for end in range(window, len(rows)):
        sample = rows[end - window:end]
        training = TrainingSet(
            identity=rows[end].get("identity") or FundIdentity("", "", "", "", None, ""),
            cutoff_at=sample[-1]["nav_date"],
            rows=sample,
            feature_names=FACTOR_NAMES,
            source_versions=[],
            profile_version=None,
        )
        # 基线在没有画像时自行估计，保证比较发生在当时可见样本内。
        try:
            baseline = fit_baseline(training, None, config)
            ridge = fit_ridge(training, config)
        except (EstimateInputError, ValueError):
            continue
        actual = Decimal(rows[end]["y"])
        base_hat = _predict_number(baseline, rows[end]["features"])
        ridge_hat = _predict_number(ridge, rows[end]["features"])
        baseline_errors.append(abs(base_hat - actual))
        ridge_errors.append(abs(ridge_hat - actual))
        residual_rows.append((ridge_hat, actual))
    return _metrics_from_pairs(residual_rows, baseline_errors, ridge_errors, len(rows))


def _metrics_from_pairs(pairs, baseline_errors, ridge_errors, sample_count):
    if not pairs:
        return _empty_validation(sample_count, [], "样本外预测没有生成")
    errors = [hat - actual for hat, actual in pairs]
    mae = sum(abs(item) for item in errors) / Decimal(len(errors))
    bias = sum(errors) / Decimal(len(errors))
    hits = compared = 0
    for hat, actual in pairs:
        if hat == 0 or actual == 0:
            continue
        compared += 1
        if (hat > 0) == (actual > 0):
            hits += 1
    ordered = sorted(errors)
    low = ordered[max(0, int(len(ordered) * 0.1) - 1)]
    high = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    covered = 0
    widths = []
    for hat, actual in pairs:
        left = hat + low
        right = hat + high
        widths.append(right - left)
        if left <= actual <= right:
            covered += 1
    return {
        "sample_count": sample_count,
        "oos_count": len(pairs),
        "mae_bp": pct_text(mae * Decimal(100)),
        "bias_bp": pct_text(bias * Decimal(100)),
        "direction_accuracy": None if compared == 0 else pct_text(Decimal(hits) / Decimal(compared)),
        "interval_coverage": pct_text(Decimal(covered) / Decimal(len(pairs))),
        "interval_width_pct": pct_text(sum(widths) / Decimal(len(widths))),
        "baseline_mae_bp": pct_text((sum(baseline_errors) / Decimal(len(baseline_errors))) * Decimal(100)),
        "ridge_mae_bp": pct_text((sum(ridge_errors) / Decimal(len(ridge_errors))) * Decimal(100)),
        "residual_low_pct": pct_text(low),
        "residual_high_pct": pct_text(high),
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
    payload["preprocessing"] = preprocessing
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
    return ModelVersion(
        model_version=model.model_version,
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
        artifact_hash=model.artifact_hash,
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
