"""多套报告提取模型配置的存储行为。"""
import json

from src.fund_estimate_store import FundEstimateStore

STAMP = "2026-10-30T10:00:00+08:00"


def make_store(tmp_path):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    return store


def add_profile(store, label, base_url, model, api_key, *, activate=False, stamp=STAMP):
    return store.save_ai_profile(
        label=label,
        base_url=base_url,
        model=model,
        api_key=api_key,
        clear_api_key=False,
        updated_at=stamp,
        activate=activate,
    )


def test_multiple_profiles_keep_keys_secret_and_activate_one(tmp_path):
    store = make_store(tmp_path)
    first = add_profile(
        store, "LongCat", "https://api.longcat.chat/openai", "LongCat-2.5-Preview",
        "sk-secret-longcat", activate=True,
    )
    second = add_profile(
        store, "百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-plus",
        "sk-secret-qwen",
    )

    view = store.public_ai_settings()
    assert view["active_profile_id"] == first["id"]
    assert [item["label"] for item in view["profiles"]] == ["LongCat", "百炼"]
    assert [item["active"] for item in view["profiles"]] == [True, False]
    assert "sk-secret" not in json.dumps(view)
    assert view["profiles"][0]["api_key_configured"] is True
    assert view["profiles"][0]["api_key_hint"] == "····gcat"
    # 顶层镜像字段跟随当前激活配置，兼容旧前端。
    assert view["base_url"] == "https://api.longcat.chat/openai"
    assert view["model"] == "LongCat-2.5-Preview"

    store.activate_ai_profile(second["id"], "2026-10-30T10:01:00+08:00")
    secret = store.secret_ai_settings()
    assert secret["model"] == "qwen3.7-plus"
    assert secret["api_key"] == "sk-secret-qwen"
    assert secret["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert secret["profile_id"] == second["id"]


def test_saving_flat_settings_updates_active_profile_instead_of_duplicating(tmp_path):
    store = make_store(tmp_path)
    store.save_ai_settings(
        enabled=True, jobs_enabled=False,
        base_url="https://models.example.test/v1", model="reader-v1",
        api_key="key-one", clear_api_key=False, updated_at=STAMP,
    )
    store.save_ai_settings(
        enabled=True, jobs_enabled=True,
        base_url="https://models.example.test/v1", model="reader-v2",
        api_key=None, clear_api_key=False, updated_at="2026-10-30T10:02:00+08:00",
    )

    view = store.public_ai_settings()
    assert len(view["profiles"]) == 1
    assert view["profiles"][0]["model"] == "reader-v2"
    assert view["enabled"] is True
    assert view["jobs_enabled"] is True
    # 留空密钥时保留原值。
    assert store.secret_ai_settings()["api_key"] == "key-one"


def test_clear_api_key_only_affects_target_profile(tmp_path):
    store = make_store(tmp_path)
    first = add_profile(store, "A", "https://a.example.test/v1", "m-a", "key-a", activate=True)
    second = add_profile(store, "B", "https://b.example.test/v1", "m-b", "key-b")

    store.save_ai_profile(
        label="B", base_url="https://b.example.test/v1", model="m-b",
        api_key=None, clear_api_key=True, updated_at="2026-10-30T10:03:00+08:00",
        profile_id=second["id"], activate=False,
    )
    view = store.public_ai_settings()
    by_id = {item["id"]: item for item in view["profiles"]}
    assert by_id[first["id"]]["api_key_configured"] is True
    assert by_id[second["id"]]["api_key_configured"] is False
    assert store.ai_profile_secret(second["id"])["api_key"] == ""
    assert store.ai_profile_secret(first["id"])["api_key"] == "key-a"


def test_deleting_active_profile_promotes_another_and_clears_last(tmp_path):
    store = make_store(tmp_path)
    first = add_profile(store, "A", "https://a.example.test/v1", "m-a", "key-a", activate=True)
    second = add_profile(store, "B", "https://b.example.test/v1", "m-b", "key-b")

    store.delete_ai_profile(first["id"], "2026-10-30T10:04:00+08:00")
    view = store.public_ai_settings()
    assert [item["id"] for item in view["profiles"]] == [second["id"]]
    assert view["active_profile_id"] == second["id"]
    assert store.secret_ai_settings()["api_key"] == "key-b"

    store.delete_ai_profile(second["id"], "2026-10-30T10:05:00+08:00")
    view = store.public_ai_settings()
    assert view["profiles"] == []
    assert view["active_profile_id"] is None
    assert view["base_url"] == ""
    assert view["model"] == ""
    secret = store.secret_ai_settings()
    assert secret["api_key"] == ""
    assert secret["profile_id"] is None


def test_editing_inactive_profile_does_not_switch_active(tmp_path):
    store = make_store(tmp_path)
    first = add_profile(store, "A", "https://a.example.test/v1", "m-a", "key-a", activate=True)
    second = add_profile(store, "B", "https://b.example.test/v1", "m-b", "key-b")

    store.save_ai_settings(
        enabled=True, jobs_enabled=False,
        profile_id=second["id"], label="B2", base_url="https://b.example.test/v1",
        model="m-b2", api_key=None, clear_api_key=False,
        updated_at="2026-10-30T10:06:00+08:00", activate=False,
    )
    view = store.public_ai_settings()
    assert view["active_profile_id"] == first["id"]
    assert [item["model"] for item in view["profiles"]] == ["m-a", "m-b2"]
    assert store.secret_ai_settings()["api_key"] == "key-a"


def test_legacy_single_row_is_migrated_once(tmp_path):
    store = FundEstimateStore(tmp_path / "fund_estimation.db")
    store.initialize()
    with store._connect() as conn:
        conn.execute(
            """UPDATE ai_model_settings
               SET enabled = 1, jobs_enabled = 1, base_url = ?, model = ?, api_key = ?
               WHERE id = 1""",
            ("https://legacy.example.test/v1", "legacy-model", "legacy-key"),
        )

    store.initialize()
    view = store.public_ai_settings()
    assert len(view["profiles"]) == 1
    assert view["profiles"][0]["base_url"] == "https://legacy.example.test/v1"
    assert view["profiles"][0]["model"] == "legacy-model"
    assert view["active_profile_id"] == view["profiles"][0]["id"]
    assert view["enabled"] is True
    assert view["jobs_enabled"] is True
    assert "legacy-key" not in json.dumps(view)

    # 再次初始化不应重复迁移。
    store.initialize()
    assert len(store.public_ai_settings()["profiles"]) == 1
