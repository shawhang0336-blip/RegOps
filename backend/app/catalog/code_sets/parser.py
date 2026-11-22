"""码值文本解析与命名归一化（Code Set Parser）。

纯函数层（无数据库依赖）：
- `CodeValueDraft` / `CodeSetDraft`：码表草稿契约与 `validate_code_set_draft`；
- `parse_inline_enum`：解析「Y-是；N-否」「①政府；②央行」「ENUM:[...]」及纯分号名词列表；
- `normalize_code_set_name`：码表同义词映射与前后缀归一化；
- `is_rule_reference` / `parse_fixed_value` / `is_conditional_phrase`：区分格式规则、固定值与条件句。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class CodeValueDraft(BaseModel):
    value: str = Field(min_length=1)
    label_cn: str = Field(min_length=1)
    label_en: str = ""
    definition: str = ""


class CodeSetDraft(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = ""
    source_type: str = "DOCUMENT_TABLE"
    values: List[CodeValueDraft] = Field(default_factory=list)


CODE_NAME_SYNONYMS = {
    "国别": "国家和地区",
    "国别和地区": "国家和地区",
    "国家地区": "国家和地区",
    "国家和地区": "国家和地区",
    "行业属性": "行业类别",
    "行业": "行业类别",
    "所属行业": "行业类别",
    "行业类别": "行业类别",
    "国民经济行业": "行业类别",
    "行政区划": "中国大陆行政区划",
    "中国大陆行政区划": "中国大陆行政区划",
    "地区": "中国大陆行政区划",
    "交易编码": "交易代码",
    "交易代码": "交易代码",
    # "国际收支交易编码表"与"涉外收支交易分类与代码"是同一张 6 位交易代码表的两种叫法
    "国际收支交易": "交易",
    "国际收支交易编码": "交易",
    "涉外收支交易": "交易",
    "涉外收支交易分类": "交易",
    "涉外收支交易分类与代码": "交易",
    "对方与本机构的关系": "对方与本机构被代理居民机构委托人的关系",
    "对方与本机构被代理居民机构委托人的关系": "对方与本机构被代理居民机构委托人的关系",
    "外债的债务类型": "债务类型",
}


CIRCLED_NUM = "①②③④⑤⑥⑦⑧⑨⑩"
CIRCLED_INDEX = {char: str(index + 1) for index, char in enumerate(CIRCLED_NUM)}

# 内联枚举里的噪声片段：默认值说明、填写要求等。原文常写成
# "Y-是；N-否；默认N"，旧的整段解析会因为这个片段而全盘放弃。
ENUM_NOISE_RE = re.compile(r"^(?:默认|默认为|不填|留空|可选|必填|填写|请填|说明|注[：:]|见[：:])")
_ENUM_TOKENS = r"A-Za-z0-9" + CIRCLED_NUM
_ENUM_FORWARD_RE = re.compile(rf"^([{_ENUM_TOKENS}]+)\s*[\-—–－:：、]\s*(.+)$")
_ENUM_GLUED_RE = re.compile(rf"^([{_ENUM_TOKENS}]{{1,6}})([^\x00-\x7F].*)$")
_ENUM_REVERSE_RE = re.compile(rf"^(.+?)\s*[\-—–－:：、]\s*([{_ENUM_TOKENS}]+)$")


def _split_enum_fragments(text: str) -> List[str]:
    """按圆圈序号或中英文分隔符切成候选片段，丢弃序号之前的说明文字。"""
    raw = (text or "").strip()
    if not raw:
        return []
    positions = [index for index, char in enumerate(raw) if char in CIRCLED_INDEX]
    if len(positions) >= 2:
        # "按九个子部门码值填列：①政府；②央行…" → 从第一个序号开始切
        body = raw[positions[0]:]
        return [part.strip() for part in re.split(rf"(?=[{CIRCLED_NUM}])", body) if part.strip()]
    return [part.strip() for part in re.split(r"[；;，,、\n]", raw) if part.strip()]


def _parse_enum_pair(part: str) -> Optional[Tuple[str, str]]:
    """把单个片段解析成 (码值, 名称)；不是"码-值"结构就返回 None。"""
    text = (part or "").strip().strip("。.;；,，、:： ")
    if not text:
        return None
    match = _ENUM_FORWARD_RE.match(text) or _ENUM_GLUED_RE.match(text)
    if match:
        code, label = match.group(1).strip(), match.group(2).strip()
    else:
        match = _ENUM_REVERSE_RE.match(text)
        if not match:
            return None
        label, code = match.group(1).strip(), match.group(2).strip()
    label = label.strip("。.;；,，、 ")
    if not code or not label or code == label:
        return None
    if len(code) > 8 or len(label) > 80:
        return None
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", label):
        return None
    return CIRCLED_INDEX.get(code, code)[:20], label[:80]


_ENUM_LIST_RE = re.compile(r"\[([^\[\]]{4,2000})\]")


def _parse_enum_list(text: str) -> List[Dict[str, str]]:
    """解析 AI 复核给出的 ENUM：["上市普通股","非上市普通股（但可交易）"] 这种形式。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if not re.search(r"\bENUM\b", raw, re.IGNORECASE) and not raw.startswith("["):
        return []
    match = _ENUM_LIST_RE.search(raw)
    if not match:
        return []
    labels = re.findall(r"[\"“]([^\"”]{1,80})[\"”]", match.group(1))
    if len(labels) < 2:
        return []
    return [
        {"value": str(index + 1), "label_cn": label.strip()[:80]}
        for index, label in enumerate(labels)
        if label.strip()
    ]


def parse_inline_enum(text: str) -> List[Dict[str, str]]:
    """把 "Y-是；N-否" / "①政府；②央行" / "是-Y，否-N" 这类内联枚举解析成码值对；解析失败返回空。

    逐片段解析：个别片段（如"默认N""填写N"）解析不出来只跳过它，不再整段放弃；
    至少要有 2 个有效码值才认为是枚举，避免把普通句子误判成码表。
    """
    listed = _parse_enum_list(text)
    if listed:
        return listed
    values: List[Dict[str, str]] = []
    seen = set()
    for part in _split_enum_fragments(text):
        if ENUM_NOISE_RE.match(part):
            continue
        pair = _parse_enum_pair(part)
        if pair is None and re.search(r"[：:]", part):
            # "交易方式枚举：1-线下" —— 去掉说明前缀再试一次
            pair = _parse_enum_pair(re.split(r"[：:]", part)[-1])
        if pair is None:
            continue
        code, label = pair
        if code in seen:
            continue
        seen.add(code)
        values.append({"value": code, "label_cn": label})
    if len(values) < 2:
        # 「一般贸易区；自由贸易试验区；保税区；…」——没有数字标记的纯分号名词列表。
        # 它就是一张内联枚举（TAXFREECODE 就是这种），之前解析不出被误关联到别的码表。
        fallback = _parse_bare_semicolon_list(text)
        if fallback:
            return fallback
        return []
    return values[:200]


def _parse_bare_semicolon_list(text: str) -> List[Dict[str, str]]:
    """纯分号分隔的名词短语列表 → 枚举（value 用序号）。

    只在没有数字标记时兜底用；含「填/为/必须/如/当」这类动词的按句子处理，不算枚举。
    """
    if not text or "；" not in text:
        return []
    if re.search(r"[填必须当如若为]", text):
        return []
    parts = [part.strip() for part in text.split("；") if part.strip()]
    if len(parts) < 2:
        return []
    if not all(1 < len(part) <= 25 for part in parts):
        return []
    return [{"value": str(index + 1), "label_cn": part} for index, part in enumerate(parts)]


def normalize_code_set_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    text = raw_name.strip()
    # 先去掉括号及内容（如"（3位字母代码）"），避免其污染核心名
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(
        r"^(?:参?见|参照|本制度|数据采集规范最新版本中的相关代码表或本制度|数据采集规范(?:最新版本)?(?:中|中的)?的?)+",
        "",
        text,
    )
    # 去掉"附录"残留（括号在上一步已移除）
    text = re.sub(r"(?:五、附录|附[录表])", "", text)
    # "…代码表填列"这类尾巴：先摘掉"填列/填报"，否则后面的"代码表"剥离会被挡住
    text = re.sub(r"(?:填写|填报|填列|填制|填用)$", "", text)
    text = re.sub(r"(?:代码表|分类标准表|分类标准|代码|名录|表|填写|填)+$", "", text)
    # 剥离后缀后可能留下连接词（"涉外收支交易分类与代码" → "…分类与"）
    text = re.sub(r"[与和及或等的]+$", "", text)
    text = re.sub(r"[\s/（）()—\-·:：，,。]", "", text)
    return CODE_NAME_SYNONYMS.get(text, text)


def validate_code_set_draft(draft: CodeSetDraft) -> List[Dict[str, Any]]:
    issues = []
    seen = set()
    for item in draft.values:
        if item.value in seen:
            issues.append({"severity": "WARNING", "code": "DUPLICATE_CODE_VALUE", "value": item.value})
        seen.add(item.value)
        if not item.label_cn.strip():
            issues.append({"severity": "ERROR", "code": "EMPTY_LABEL", "value": item.value})
    if not draft.values:
        issues.append({"severity": "WARNING", "code": "EMPTY_CODE_SET", "value": ""})
    return issues



# 「本项固定填“中国”」「个人主体固定取值“N/A”」——这是固定值约束，不是码表引用。
# AI 抽取时把这类表述填进了 code_reference_text，于是一路变成"码表引用解析不到"，
# 在码值页堆出一堆要补的占位码表。用户点进去只看到一张空表，完全不知道要改什么。
FIXED_VALUE_HINT_RE = re.compile(r"(?:本项)?(?:固定|恒)\s*(?:填|填报|填写|取\s*值|取|为|值)?\s*[:：]?\s*")
_QUOTES = "\"'“”‘’「」『』《》【】[]()（） "


def _has_error_issues(issues: List[Dict[str, Any]]) -> bool:
    """只有 ERROR 才算需要人处理。WARNING（比如 AI 复核不可用）只是提示。"""
    return any(str(item.get("severity") or "").upper() == "ERROR" for item in issues)


# 「流水码编制规则」「外债编号统一编号规则」「银行资本项目报送参考规则」
# 以及 TERM_INFERENCE 的正则——这些是格式约束，不是码表引用。
# AI 抽取时把它们也填进了 code_reference_text，一路变成"码表引用解析不到"。
_RULE_NOT_CODE_SET_RE = re.compile(
    r"(编制规则|编号规则|参考规则|报送规则|填列规则)|TERM_INFERENCE|^\s*[\^\[]"
    # 「无对应号码…填写"N/A"」「如为个人主体，填写"个人"」——条件性固定值，
    # 和 E0901 同类，也不是码表引用
    r"|(?:无对应|如为|若|不涉及)[^。；]{0,30}(?:填写|填)\s*[“\"'「]"
)


def is_rule_reference(text: str) -> bool:
    """这段「引用」其实是格式规则，不是码表。应从 code_reference_text 里清掉，
    规则本身留在 check_text 里，阶段 2 会按 REGEXP 处理。"""
    if not text:
        return False
    return bool(_RULE_NOT_CODE_SET_RE.search(text))


def parse_fixed_value(text: str) -> Optional[str]:
    """从引用原文里提取固定值。

    「本项固定填“中国”」→ 中国；「0 或 1」这种多值、或者根本不是固定值表述的，返回 None。
    """
    if not text:
        return None
    match = FIXED_VALUE_HINT_RE.search(text)
    if not match:
        return None
    rest = text[match.end():].strip()
    value = rest.split("，")[0].split("。")[0].strip(_QUOTES).strip()
    value = re.split(r"[，。；;、\s]", value, maxsplit=1)[0].strip(_QUOTES)
    if not value or len(value) > 30:
        return None
    # 「0 或 1」是两个取值，不是固定值
    if "或" in rest[: len(value) + 4]:
        return None
    return value


def is_conditional_phrase(text: str) -> bool:
    """这句话是不是只在某个前提下成立。

    「如为个人主体，则此项填写 N/A」是条件取值——非个人主体时要填真实名称，
    卡成无条件 ENUM 会把正常数据全判违规。判断依据必须看校验原文，
    不能只看「固定取值」那句引用摘要。
    """
    if not text:
        return False
    return bool(re.search(r"(如|若|当|如果)[^。；]{0,40}(则|时|的|，)", text))

