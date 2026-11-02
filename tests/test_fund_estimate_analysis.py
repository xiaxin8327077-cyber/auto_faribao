import copy
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.fund_estimate_ai import AiUnavailable, EstimateAi
from src.fund_estimate_service import _verification_overall, _verification_statistics
from src.fund_estimate_store import FundEstimateStore


SHORT_REVIEW = "\n".join([
    "方向判断优于同期间简单基线，现有结果仅反映当日，额外判断力仍需后续净值验证。",
    "预估整体偏低，绝对误差相对实际波动较小，应重点核对偏差是否在相同方向持续出现。",
    "最大偏差来自测试纯债A的当日记录，原因仍待核验，当前资料不足以归因于久期或持仓。",
    "下一净值日优先检查该基金的低估是否重现，并用新增样本重算同期间方向基线再比较。",
])


def sample_data():
    items = [
        {"product_id": "p1", "code": "000001", "name": "测试纯债A", "trade_date": "2026-10-26",
         "predicted_pct": "0.10", "realized_pct": "0.20", "error_pct": "-0.10",
         "direction_ok": True, "snapshot_generated_at": "2026-10-26T17:00:00+08:00",
         "verification_status": "verified"},
        {"product_id": "p2", "code": "000002", "name": "测试纯债B", "trade_date": "2026-10-26",
         "predicted_pct": "-0.10", "realized_pct": "-0.12", "error_pct": "0.02",
         "direction_ok": True, "snapshot_generated_at": "2026-10-26T17:00:00+08:00",
         "verification_status": "verified"},
    ]
    stats = _verification_statistics(items)
    stats.setdefault("pending_returns", 0)
    return items, stats, _verification_overall(items, stats)


@pytest.fixture
def store(tmp_path):
    instance = FundEstimateStore(tmp_path / "estimate.db")
    instance.initialize()
    return instance


class RecordingAi:
    def __init__(self, replies=None, *, release=None, enabled=True):
        self.replies = list(replies or [SHORT_REVIEW])
        self.release = release
        self.enabled = enabled
        self.model = "test-model"
        self.calls = []
        self.started = threading.Event()
        self.lock = threading.Lock()

    def is_enabled(self):
        return self.enabled

    def model_name(self):
        return self.model

    def complete(self, messages, max_tokens=4096):
        with self.lock:
            self.calls.append((copy.deepcopy(messages), max_tokens))
            reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        self.started.set()
        if self.release is not None:
            assert self.release.wait(3), "test must release slow AI"
        if isinstance(reply, Exception):
            raise reply
        return reply


def analysis_for(store, ai):
    from src.fund_estimate_analysis import VerificationAnalysis
    return VerificationAnalysis(store, ai)


def completed(analysis, data, *, status="ready"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = analysis.read(*data)
        if result["ai_status"] == status:
            return result
        time.sleep(0.005)
    pytest.fail(f"analysis did not become {status}: {result}")


def assert_compact(text):
    lines = text.splitlines()
    assert len(lines) == 4
    assert 100 <= len("".join(lines)) <= 180
    assert all(line.endswith("。") for line in lines)


def test_month_day_dates_are_not_treated_as_unsupported_numbers():
    from src.fund_estimate_analysis import _supported_numbers

    payload = {"stats": {"direction_accuracy": 0.65, "mae_pct": 0.01877}}
    text = "10月29日方向准确率为65.000%，平均绝对误差1.877bp。"
    assert _supported_numbers(text, payload) is True


def test_completed_analysis_reports_generating_model(store):
    ai = RecordingAi()
    ai.model = "gpt-6.1-terra"
    analysis = analysis_for(store, ai)
    data = sample_data()
    first = analysis.read(*data)
    assert first["ai_model"] == "gpt-6.1-terra"
    done = completed(analysis, data)
    assert done["ai_analysis"] == SHORT_REVIEW
    assert done["ai_model"] == "gpt-6.1-terra"


def test_switching_model_keeps_completed_analysis_until_manual_regeneration(store):
    ai = RecordingAi(replies=[SHORT_REVIEW, SHORT_REVIEW])
    ai.model = "model-one"
    analysis = analysis_for(store, ai)
    data = sample_data()
    assert completed(analysis, data)["ai_model"] == "model-one"
    assert len(ai.calls) == 1

    ai.model = "model-two"
    reread = analysis.read(*data)
    assert reread["ai_status"] == "ready"
    assert reread['ai_model'] == 'model-one'
    assert len(ai.calls) == 1
    analysis.read(*data, force=True)
    done = completed(analysis, data)
    assert done["ai_model"] == "model-two"
    assert len(ai.calls) == 2


def test_manual_regeneration_bypasses_ready_cache_and_coalesces_double_clicks(store):
    ai = RecordingAi()
    analysis = analysis_for(store, ai)
    data = sample_data()
    completed(analysis, data)
    release = threading.Event(); ai.release = release; ai.started.clear()
    try:
        assert analysis.read(*data, force=True)['ai_status'] == 'generating'
        assert ai.started.wait(1)
        for _ in range(5):
            assert analysis.read(*data, force=True)['ai_status'] == 'generating'
            assert analysis.read(*data)['ai_status'] == 'generating'
        assert len(ai.calls) == 2
    finally:
        release.set()
    assert completed(analysis, data)['ai_analysis'] == SHORT_REVIEW
    assert len(ai.calls) == 2


def _saved_ai_profile(store, label, *, model=None, active=False):
    return store.save_ai_profile(label=label, base_url=f'https://{label}.example/v1',
        model=model or label, api_key='local-dummy-key', clear_api_key=False,
        updated_at='2026-11-02T10:00:00+08:00', activate=active)


def test_queued_analysis_keeps_model_configuration_when_active_profile_changes(store, monkeypatch):
    first = _saved_ai_profile(store, 'model-a', active=True)
    second = _saved_ai_profile(store, 'model-b')
    store.save_ai_settings(enabled=True, jobs_enabled=False, updated_at='2026-11-02T10:00:00+08:00')
    requested_models = []
    def poster(settings, *_):
        requested_models.append(settings['model'])
        return 200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': SHORT_REVIEW}}]})
    analysis = analysis_for(store, EstimateAi(store, poster=poster))
    started, release = threading.Event(), threading.Event()
    original = analysis._generate
    def paused(*args, **kwargs):
        started.set(); assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(analysis, '_generate', paused)
    try:
        assert analysis.read(*sample_data())['ai_model'] == 'model-a'
        assert started.wait(1)
        worker = analysis._worker
        store.activate_ai_profile(second['id'], '2026-11-02T10:01:00+08:00')
    finally:
        release.set()
    worker.join(3)
    assert requested_models == ['model-a']
    store.activate_ai_profile(first['id'], '2026-11-02T10:02:00+08:00')
    assert completed(analysis, sample_data())['ai_model'] == 'model-a'


def test_profiles_with_same_model_name_do_not_share_analysis_cache(store):
    _saved_ai_profile(store, 'provider-a', model='reader', active=True)
    second = _saved_ai_profile(store, 'provider-b', model='reader')
    store.save_ai_settings(enabled=True, jobs_enabled=False, updated_at='2026-11-02T10:00:00+08:00')
    requested_urls = []
    def poster(settings, *_):
        requested_urls.append(settings['base_url'])
        return 200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': SHORT_REVIEW}}]})
    analysis = analysis_for(store, EstimateAi(store, poster=poster))
    completed(analysis, sample_data())
    store.activate_ai_profile(second['id'], '2026-11-02T10:01:00+08:00')
    assert analysis.read(*sample_data())['ai_status'] == 'ready'
    assert requested_urls == ['https://provider-a.example/v1']
    analysis.read(*sample_data(), force=True)
    completed(analysis, sample_data())
    assert requested_urls == ['https://provider-a.example/v1', 'https://provider-b.example/v1']


def test_legacy_completed_analysis_survives_config_cache_migration_without_a_request(store):
    from src.fund_estimate_analysis import _analysis_payload, _json, CACHE_VERSION
    data = sample_data()
    payload = _analysis_payload(*data)
    digest = hashlib.sha256(_json([CACHE_VERSION, payload, 'old-model']).encode()).hexdigest()[:24]
    store.set_watermark(f'verification-analysis:{CACHE_VERSION}:{digest}', json.dumps({
        'version': CACHE_VERSION, 'status': 'ready', 'analysis': SHORT_REVIEW, 'model': 'old-model'}),
        '2026-11-02T10:00:00+08:00')
    _saved_ai_profile(store, 'new-model', active=True)
    store.save_ai_settings(enabled=True, jobs_enabled=False, updated_at='2026-11-02T10:00:00+08:00')
    requests = []
    def poster(*_):
        requests.append(1)
        raise AssertionError('opening unchanged data must not request AI')
    ai = EstimateAi(store, poster=poster)
    result = analysis_for(store, ai).read(*data)
    assert result == {'ai_status': 'ready', 'ai_model': 'old-model', 'ai_analysis': SHORT_REVIEW}
    assert analysis_for(FundEstimateStore(store.path), ai).read(*data) == result
    assert requests == []


def test_last_explicit_model_choice_controls_result_when_forced_requests_interleave(store):
    first = _saved_ai_profile(store, 'model-b', active=True)
    second = _saved_ai_profile(store, 'model-c')
    store.save_ai_settings(enabled=True, jobs_enabled=False, updated_at='2026-11-02T10:00:00+08:00')
    started, release = threading.Event(), threading.Event()
    calls = []
    def poster(settings, *_):
        calls.append(settings['model'])
        if len(calls) == 1:
            started.set(); assert release.wait(3)
        return 200, json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': SHORT_REVIEW}}]})
    ai = EstimateAi(store, poster=poster)
    analysis = analysis_for(store, ai)
    data = sample_data()
    try:
        assert analysis.read(*data, force=True)['ai_model'] == 'model-b'
        assert started.wait(1)
        worker = analysis._worker
        store.activate_ai_profile(second['id'], '2026-11-02T10:01:00+08:00')
        assert analysis.read(*data, force=True)['ai_model'] == 'model-c'
        store.activate_ai_profile(first['id'], '2026-11-02T10:02:00+08:00')
        assert analysis.read(*data, force=True)['ai_model'] == 'model-b'
    finally:
        release.set()
    worker.join(3)
    assert analysis.read(*data)['ai_model'] == 'model-b'
    restarted = analysis_for(FundEstimateStore(store.path), ai)
    assert restarted.read(*data)['ai_model'] == 'model-b'
    assert calls == ['model-b', 'model-c']


def test_slow_ai_does_not_block_read_and_repeated_calls_share_generation(store):
    release = threading.Event()
    ai = RecordingAi(release=release)
    analysis = analysis_for(store, ai)
    data = sample_data()
    try:
        started = time.monotonic()
        first = analysis.read(*data)
        assert time.monotonic() - started < 0.20
        assert first["ai_status"] == "generating"
        assert_compact(first["ai_analysis"])
        assert ai.started.wait(1)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: analysis.read(*data), range(40)))
        assert all(result["ai_status"] == "generating" for result in results)
        assert len(ai.calls) == 1
    finally:
        release.set()
    assert completed(analysis, data)["ai_analysis"] == SHORT_REVIEW


def test_persistent_cache_restores_current_version_without_ai_request(store):
    data = sample_data()
    first_ai = RecordingAi()
    completed(analysis_for(store, first_ai), data)
    restored_store = FundEstimateStore(store.path)
    restored = analysis_for(restored_store, RecordingAi([RuntimeError("must not call")]))
    assert restored.read(*data) == {"ai_analysis": SHORT_REVIEW, "ai_status": "ready", "ai_model": "test-model"}
    assert not restored.ai.calls


def test_old_long_cache_version_is_ignored(store):
    items, stats, overall = sample_data()
    old_digest = hashlib.sha256(json.dumps(["overall-v3", items, stats], ensure_ascii=False,
                                          sort_keys=True).encode()).hexdigest()[:16]
    old_text = "历史长点评" * 120
    store.set_watermark(f"verification-analysis:{old_digest}", old_text, "2026-10-26")
    ai = RecordingAi()
    result = completed(analysis_for(store, ai), (items, stats, overall))
    assert result["ai_analysis"] == SHORT_REVIEW
    assert len(ai.calls) == 1


def test_new_data_never_displays_previous_version_commentary(store):
    ai = RecordingAi()
    analysis = analysis_for(store, ai)
    completed(analysis, sample_data())
    release = threading.Event()
    ai.release = release
    ai.started.clear()
    items, _, _ = sample_data()
    items[0].update(realized_pct="0.30", error_pct="-0.20")
    stats = _verification_statistics(items)
    data = items, stats, _verification_overall(items, stats)
    try:
        result = analysis.read(*data)
        assert result["ai_status"] == "generating"
        assert result["ai_analysis"] != SHORT_REVIEW
        assert_compact(result["ai_analysis"])
        assert ai.started.wait(1)
    finally:
        release.set()
    completed(analysis, data)


def test_failed_generation_persists_cooldown_and_can_retry_after_deadline(store, monkeypatch):
    import src.fund_estimate_analysis as module
    clock = [1_800_000_000.0]
    monkeypatch.setattr(module, "time", lambda: clock[0])
    ai = RecordingAi([RuntimeError("unavailable"), SHORT_REVIEW])
    analysis = analysis_for(store, ai)
    data = sample_data()
    result = completed(analysis, data, status="failed")
    assert_compact(result["ai_analysis"])
    for _ in range(15):
        assert analysis.read(*data)["ai_status"] == "failed"
    assert len(ai.calls) == 1
    restarted = analysis_for(FundEstimateStore(store.path), ai)
    assert restarted.read(*data)["ai_status"] == "failed"
    assert len(ai.calls) == 1
    clock[0] += 3600
    assert completed(restarted, data)["ai_analysis"] == SHORT_REVIEW
    assert len(ai.calls) == 2


def test_long_review_gets_one_compression_request_with_compatible_reasoning_budget(store):
    ai = RecordingAi(["这是超长点评，需要压缩。" * 60, SHORT_REVIEW])
    result = completed(analysis_for(store, ai), sample_data())
    assert result["ai_analysis"] == SHORT_REVIEW
    assert len(ai.calls) == 2
    assert all(budget == 4096 for _, budget in ai.calls)
    assert "压缩" in ai.calls[1][0][-1]["content"]


@pytest.mark.parametrize("bad_text", ["这是超长点评。" * 60, "\n".join(["重复罗列。"] * 5), ""],
                         ids=["too-long", "too-many-lines", "empty"])
def test_invalid_output_after_one_compression_uses_complete_four_dimension_summary(store, bad_text):
    ai = RecordingAi([bad_text, bad_text])
    result = completed(analysis_for(store, ai), sample_data(), status="failed")
    assert len(ai.calls) == 2
    text = result["ai_analysis"]
    assert_compact(text)
    assert "基线" in text
    assert "偏低" in text and "实际" in text
    assert "测试纯债A" in text and "2026-10-26" in text
    assert "核对" in text and "仅反映当日" in text
    assert text != bad_text[:180]


def test_ai_disabled_uses_short_deterministic_summary_and_no_model_request(store):
    ai = RecordingAi(enabled=False)
    result = analysis_for(store, ai).read(*sample_data())
    assert result["ai_status"] == "disabled"
    assert_compact(result["ai_analysis"])
    assert ai.calls == []


def test_no_items_has_no_background_generation(store):
    ai = RecordingAi()
    assert analysis_for(store, ai).read([], {}, {}) == {"ai_analysis": None, "ai_status": "ready"}
    assert ai.calls == []


def test_allowlisted_payload_and_cache_identity_exclude_holding_money_and_shares(store):
    data = sample_data()
    items, stats, overall = copy.deepcopy(data)
    private_values = {"holding_amount": "987654321.09", "holding_shares": "876543210.12",
                      "predicted_profit": "765432109.23", "realized_profit": "654321098.34",
                      "profit_error": "543210987.45"}
    items[0].update(private_values)
    stats.update(private_values)
    overall.update(private_values, summary="私人金额987654321.09元")
    overall["by_fund"][0].update(private_values)
    overall["by_day"][0].update(private_values)
    overall["diagnostics"].update(private_values)
    ai = RecordingAi()
    analysis = analysis_for(store, ai)
    completed(analysis, (items, stats, overall))
    request = json.dumps(ai.calls, ensure_ascii=False)
    assert all(key not in request and value not in request for key, value in private_values.items())
    payload = json.loads(ai.calls[0][0][-1]["content"].split("数据：", 1)[1])
    assert payload["period"]["day_count"] == 1
    assert payload["stats"].get("pending_returns") == stats.get("pending_returns", 0)
    assert payload["items"][0]["predicted_pct"] == "0.10"
    assert payload["diagnostics"]["mae_bp"] == data[2]["diagnostics"]["mae_bp"]
    assert analysis.read(*data)["ai_status"] == "ready"
    assert len(ai.calls) == 1


def test_pending_returns_do_not_invent_comparable_results_or_dividend_causes(store):
    items = [{"product_id": "p1", "code": "000001", "name": "测试纯债A",
              "trade_date": "2026-10-26", "predicted_pct": "0.10", "realized_pct": None,
              "error_pct": None, "direction_ok": None, "verification_status": "pending_return"}]
    stats = _verification_statistics(items)
    overall = _verification_overall(items, stats)
    result = analysis_for(store, None).read(items, stats, overall)
    text = result["ai_analysis"]
    assert "无法判断" in text
    assert "收益口径" in text and "核对" in text
    assert "拖累" in text
    assert "分红导致" not in text
    assert len(text.replace("\n", "")) <= 180


def test_mock_with_legacy_complete_signature_remains_supported(store):
    class LegacyAi:
        def is_enabled(self):
            return True

        def complete(self, messages):
            return SHORT_REVIEW

    assert completed(analysis_for(store, LegacyAi()), sample_data())["ai_analysis"] == SHORT_REVIEW


def test_new_versions_share_one_worker_and_keep_their_own_cache(store):
    release = threading.Event()
    ai = RecordingAi(release=release)
    analysis = analysis_for(store, ai)
    first_data = sample_data()
    items, _, _ = sample_data()
    items[0].update(realized_pct="0.30", error_pct="-0.20")
    stats = _verification_statistics(items)
    second_data = items, stats, _verification_overall(items, stats)
    try:
        analysis.read(*first_data)
        assert ai.started.wait(1)
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda _: analysis.read(*second_data), range(40)))
        assert len(ai.calls) == 1
    finally:
        release.set()
    completed(analysis, second_data)
    assert len(ai.calls) == 2
    assert analysis.read(*first_data)["ai_status"] == "ready"


@pytest.mark.parametrize("replacement", [
    "方向判断为1.000%，现有结果仅反映当日，额外判断力仍需后续净值验证。",
    "方向判断为88.888%，现有结果仅反映当日，额外判断力仍需后续净值验证。",
    "因为基金久期偏长，预估持续低估，下一净值日无需再核验该原因。",
    "好。",
], ids=["ratio-wrong-unit", "invented-number", "unsupported-cause", "unhelpfully-short"])
def test_incorrect_numbers_or_unsupported_cause_get_one_rewrite_then_fallback(store, replacement):
    malformed = SHORT_REVIEW.splitlines()
    malformed[0] = replacement
    malformed = "\n".join(malformed)
    if replacement == "好。":
        malformed = "\n".join(["好。"] * 4)
    ai = RecordingAi([malformed, malformed])
    result = completed(analysis_for(store, ai), sample_data(), status="failed")
    assert len(ai.calls) == 2
    assert_compact(result["ai_analysis"])


def test_grounded_percent_and_bp_values_and_qualified_hypotheses_are_accepted(store):
    lines = SHORT_REVIEW.splitlines()
    lines[0] = "方向一致率为100.000%，超过同期间简单基线，当前结论仅反映当日，仍需新增记录复核。"
    lines[1] = "平均绝对误差为6.000bp，小于实际平均波动，整体偏低，需检查同方向偏差是否再次出现。"
    lines[2] = "最大偏差来自测试纯债A，久期差异仅为待核验假设，应结合正式报告检查，不能直接认定。"
    text = "\n".join(lines)
    ai = RecordingAi([text])
    result = completed(analysis_for(store, ai), sample_data())
    assert result["ai_analysis"] == text
    assert len(ai.calls) == 1


@pytest.mark.parametrize("predicted,realized", [("0", "0"), ("0.2", "0.2"), ("0.3", "-0.2"), ("-0.3", "0.2")])
def test_deterministic_summary_is_short_for_zero_and_signed_error_branches(store, predicted, realized):
    from decimal import Decimal
    items, _, _ = sample_data()
    for item in items:
        item.update(name="这是十个中文字符基金名", predicted_pct=predicted, realized_pct=realized,
                    error_pct=str(Decimal(predicted) - Decimal(realized)),
                    direction_ok=(Decimal(predicted).compare(0) == Decimal(realized).compare(0)))
    stats = _verification_statistics(items)
    result = analysis_for(store, None).read(items, stats, _verification_overall(items, stats))
    assert_compact(result["ai_analysis"])


def test_legacy_ai_internal_type_error_does_not_trigger_duplicate_request(store):
    class BrokenAi:
        calls = 0

        def is_enabled(self):
            return True

        def complete(self, messages, max_tokens=4096):
            self.calls += 1
            raise TypeError("internal model adapter error")

    ai = BrokenAi()
    completed(analysis_for(store, ai), sample_data(), status="failed")
    assert ai.calls == 1


def test_longest_summary_labels_and_missing_diagnostics_still_fit_short_budget(store):
    items, stats, overall = sample_data()
    items[0].update(name="长" * 10, trade_date="2026-10-25")
    overall.update(day_count=2, diagnostics={})
    assert_compact(analysis_for(store, None).read(items, stats, overall)["ai_analysis"])


def test_reasoning_budget_upgrade_ignores_old_failure_cooldown_and_persists_new_version(store):
    from src.fund_estimate_analysis import _analysis_payload
    data = sample_data()
    payload = _analysis_payload(*data)

    def key_for(version):
        digest = hashlib.sha256(json.dumps([version, payload, "test-model"], ensure_ascii=False,
                                          sort_keys=True, default=str).encode()).hexdigest()[:24]
        return f"verification-analysis:{version}:{digest}"

    store.set_watermark(key_for("short-v1"), json.dumps({
        "version": "short-v1", "status": "failed", "analysis": "旧512预算失败摘要",
        "attempts": 9, "retry_at": 9_999_999_999.0,
    }), "2026-10-27T21:26:50+08:00")
    ai = RecordingAi()
    analysis = analysis_for(store, ai)
    assert analysis.read(*data)["ai_status"] == "generating"
    assert completed(analysis, data)["ai_analysis"] == SHORT_REVIEW
    assert len(ai.calls) == 1 and ai.calls[0][1] == 4096
    cached = json.loads(store.get_watermark(key_for("short-v2")))
    assert cached["version"] == "short-v2"
    assert cached["status"] == "ready"


def test_estimate_ai_allows_short_budget_and_keeps_default_document_budget():
    class Settings:
        def secret_ai_settings(self):
            return {"enabled": True, "base_url": "https://example.invalid/v1", "api_key": "key", "model": "m"}

    budgets = []

    def poster(settings, messages, max_tokens):
        budgets.append(max_tokens)
        return 200, json.dumps({"choices": [{"message": {"content": "结果"}, "finish_reason": "stop"}]})

    ai = EstimateAi(Settings(), poster=poster)
    assert ai.complete([{"role": "user", "content": "短评"}], max_tokens=512) == "结果"
    assert ai.complete([{"role": "user", "content": "提取报告"}]) == "结果"
    assert budgets == [512, 4096]


def test_estimate_ai_reports_length_before_missing_content_for_reasoning_only_response():
    class Settings:
        def secret_ai_settings(self):
            return {"enabled": True, "base_url": "https://example.invalid/v1", "api_key": "key", "model": "m"}

    def poster(settings, messages, max_tokens):
        return 200, json.dumps({"choices": [{"message": {"role": "assistant", "reasoning_content": "推理正文尚未完成"},
                                            "finish_reason": "length"}]})

    with pytest.raises(AiUnavailable, match="截断"):
        EstimateAi(Settings(), poster=poster).complete([{"role": "user", "content": "短评"}], max_tokens=512)
