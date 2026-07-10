import pytest

from src.processor import _to_chinese_number, format_report


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "一"),
        (2, "二"),
        (10, "十"),
        (11, "十一"),
        (20, "二十"),
        (21, "二十一"),
        (101, "一百零一"),
        (1001, "一千零一"),
    ],
)
def test_converts_positive_task_indexes_to_chinese(value, expected):
    assert _to_chinese_number(value) == expected


def test_formats_generated_report_with_chinese_numbering():
    report = format_report(
        [
            "【监管数核工具】修复生产环境已知bug：修复生产环境已知bug",
            "【监管数核工具】优化对账逻辑：优化匹配规则",
        ]
    )

    assert report == (
        "一、【监管数核工具】修复生产环境已知bug：修复生产环境已知bug\n"
        "二、【监管数核工具】优化对账逻辑：优化匹配规则"
    )
