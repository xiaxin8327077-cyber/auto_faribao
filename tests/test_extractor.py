from src.config import SourceConfig
from src.extractor import _build_extract_eval_config, _format_task_row, _format_task_rows


def test_source_config_defaults_project_field():
    assert SourceConfig({}).project_field == "所属项目"
    assert SourceConfig({"project_field": "关联项目"}).project_field == "关联项目"


def test_formats_task_with_project_and_description():
    assert _format_task_row(
        {
            "project": "数据管理平台实施工作",
            "name": "处理数据质量问题",
            "description": "完成规则调整",
        }
    ) == "【数据管理平台实施工作】处理数据质量问题：完成规则调整"


def test_formats_task_with_project_without_description():
    assert _format_task_row(
        {
            "project": "数据管理平台实施工作",
            "name": "处理数据质量问题",
            "description": "",
        }
    ) == "【数据管理平台实施工作】处理数据质量问题"


def test_project_placeholder_and_empty_project_keep_legacy_format():
    rows = [
        {"project": "...", "name": "任务一", "description": "说明"},
        {"project": "", "name": "任务二", "description": ""},
    ]

    assert _format_task_rows(rows) == ["任务一：说明", "任务二"]


def test_format_task_rows_keeps_list_of_strings_contract():
    assert _format_task_rows(
        [{"project": "项目甲", "name": "任务一", "description": ""}]
    ) == ["【项目甲】任务一"]


def test_extract_eval_config_includes_project_field():
    cfg = SourceConfig({"project_field": "关联项目"})

    assert _build_extract_eval_config(cfg)["projectField"] == "关联项目"
