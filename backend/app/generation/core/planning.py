"""确定性生成规划与规则语义分流闸门（Layer 5 generation/core，纯函数，零 DB / LLM）。

程序 vs AI 介入边界：
- `derive_generation_plan`：每表的规则/用例配额是预算算术题，不需要也不应该花一次 LLM 调用；
  由字段数、必填/列宽机械规则数、语义候选字段数与唯一性信号直接算出，并经 `_validate_plan` 同口径校验。
- `needs_semantic_rule_binding`：在逐字段调用 AI 绑定规则前做「语义分流」——
  字典校验原文剥离已被确定性规则覆盖的常规短语后，若不再含任何条件/比较/格式/枚举/公式信号，
  且字段不是日期/数值这类常需推断格式与值域的类型、术语说明也没给出约束，则直接跳过 AI（0 Token）。
  宁可多问 AI（只多花 Token），绝不漏问（会丢规则）：所有不确定场景一律判为需要 AI。
"""

import re
from typing import Any, Dict, Iterable, Optional, Tuple

_STRIP_OPTIONAL = re.compile(r"非必填项?|选填项?|可为空|可空|可不填")
_STRIP_REQUIRED = re.compile(r"必填项?")
_STRIP_CODE_SET = re.compile(
    r"(必须|应|需)?(是|在|为|见|参见|参照)?[^，,。；;\n]{0,24}?(代码表|码表)(中|里|内)?(存在)?的?(最细分类的)?(记录|代码|取值|值)?"
)
_STRIP_FIXED = re.compile(r"(本项)?固定(为|填写|填|值)?[^，,。；;\n]{0,24}")
_CONSTRAINT = re.compile(
    r"当|若|如果|假如|否则|则|须|必须|应当|应该|应为|应填|应与|应在|应于|应满足|不应|不得|不能|不可|禁止|只能|仅|"
    r"主键|唯一|重复|大于|小于|不超过|不低于|不少于|不多于|以上|以下|之间|等于|一致|相同|相等|早于|晚于|之前|之后|"
    r"格式|\d+\s*位|第\s*\d+|位数|YYYY|yyyy|yymmdd|MMDD|开头|结尾|分隔|字母|数字|大写|小写|半角|全角|汉字|中文|"
    r"代码表|码表|存在|选择|取值|范围|区间|合计|之和|总额|余额|差额|比例|百分|身份证|校验|邮箱|eMail|email|"
    r"[<>=≥≤＞＜＝]|[A-Za-z0-9]{1,4}\s*[-－—:：]\s*[\u4e00-\u9fffA-Za-z]|(?:^|[^0-9])\d{1,2}\s*、"
)
_DATE_HINT = re.compile(r"日期|时间|年月|年份|月份")
_DATE_NAME = re.compile(r"DATE|TIME|_DT$|DT$", re.IGNORECASE)
_UNIQUE_HINT = re.compile(r"主键|唯一|不可重复|不能重复|不得重复|不允许重复")


def _det_kinds(field_det_rules: Iterable[Any]) -> set:
    kinds = set()
    for rule in field_det_rules or []:
        key = getattr(rule, "rule_key", "") or ""
        if key.endswith("_FIXED_VALUE_ENUM"):
            kinds.add("FIXED_VALUE")
        elif key.endswith("_CODE_SET_ENUM"):
            kinds.add("CODE_SET")
        else:
            kinds.add(getattr(rule, "operator", ""))
    return kinds


def _strip_covered(text: str, kinds: set) -> str:
    """只剥离已被确定性规则完整覆盖的常规短语，保留 `-`/`、`/`:`/`<`/`>` 等内联枚举与比较符号。"""
    residual = _STRIP_OPTIONAL.sub("", text or "")
    if "REQUIRED" in kinds:
        residual = _STRIP_REQUIRED.sub("", residual)
    if "CODE_SET" in kinds:
        residual = _STRIP_CODE_SET.sub("", residual)
    if "FIXED_VALUE" in kinds:
        residual = _STRIP_FIXED.sub("", residual)
    return residual


def _field_term_snippet(terms: str, content: str, all_contents: Iterable[str]) -> str:
    """只提取当前字段在术语说明中的本条定义，绝不跨行串读下一个字段的约束条件。"""
    if not terms or len(content) < 2:
        return ""
    other_contents = [c for c in all_contents if c and c != content and len(c) >= 2]
    lines = terms.splitlines()
    for idx, line in enumerate(lines):
        if content not in line:
            continue
        collected = [line[line.find(content) + len(content) :]]
        for nxt in lines[idx + 1 : idx + 3]:
            stripped = nxt.strip()
            if not stripped:
                break
            # 如果下一行以另一字段的中文名开头，立即停止，避免把相邻字段的条件约束串味到当前字段
            if any(stripped.startswith(other) for other in other_contents):
                break
            collected.append(stripped)
        return " ".join(collected)
    return ""


def needs_semantic_rule_binding(spec: Any, field: Any, field_det_rules: Optional[Iterable[Any]] = None) -> Tuple[bool, str]:
    """判断该字段是否还需要 AI 做语义规则绑定；返回 (是否需要, 原因)。"""
    kinds = _det_kinds(field_det_rules or [])
    if (getattr(field, "data_type", "") or "").upper() == "NUMBER":
        return True, "数值字段可能存在值域/勾稽约束，交 AI 推断"
    content = getattr(field, "content", "") or ""
    name = getattr(field, "name", "") or ""
    if _DATE_HINT.search(content) or _DATE_NAME.search(name):
        return True, "日期/时间字段可能存在格式约束，交 AI 推断"
    residual = _strip_covered(getattr(field, "check_text", "") or "", kinds)
    hit = _CONSTRAINT.search(residual)
    if hit:
        return True, f"校验原文含约束信号「{hit.group(0)}」"
    terms = getattr(spec, "terms_text", "") or ""
    if name and re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", terms):
        return True, "术语说明中直接引用了字段英文名"
    all_contents = [getattr(f, "content", "") or "" for f in (getattr(spec, "fields", None) or [])]
    snippet = _field_term_snippet(terms, content, all_contents)
    if snippet:
        term_residual = _strip_covered(snippet, kinds)
        hit = _CONSTRAINT.search(term_residual)
        if hit:
            return True, f"术语说明含约束信号「{hit.group(0)}」"
    for sr in getattr(spec, "supplementary_rules", None) or ():
        if not isinstance(sr, dict):
            continue
        tfields = {str(x).upper() for x in (sr.get("target_fields") or [])}
        rtext = str(sr.get("rule_text") or "")
        if (
            (name and name.upper() in tfields)
            or (name and re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", rtext))
            or (content and len(content) >= 2 and content in rtext)
        ):
            return True, "存在独立核查规则补充证据，交 AI 结合生成规则"
    covered = "、".join(sorted(k for k in kinds if k)) or "无"
    return False, f"校验原文无残余语义约束（已确定性覆盖：{covered}）"


def derive_generation_plan(spec: Any, budget: Dict[str, Any]) -> Dict[str, Any]:
    """按预算确定性推导每表生成计划（替代原先每表 1 次的 assign_generation_plan LLM 调用）。"""
    max_rules = max(1, int(budget.get("max_rules_per_table", 30)))
    max_cases = max(2, int(budget.get("max_cases_per_table", 60)))
    fields = list(getattr(spec, "fields", []) or [])
    mechanical = sum(1 for f in fields if (f.check_text or "").strip().startswith("必填"))
    mechanical += sum(
        1 for f in fields if (f.data_type or "").upper() == "VARCHAR" and 0 < int(f.length or 0) <= 4000
    )
    semantic = sum(1 for f in fields if needs_semantic_rule_binding(spec, f)[0])
    mandatory = min(mechanical, max_rules)
    expanded = min(semantic, max_rules - mandatory)
    unique_signal = any(_UNIQUE_HINT.search(f.check_text or "") for f in fields)
    golden_count = 2 if unique_signal else 1

    def _estimate(mutations: int) -> int:
        return golden_count + mutations * (mandatory + expanded)

    mutations = 2 if _estimate(2) <= max_cases else 1
    while _estimate(mutations) > max_cases and expanded > 0:
        expanded -= 1
    while _estimate(mutations) > max_cases and mandatory > 0:
        mandatory -= 1
    if golden_count + mutations * (mandatory + expanded) > max_cases:
        golden_count = 1
    rationale = (
        f"确定性预算规划：{len(fields)} 个字段，机械规则约 {mechanical} 条、语义候选字段 {semantic} 个；"
        f"{'检测到唯一性约束，黄金样本取 2 条以校验跨行判重；' if golden_count > 1 else ''}"
        f"每条规则 {mutations} 个变异，预计用例 {golden_count + mutations * (mandatory + expanded)}/{max_cases}。"
    )
    return {
        "mandatory_rule_target": mandatory,
        "expanded_rule_target": expanded,
        "golden_count": golden_count,
        "mutations_per_rule": mutations,
        "rationale": rationale,
    }
