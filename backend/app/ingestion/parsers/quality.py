"""切块后的结构自检。

目的不是替代 AI 复核，而是在 AI 之前把"确定是解析坏了"的块挑出来。
公式锚点错位、折行串行、列切错这些问题，在字段结构里都会留下很硬的痕迹
（校验原文只剩一个"+"、字段名只剩一个字），这类判断不需要模型。
"""

import re
from typing import Any, Dict, List

# 校验原文短到这种程度的，基本可以断定是列切错或折行串行留下的残片
FRAGMENT_MAX_LEN = 3
# 这些短词本身是合法的校验说明，不能算碎片
LEGIT_SHORT_CHECK = {"必填项", "非必填", "必填", "选填", "必填项。", "非必填项"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def inspect_spec(spec: Dict[str, Any], *, table_code: str = "") -> List[Dict[str, Any]]:
    """检查一份字段结构，返回确定性问题列表。

    只报"几乎不可能是正常情况"的问题，宁可漏报也不误报——
    误报会让用户对自检失去信任，反而增加核对成本。
    """
    issues: List[Dict[str, Any]] = []
    fields = spec.get("fields") or []
    if not fields:
        return [
            {
                "severity": "ERROR",
                "code": "EMPTY_SPEC",
                "field": "",
                "message": "这个块没有解析出任何字段，切块位置可能不对",
            }
        ]

    seen: Dict[str, int] = {}
    for field in fields:
        name = _text(field.get("name"))
        content = _text(field.get("content"))
        check = _text(field.get("check_text"))
        data_type = _text(field.get("data_type"))

        if not name:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "EMPTY_FIELD_NAME",
                    "field": "",
                    "message": "存在没有字段名的条目，通常是切块时把表头行切丢了",
                }
            )
            continue
        seen[name] = seen.get(name, 0) + 1

        if not data_type:
            issues.append(
                {
                    "severity": "WARNING",
                    "code": "MISSING_TYPE",
                    "field": name,
                    "message": "没有识别出数据类型",
                }
            )

        if check and len(check) <= FRAGMENT_MAX_LEN and check not in LEGIT_SHORT_CHECK:
            issues.append(
                {
                    "severity": "ERROR",
                    "code": "FRAGMENT_CHECK_TEXT",
                    "field": name,
                    "message": f"校验原文只剩「{check}」，疑似被公式或折行切断（列错位）",
                }
            )

        # "数量""金额""备注"这类两字字段名是正常的，只有单字才几乎可以断定是折行截断
        if content and len(content) == 1:
            issues.append(
                {
                    "severity": "WARNING",
                    "code": "TRUNCATED_FIELD_NAME",
                    "field": name,
                    "message": f"字段中文名只有「{content}」，疑似折行后只取了第一行",
                }
            )

        if not check:
            issues.append(
                {
                    "severity": "WARNING",
                    "code": "MISSING_CHECK_TEXT",
                    "field": name,
                    "message": "没有解析到校验说明",
                }
            )

    duplicates = [name for name, count in seen.items() if count > 1]
    if duplicates:
        issues.append(
            {
                "severity": "ERROR",
                "code": "DUPLICATE_FIELD",
                "field": duplicates[0],
                "message": f"字段名重复：{'、'.join(duplicates[:5])}",
            }
        )

    return issues


def summarize(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "errors": sum(1 for item in issues if item.get("severity") == "ERROR"),
        "warnings": sum(1 for item in issues if item.get("severity") == "WARNING"),
    }


def inspect_block(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    """对 extract_document 产出的单个块做自检。"""
    content = block.get("content_json") or {}
    spec = content.get("spec") or content.get("parent_spec")
    if spec:
        return inspect_spec(spec, table_code=str(spec.get("table_code") or ""))
    code_spec = content.get("code_set_spec") or {}
    if code_spec:
        values = code_spec.get("values") or []
        if not values:
            return [
                {
                    "severity": "ERROR",
                    "code": "EMPTY_CODE_SET",
                    "field": "",
                    "message": "码表块没有解析出任何码值",
                }
            ]
    return []
