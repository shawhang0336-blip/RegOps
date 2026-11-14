#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外管局 1.4 版《金融机构外汇业务数据采集规范》（汇发〔2025〕38号）
第六章 6.1 单位基本情况表 —— 仿 limenagent/prodagent 架构完整离线可运行示例

核心架构特性（对标李号双 limenagent/prodagent）：
1. 机制在内，策略在外：内核仅包含 EventLog、BlobStore、Bus、Channel、Scheduler、Interrupt 六大部件。
2. 确定性工具 + AI 参数赋值：AI 绝不手写 SQL 或直接造数据，仅调用算子工具填入受白名单约束的槽位参数。
3. Bus 总线门禁裁决：当 AI 产生字段名幻觉（如编造 ORG_CODE_ERR）时，Bus 在工具执行前物理拦截并打回。
4. wait_human 人工确权中断：对 AI 从“数据术语解释”推理出的拓展规则（AI_EXPANDED）挂起落盘，确权后从账本原样恢复。
5. 拓扑母本造数与单点靶向变异（One-Rule-One-Break）：基于 SQLite 内存沙箱验证黄金母本 0 误报、变异坏数据 100% 精准拦截。
6. go() 沙箱自愈回边：第一轮 AI 漏配操作类型 'D' 导致靶向测试漏拦，沙箱自动通过 go("bind_table_rules", ...) 打回自愈。
7. ScriptedLlm 零成本离线剧本：无需 API Key，100% 离线确定性重放完整流程与外管 <ERRRECORDS> XML 反馈报文生成。

运行方式：
    python3 safe_6_1_prodagent_example.py
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Set, Tuple


# ============================================================================
# 第一部分：prodagent 极简微内核（EventLog / BlobStore / Bus / Channel / Workflow）
# 说明：如已安装 limenagent/prodagent，可将此处替换为 `from src import ...`
# ============================================================================

@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    node: str
    payload: Dict[str, Any]


class EventLog:
    """一本账：只追加的不可变事件流，所有状态均由事件折叠（Fold）产生"""

    def __init__(self) -> None:
        self._events: List[Event] = []

    def append(self, kind: str, node: str, payload: Dict[str, Any]) -> Event:
        ev = Event(seq=len(self._events) + 1, kind=kind, node=node, payload=payload)
        self._events.append(ev)
        return ev

    @property
    def events(self) -> List[Event]:
        return list(self._events)


class BlobStore:
    """大对象存储：存放 DDL、SQL、大批量合成数据与 XML 报文，EventLog 中仅存指针"""

    def __init__(self) -> None:
        self._blobs: Dict[str, Any] = {}

    def put(self, tag: str, content: Any) -> str:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True) if not isinstance(content, str) else content
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        uri = f"blob://{tag}/{digest}"
        self._blobs[uri] = content
        return uri

    def get(self, uri: str) -> Any:
        return self._blobs[uri]


class Bus:
    """总线：每走一步对外广播，支持挂载旁观（Observer）与前置裁决拦截器（Adjudicator）"""

    def __init__(self) -> None:
        self._tool_adjudicators: List[Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]] = []

    def add_tool_adjudicator(
        self, fn: Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]
    ) -> None:
        self._tool_adjudicators.append(fn)

    def adjudicate_tool_call(
        self, tool_name: str, args: Dict[str, Any], state: Dict[str, Any]
    ) -> Optional[str]:
        for adj in self._tool_adjudicators:
            err = adj(tool_name, args, state)
            if err:
                return err
        return None


# 控制流原语：go（跳转/回边）、send（动态并发扇出）、InterruptSignal（中断挂起）
@dataclass
class GoDirective:
    target: str
    value: Any


@dataclass
class SendDirective:
    target: str
    value: Any


class InterruptSignal(Exception):
    def __init__(self, question: str, resume_node: str, pending_input: Any) -> None:
        super().__init__(question)
        self.question = question
        self.resume_node = resume_node
        self.pending_input = pending_input


def go(target: str, value: Any = None) -> GoDirective:
    return GoDirective(target=target, value=value)


def send(target: str, value: Any) -> SendDirective:
    return SendDirective(target=target, value=value)


async def wait_human(question: str, ctx: "RunContext") -> Any:
    """若账本中已有人工回复事件则直接返回，否则抛出中断信号落盘挂起"""
    if ctx.state.get("_human_reply") is not None:
        return ctx.state.pop("_human_reply")
    raise InterruptSignal(question=question, resume_node=ctx.current_node, pending_input=ctx.current_input)


@dataclass
class RunContext:
    state: Dict[str, Any]
    ledger: EventLog
    blobs: BlobStore
    bus: Bus
    current_node: str = ""
    current_input: Any = None


@dataclass
class ToolCallStep:
    tool_name: str
    args: Dict[str, Any]
    note: str = ""


class ScriptedLlm:
    """
    照剧本演戏的离线确定性模型（对标 prodagent 的 ScriptedLlm）：
    无需 API Key，按预设波次输出 Tool Calls，专门用于离线验证工具赋值、Bus 拦截与沙箱回边自愈。
    """

    def __init__(self, rounds: List[List[ToolCallStep]]) -> None:
        self._rounds = rounds
        self._cursor = 0

    def next_turn(self, prompt_input: Any, feedback_history: List[str]) -> List[ToolCallStep]:
        if self._cursor >= len(self._rounds):
            return []
        steps = self._rounds[self._cursor]
        self._cursor += 1
        return steps


class Agent:
    """受控 Agent 节点：通过模型选择工具并填入参数，所有工具调用均经过 Bus 门禁裁决"""

    def __init__(
        self,
        name: str,
        model: ScriptedLlm,
        instruction: str,
        tools: List[Callable[..., Dict[str, Any]]],
    ) -> None:
        self.name = name
        self.model = model
        self.instruction = instruction
        self.tool_map = {fn.__name__: fn for fn in tools}

    async def __call__(self, node_input: Any, ctx: RunContext) -> Any:
        feedback_log: List[str] = []
        while True:
            planned_calls = self.model.next_turn(node_input, feedback_log)
            if not planned_calls:
                break

            all_succeeded = True
            for call in planned_calls:
                # 1. 先过 Bus 总线白名单裁决器
                violation = ctx.bus.adjudicate_tool_call(call.tool_name, call.args, ctx.state)
                if violation is not None:
                    ctx.ledger.append(
                        "BUS_REJECTED",
                        self.name,
                        {"tool": call.tool_name, "args": call.args, "reason": violation},
                    )
                    print(f"  [Bus 门禁拦截] 拒绝工具调用 {call.tool_name}: {violation}")
                    feedback_log.append(violation)
                    all_succeeded = False
                    continue

                # 2. 执行确定性工具赋值
                tool_fn = self.tool_map[call.tool_name]
                result = tool_fn(ctx=ctx, **call.args)
                ctx.ledger.append(
                    "TOOL_ASSIGNED",
                    self.name,
                    {"tool": call.tool_name, "args": call.args, "result": result},
                )
                print(f"  [AI 工具赋值成功] {call.tool_name} -> 规则 {result['rule_id']} ({result['rule_source']})")

            if all_succeeded:
                break

        return ctx.state.get("bound_rules", {})


class Workflow:
    """确定性流程图与 BSP 调度器内核"""

    def __init__(self) -> None:
        self.nodes: Dict[str, Callable[[Any, RunContext], Any]] = {}
        self.edges: Dict[str, str] = {}
        self.terminals: Set[str] = set()
        self.entry_node: str = ""

    def add_node(self, name: str, fn: Callable[[Any, RunContext], Any], terminal: bool = False) -> None:
        self.nodes[name] = fn
        if terminal:
            self.terminals.add(name)

    def add_edge(self, src: str, dst: str) -> None:
        self.edges[src] = dst

    def entry(self, name: str) -> None:
        self.entry_node = name

    async def run(
        self,
        initial_input: Any,
        ctx: RunContext,
        start_node: Optional[str] = None,
    ) -> Tuple[str, Any]:
        curr = start_node or self.entry_node
        val = initial_input

        while curr:
            ctx.current_node = curr
            ctx.current_input = val
            ctx.ledger.append("NODE_ENTER", curr, {"input_summary": str(val)[:120]})
            node_fn = self.nodes[curr]

            try:
                out = await node_fn(val, ctx)
            except InterruptSignal as intr:
                ctx.ledger.append(
                    "WORKFLOW_INTERRUPTED",
                    curr,
                    {"question": intr.question, "resume_node": intr.resume_node},
                )
                return ("INTERRUPTED", intr)

            # 处理动态扇出 send()（BSP 波次并发）
            if isinstance(out, list) and out and isinstance(out[0], SendDirective):
                target_node = out[0].target
                ctx.ledger.append(
                    "BSP_FANOUT",
                    curr,
                    {"target": target_node, "wave_size": len(out)},
                )
                tasks = [self.nodes[d.target](d.value, ctx) for d in out]
                wave_results = await asyncio.gather(*tasks)
                ctx.ledger.append("BSP_BARRIER_JOIN", target_node, {"merged_count": len(wave_results)})
                curr = self.edges.get(target_node, "")
                val = wave_results[-1] if wave_results else None
                continue

            # 处理运行时跳转与回边 go()
            if isinstance(out, GoDirective):
                ctx.ledger.append("GO_REDIRECT", curr, {"to": out.target, "reason": str(out.value)[:160]})
                curr = out.target
                val = out.value
                continue

            ctx.ledger.append("NODE_EXIT", curr, {"status": "OK"})
            if curr in self.terminals:
                return ("COMPLETED", out)
            curr = self.edges.get(curr, "")
            val = out

        return ("COMPLETED", val)


# ============================================================================
# 第二部分：外管 1.4 版『6.1 单位基本情况表』确定性解析器与算子工具箱
# ============================================================================

def extract_safe_6_1_spec_whitelist() -> Dict[str, Any]:
    """
    模拟从《金融机构外汇业务数据采集规范（1.4版）》第六章 6.1 节确定性提取的结构化字典与术语。
    注意：此函数 100% 由确定性文档解析器产出，构成不可篡改的字段白名单。
    """
    return {
        "table_code": "SAFE_BOP_6_1_UNIT",
        "table_cn_name": "6.1 单位基本情况表",
        "xml_root_path": "/MSG/RECORDS/REC",
        "official_dicts": {
            "ACTIONTYPE_DICT": ["A", "C", "D"],
            "COUNTRY_DICT": ["CHN", "HKG", "USA", "JPN", "SGP", "DEU"],
            "ECONOMY_DICT": ["110", "159", "210", "230", "310", "320"],
            "INDUSTRY_DICT": ["C391", "I651", "J662", "F511", "L721"],
            "YES_NO_DICT": ["Y", "N"],
            "SPECIAL_ECO_DICT": ["01", "02", "03", "04", "99"],
        },
        "columns": {
            "BUSSNO": {"cn": "业务数据主键", "type": "VARCHAR(32)", "pk": True, "raw_check": "必填项，唯一主键"},
            "ACTIONTYPE": {"cn": "操作类型", "type": "CHAR(1)", "pk": False, "raw_check": "必填项，A-新建，C-修改，D-删除"},
            "ACTIONDESC": {"cn": "修改/删除原因", "type": "VARCHAR(128)", "pk": False, "raw_check": "当操作类型为C或D时必填；为A时必须为空"},
            "CUSTCODE": {"cn": "组织机构代码", "type": "VARCHAR(18)", "pk": False, "raw_check": "必填项，18位统一社会信用代码或9位特殊机构代码"},
            "CUSTNAME": {"cn": "机构中文名称", "type": "VARCHAR(128)", "pk": False, "raw_check": "必填项，不少于4个字符"},
            "COUNTRYCODE": {"cn": "常驻国家/地区代码", "type": "CHAR(3)", "pk": False, "raw_check": "必填项，必须在国别代码表中"},
            "ECONOMYTYPE": {"cn": "经济类型代码", "type": "CHAR(3)", "pk": False, "raw_check": "必填项，必须在经济类型代码表中"},
            "INVCOUNTRYCODE": {"cn": "外方投资者国别", "type": "CHAR(3)", "pk": False, "raw_check": "外商投资及港澳台投资企业必填"},
            "ISSPECIALECOROOM": {"cn": "是否特殊经济区内企业", "type": "CHAR(1)", "pk": False, "raw_check": "必填项，Y-是，N-否"},
            "SPECIALECOCODE": {"cn": "特殊经济区企业类型", "type": "CHAR(2)", "pk": False, "raw_check": "当是否特殊经济区内企业为Y时必填"},
        },
        "term_explanations": [
            "【术语解释 6.1.1-组织机构代码】：统一社会信用代码为18位，第18位须符合GB 32100-2015的MOD-31加权校验位规则。",
            "【术语解释 6.1.1-经济类型与投资国别】：经济类型代码以2（港澳台投资，如210/230）或3（外商投资，如310/320）开头时，外方投资者国别（INVCOUNTRYCODE）不能为空且不能为CHN；当经济类型为110（国有内资）时，INVCOUNTRYCODE必须为空。",
            "【术语解释 6.1.1-特殊经济区】：当ISSPECIALECOROOM为N时，特殊经济区企业类型（SPECIALECOCODE）必须为空，不得填报码值。",
        ],
    }


# --- GB 32100-2015 18位统一社会信用代码确定性算法（供 SQLite UDF 与造数器共用） ---
USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def compute_uscc_check_char(prefix17: str) -> str:
    total = sum(USCC_CHARS.index(ch) * w for ch, w in zip(prefix17, USCC_WEIGHTS))
    idx = (31 - (total % 31)) % 31
    return USCC_CHARS[idx]


def is_valid_uscc_18(code: Optional[str]) -> int:
    if not code or len(code) != 18:
        return 0
    if any(ch not in USCC_CHARS for ch in code):
        return 0
    return 1 if compute_uscc_check_char(code[:17]) == code[17] else 0


# --- Bus 总线白名单裁决钩子（物理阻断 AI 字段幻觉） ---
def bus_field_whitelist_gatekeeper(
    tool_name: str, args: Dict[str, Any], state: Dict[str, Any]
) -> Optional[str]:
    whitelist: Set[str] = set(state["table_spec"]["columns"].keys())
    checked_keys = ["field", "if_field", "then_field", "lhs_field", "rhs_field"]
    for key in checked_keys:
        if key in args and args[key] is not None:
            f_name = args[key]
            if f_name not in whitelist:
                return (
                    f"字段幻觉拦截：参数 `{key}='{f_name}'` 不在 6.1 表数据字典白名单 "
                    f"{sorted(whitelist)} 中！"
                )
    return None


# --- 暴露给 AI 赋值的确定性算子工具箱（AI 只能填槽位，工具负责生成 SQL 与变异器） ---

def _register_rule(ctx: RunContext, rule_obj: Dict[str, Any]) -> Dict[str, Any]:
    rules = ctx.state.setdefault("bound_rules", {})
    rules[rule_obj["rule_id"]] = rule_obj
    return {"rule_id": rule_obj["rule_id"], "rule_source": rule_obj["rule_source"], "status": "BOUND"}


def bind_enum_dict_operator(
    ctx: RunContext,
    rule_id: str,
    rule_source: Literal["DOC_EXPLICIT", "AI_EXPANDED"],
    field: str,
    dict_name: str,
    rationale: str,
) -> Dict[str, Any]:
    """算子1：枚举与外管代码表算子"""
    allowed = ctx.state["table_spec"]["official_dicts"][dict_name]
    vals_sql = ", ".join(f"'{v}'" for v in allowed)
    allowed_label = "/".join(allowed)
    sql_where = f"({field} IS NULL OR {field} NOT IN ({vals_sql}))"
    return _register_rule(
        ctx,
        {
            "rule_id": rule_id,
            "operator": "bind_enum_dict_operator",
            "rule_source": rule_source,
            "target_field": field,
            "sql_where": sql_where,
            "error_desc": f"{field} 取值不在代码表 {dict_name} 允许范围({allowed_label})内",
            "rationale": rationale,
            "mutation_recipe": {"op": "SET_INVALID_ENUM", "field": field, "bad_value": "Z"},
        },
    )


def bind_cond_depend_operator(
    ctx: RunContext,
    rule_id: str,
    rule_source: Literal["DOC_EXPLICIT", "AI_EXPANDED"],
    if_field: str,
    if_values: List[str],
    then_field: str,
    requirement: Literal["REQUIRED", "MUST_BE_EMPTY", "NOT_EQUAL_CHN"],
    rationale: str,
) -> Dict[str, Any]:
    """算子2：条件依赖算子（覆盖跨字段条件必填、条件禁填、条件排他）"""
    if_sql = ", ".join(f"'{v}'" for v in if_values)
    if_label = "/".join(if_values)
    if requirement == "REQUIRED":
        then_sql = f"({then_field} IS NULL OR TRIM({then_field}) = '')"
        mutation = {
            "op": "COND_BREAK",
            "overrides": {if_field: if_values[0], then_field: ""},
        }
    elif requirement == "MUST_BE_EMPTY":
        then_sql = f"({then_field} IS NOT NULL AND TRIM({then_field}) <> '')"
        mutation = {
            "op": "COND_BREAK",
            "overrides": {if_field: if_values[0], then_field: "99"},
        }
    else:  # NOT_EQUAL_CHN
        then_sql = f"({then_field} IS NULL OR TRIM({then_field}) = '' OR {then_field} = 'CHN')"
        mutation = {
            "op": "COND_BREAK",
            "overrides": {if_field: if_values[0], then_field: "CHN"},
        }

    sql_where = f"({if_field} IN ({if_sql}) AND {then_sql})"
    return _register_rule(
        ctx,
        {
            "rule_id": rule_id,
            "operator": "bind_cond_depend_operator",
            "rule_source": rule_source,
            "target_field": then_field,
            "sql_where": sql_where,
            "error_desc": f"当 {if_field} 取值为 {if_label} 时，{then_field} 违反约束 {requirement}",
            "rationale": rationale,
            "mutation_recipe": mutation,
        },
    )


def bind_checksum_algo_operator(
    ctx: RunContext,
    rule_id: str,
    rule_source: Literal["DOC_EXPLICIT", "AI_EXPANDED"],
    field: str,
    algo_name: Literal["USCC_MOD31_18"],
    rationale: str,
) -> Dict[str, Any]:
    """算子3：行业编码校验位算法算子（如 18 位统一社会信用代码 MOD-31 校验）"""
    sql_where = f"(VALID_USCC18({field}) = 0)"
    return _register_rule(
        ctx,
        {
            "rule_id": rule_id,
            "operator": "bind_checksum_algo_operator",
            "rule_source": rule_source,
            "target_field": field,
            "sql_where": sql_where,
            "error_desc": f"{field} 未通过 {algo_name} 编码结构与第18位校验码核验",
            "rationale": rationale,
            "mutation_recipe": {
                "op": "CORRUPT_USCC_CHECKDIGIT",
                "field": field,
                "bad_value": "911100001000010430",  # 正确末位应为 K，故意篡改为 0
            },
        },
    )


# ============================================================================
# 第三部分：确定性编译器、黄金母本生成器、靶向变异器与 SQLite 沙箱矩阵对账
# ============================================================================

def compile_ddl_and_validation_sql(
    table_spec: Dict[str, Any], rules: Dict[str, Dict[str, Any]], approved_ids: Set[str]
) -> Tuple[str, Dict[str, str]]:
    """确定性编译器：根据白名单字典生成 CREATE TABLE DDL 与每条规则的查询 SQL"""
    t_name = table_spec["table_code"]
    col_defs = []
    for col_en, meta in table_spec["columns"].items():
        pk_clause = " PRIMARY KEY" if meta["pk"] else ""
        col_defs.append(f"    {col_en} {meta['type']}{pk_clause}")
    ddl = f"CREATE TABLE IF NOT EXISTS {t_name} (\n" + ",\n".join(col_defs) + "\n);"

    compiled_sqls: Dict[str, str] = {}
    for r_id, r_meta in rules.items():
        if r_id not in approved_ids:
            continue
        safe_desc = r_meta["error_desc"].replace("'", "''")
        compiled_sqls[r_id] = (
            f"SELECT BUSSNO, '{r_id}' AS ERR_RULE, '{r_meta['target_field']}' AS ERR_FIELD, "
            f"'{safe_desc}' AS ERR_DESC "
            f"FROM {t_name} WHERE {r_meta['sql_where']};"
        )
    return ddl, compiled_sqls


def generate_golden_and_mutated_dataset(
    table_spec: Dict[str, Any], rules: Dict[str, Dict[str, Any]], approved_ids: Set[str]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    确定性造数与单点靶向变异引擎（One-Rule-One-Break）：
    1. 先用合规生成器生成覆盖 A/C 不同业务场景的 100% 合规黄金母本（Golden Records）。
    2. 再针对每一条启用的校验规则，克隆黄金母本并仅定向破坏目标字段。
    """
    valid_uscc_1 = "91110000100001043" + compute_uscc_check_char("91110000100001043")
    valid_uscc_2 = "91310000607216535" + compute_uscc_check_char("91310000607216535")

    golden_records = [
        {
            "BUSSNO": "GOLDEN_A_001",
            "ACTIONTYPE": "A",
            "ACTIONDESC": "",
            "CUSTCODE": valid_uscc_1,
            "CUSTNAME": "中航国际跨境贸易有限公司",
            "COUNTRYCODE": "CHN",
            "ECONOMYTYPE": "110",
            "INVCOUNTRYCODE": "",
            "ISSPECIALECOROOM": "N",
            "SPECIALECOCODE": "",
            "expected_rule_id": None,
        },
        {
            "BUSSNO": "GOLDEN_C_002",
            "ACTIONTYPE": "C",
            "ACTIONDESC": "变更外方投资者国别及特殊经济区属性",
            "CUSTCODE": valid_uscc_2,
            "CUSTNAME": "上海临港精密智造外资有限公司",
            "COUNTRYCODE": "CHN",
            "ECONOMYTYPE": "310",
            "INVCOUNTRYCODE": "DEU",
            "ISSPECIALECOROOM": "Y",
            "SPECIALECOCODE": "01",
            "expected_rule_id": None,
        },
    ]

    mutated_records: List[Dict[str, Any]] = []
    base = golden_records[0]

    for r_id in sorted(approved_ids):
        r_meta = rules[r_id]
        recipe = r_meta["mutation_recipe"]
        clone = copy.deepcopy(base)
        clone["BUSSNO"] = f"MUT_{r_id}"
        clone["expected_rule_id"] = r_id

        if recipe["op"] in ("SET_INVALID_ENUM", "CORRUPT_USCC_CHECKDIGIT"):
            clone[recipe["field"]] = recipe["bad_value"]
        elif recipe["op"] == "COND_BREAK":
            for k, v in recipe["overrides"].items():
                clone[k] = v
            if clone["ACTIONTYPE"] in ("C", "D") and r_id != "R_6_1_002":
                clone["ACTIONDESC"] = "合规修改原因说明"
            if clone["ISSPECIALECOROOM"] == "Y" and r_id != "R_6_1_004":
                clone["SPECIALECOCODE"] = "01"
        mutated_records.append(clone)

    # 额外加入一条针对第六章规范明文规定“操作类型为 D（删除）时修改/删除原因也必填”的边界探针样本
    probe_d = copy.deepcopy(base)
    probe_d["BUSSNO"] = "MUT_R_6_1_002_BRANCH_D"
    probe_d["ACTIONTYPE"] = "D"
    probe_d["ACTIONDESC"] = ""  # 故意置空删除原因，测试 R_6_1_002 是否完整覆盖了 'D' 分支
    probe_d["expected_rule_id"] = "R_6_1_002"
    mutated_records.append(probe_d)

    return {"golden": golden_records, "mutated": mutated_records}


def execute_sqlite_sandbox_audit(
    table_spec: Dict[str, Any],
    ddl: str,
    compiled_sqls: Dict[str, str],
    dataset: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """在内存 SQLite 沙箱中建表、灌数、执行全部检核 SQL，并生成外管 <ERRRECORDS> 报文与对账诊断"""
    conn = sqlite3.connect(":memory:")
    conn.create_function("VALID_USCC18", 1, is_valid_uscc_18)
    cur = conn.cursor()
    cur.execute(ddl)

    cols = list(table_spec["columns"].keys())
    placeholders = ", ".join(["?"] * len(cols))
    insert_sql = f"INSERT INTO {table_spec['table_code']} ({', '.join(cols)}) VALUES ({placeholders})"

    all_rows = dataset["golden"] + dataset["mutated"]
    for row in all_rows:
        cur.execute(insert_sql, [row[c] for c in cols])
    conn.commit()

    hits_by_bussno: Dict[str, List[Tuple[str, str, str]]] = {r["BUSSNO"]: [] for r in all_rows}
    for r_id, sql in compiled_sqls.items():
        for bussno, err_rule, err_field, err_desc in cur.execute(sql).fetchall():
            hits_by_bussno[bussno].append((err_rule, err_field, err_desc))

    conn.close()

    # 1. 审计黄金合规样本（必须 0 误报）
    false_positives = []
    for g in dataset["golden"]:
        triggered = [h[0] for h in hits_by_bussno[g["BUSSNO"]]]
        if triggered:
            false_positives.append({"bussno": g["BUSSNO"], "unexpected_rules": triggered})

    # 2. 审计单点靶向变异样本（必须精准命中 expected_rule_id）
    false_negatives = []
    exact_matches = []
    for m in dataset["mutated"]:
        bussno = m["BUSSNO"]
        expected = m["expected_rule_id"]
        triggered = [h[0] for h in hits_by_bussno[bussno]]
        if expected not in triggered:
            false_negatives.append(
                {
                    "bussno": bussno,
                    "expected_rule_id": expected,
                    "actual_triggered": triggered,
                    "diagnostic_hint": (
                        f"样本 {bussno} (ACTIONTYPE='{m['ACTIONTYPE']}', ACTIONDESC='{m['ACTIONDESC']}') "
                        f"预期触发规则 {expected} 但未被拦截！请检查 {expected} 的触发值列表是否遗漏了 '{m['ACTIONTYPE']}'。"
                    ),
                }
            )
        else:
            exact_matches.append({"bussno": bussno, "matched_rule": expected})

    # 3. 按照外管第五章/第六章格式生成 <ERRRECORDS> 模拟反馈 XML 报文
    xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<MSG>",
        "  <APPTYPE>BOP</APPTYPE>",
        "  <CURRENTFILE>BOP_UNIT_6_1</CURRENTFILE>",
        f"  <TOTALRECORDS>{len(all_rows)}</TOTALRECORDS>",
        f"  <SUCRECORDS>{sum(1 for v in hits_by_bussno.values() if not v)}</SUCRECORDS>",
        f"  <FALRECORDS>{sum(1 for v in hits_by_bussno.values() if v)}</FALRECORDS>",
        "  <ERRRECORDS>",
    ]
    for bussno, errs in hits_by_bussno.items():
        if not errs:
            continue
        xml_lines.append("    <REC>")
        xml_lines.append(f"      <BUSSNO>{bussno}</BUSSNO>")
        xml_lines.append("      <ERRFIELDS>")
        for err_rule, err_field, err_desc in errs:
            col_cn = table_spec["columns"][err_field]["cn"]
            xml_lines.append("        <ERR>")
            xml_lines.append(f"          <ERRFIELD>{err_field}</ERRFIELD>")
            xml_lines.append(f"          <ERRFIELDCN>{col_cn}</ERRFIELDCN>")
            xml_lines.append(f"          <ERRDESC>[{err_rule}] {err_desc}</ERRDESC>")
            xml_lines.append("        </ERR>")
        xml_lines.append("      </ERRFIELDS>")
        xml_lines.append("    </REC>")
    xml_lines.append("  </ERRRECORDS>")
    xml_lines.append("</MSG>")

    all_passed = (len(false_positives) == 0) and (len(false_negatives) == 0)
    return {
        "all_passed": all_passed,
        "golden_total": len(dataset["golden"]),
        "mutated_total": len(dataset["mutated"]),
        "exact_matches_count": len(exact_matches),
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "feedback_xml": "\n".join(xml_lines),
    }


# ============================================================================
# 第四部分：组装 prodagent 风格 Workflow 节点与 ScriptedLlm 剧本
# ============================================================================

def build_scripted_llm_for_6_1() -> ScriptedLlm:
    """
    构造三幕完整离线剧本：
    - 第 1 幕（初次赋值）：AI 故意犯一个字段名幻觉（填了 ORG_CODE_ERR），被 Bus 当场拦截；
      同时在给 R_6_1_002 赋值时故意漏掉了 'D'（只填了 if_values=['C']），留给沙箱测试去发现。
    - 第 2 幕（同轮修正 Bus 拦截）：AI 收到 Bus 的白名单报错后，立即把 ORG_CODE_ERR 修正为 CUSTCODE。
    - 第 3 幕（沙箱 go 回边自愈）：沙箱发现 MUT_R_6_1_002_BRANCH_D 漏检并调用 go("bind_table_rules") 回边后，
      AI 将 R_6_1_002 的 if_values 修正为 ['C', 'D']。
    """
    round_1_initial_binding = [
        ToolCallStep(
            tool_name="bind_enum_dict_operator",
            args={
                "rule_id": "R_6_1_001",
                "rule_source": "DOC_EXPLICIT",
                "field": "ACTIONTYPE",
                "dict_name": "ACTIONTYPE_DICT",
                "rationale": "6.1.3 数据字典：ACTIONTYPE 必填项，A-新建，C-修改，D-删除",
            },
        ),
        ToolCallStep(
            tool_name="bind_cond_depend_operator",
            args={
                "rule_id": "R_6_1_002",
                "rule_source": "DOC_EXPLICIT",
                "if_field": "ACTIONTYPE",
                "if_values": ["C"],  # <-- 缺陷：漏了 "D"，将在沙箱阶段被靶向探针捕获并触发 go() 回边
                "then_field": "ACTIONDESC",
                "requirement": "REQUIRED",
                "rationale": "6.1.3 数据字典：修改/删除原因在操作类型为C或D时必填",
            },
        ),
        ToolCallStep(
            tool_name="bind_checksum_algo_operator",
            args={
                "rule_id": "R_6_1_003",
                "rule_source": "AI_EXPANDED",
                "field": "ORG_CODE_ERR",  # <-- 幻觉字段名！将被 Bus 拦截
                "algo_name": "USCC_MOD31_18",
                "rationale": "6.1.1 数据术语解释：18位统一社会信用代码需满足GB 32100-2015校验位",
            },
        ),
        ToolCallStep(
            tool_name="bind_cond_depend_operator",
            args={
                "rule_id": "R_6_1_004",
                "rule_source": "DOC_EXPLICIT",
                "if_field": "ISSPECIALECOROOM",
                "if_values": ["Y"],
                "then_field": "SPECIALECOCODE",
                "requirement": "REQUIRED",
                "rationale": "6.1.3 数据字典：当是否特殊经济区内企业为Y时必填",
            },
        ),
        ToolCallStep(
            tool_name="bind_cond_depend_operator",
            args={
                "rule_id": "R_6_1_005",
                "rule_source": "AI_EXPANDED",
                "if_field": "ISSPECIALECOROOM",
                "if_values": ["N"],
                "then_field": "SPECIALECOCODE",
                "requirement": "MUST_BE_EMPTY",
                "rationale": "6.1.1 术语解释拓展：非特殊经济区内企业不得填报特殊经济区企业类型码值",
            },
        ),
        ToolCallStep(
            tool_name="bind_cond_depend_operator",
            args={
                "rule_id": "R_6_1_006",
                "rule_source": "AI_EXPANDED",
                "if_field": "ECONOMYTYPE",
                "if_values": ["210", "230", "310", "320"],
                "then_field": "INVCOUNTRYCODE",
                "requirement": "NOT_EQUAL_CHN",
                "rationale": "6.1.1 术语解释拓展：外商及港澳台投资企业的外方投资者国别不能为空且不得为中国境内(CHN)",
            },
        ),
    ]

    round_2_fix_bus_hallucination = [
        ToolCallStep(
            tool_name="bind_checksum_algo_operator",
            args={
                "rule_id": "R_6_1_003",
                "rule_source": "AI_EXPANDED",
                "field": "CUSTCODE",
                "algo_name": "USCC_MOD31_18",
                "rationale": "修正字段名为白名单中的 CUSTCODE，绑定 GB 32100-2015 18位校验位算法",
            },
        )
    ]

    round_3_self_heal_from_sandbox = [
        ToolCallStep(
            tool_name="bind_cond_depend_operator",
            args={
                "rule_id": "R_6_1_002",
                "rule_source": "DOC_EXPLICIT",
                "if_field": "ACTIONTYPE",
                "if_values": ["C", "D"],  # <-- 自愈修复：补齐删除操作 'D'
                "then_field": "ACTIONDESC",
                "requirement": "REQUIRED",
                "rationale": "根据沙箱回边诊断修正：当操作类型为 C 或 D 时，修改/删除原因均必填",
            },
        )
    ]

    return ScriptedLlm([round_1_initial_binding, round_2_fix_bus_hallucination, round_3_self_heal_from_sandbox])


# --- 定义 Workflow 上的各个节点 ---

async def parse_spec_node(doc_name: str, ctx: RunContext) -> Any:
    print(f"\n[Node 1: parse_spec] 确定性提取 `{doc_name}` 字段白名单与术语解释...")
    spec = extract_safe_6_1_spec_whitelist()
    ctx.state["table_spec"] = spec
    print(f"  -> 提取表 `{spec['table_code']}` ({spec['table_cn_name']})，锁定 {len(spec['columns'])} 个白名单字段。")
    return [send("bind_table_rules", spec)]


async def confirm_expanded_node(bound_rules: Dict[str, Dict[str, Any]], ctx: RunContext) -> Any:
    expanded_ids = [r_id for r_id, r in bound_rules.items() if r["rule_source"] == "AI_EXPANDED"]
    explicit_ids = [r_id for r_id, r in bound_rules.items() if r["rule_source"] == "DOC_EXPLICIT"]

    if expanded_ids and not ctx.state.get("expanded_approved"):
        print(f"\n[Node 3: confirm_expanded] 检测到 {len(expanded_ids)} 条 AI 拓展规则，调用 wait_human() 挂起等待人工确权...")
        question = (
            f"请确认是否启用以下 {len(expanded_ids)} 条基于『6.1.1 数据术语解释』拓展的校验规则: "
            f"{expanded_ids}（显性规则 {explicit_ids} 已默认启用）"
        )
        reply = await wait_human(question, ctx)
        ctx.state["approved_rule_ids"] = set(explicit_ids) | set(reply["approved_expanded_ids"])
        ctx.state["expanded_approved"] = True
        print(f"  -> [恢复执行] 收到人工确权事件，最终启用规则集: {sorted(ctx.state['approved_rule_ids'])}")

    return ctx.state["approved_rule_ids"]


async def build_and_audit_node(approved_ids: Set[str], ctx: RunContext) -> Any:
    attempt = ctx.state.get("audit_attempt", 0) + 1
    ctx.state["audit_attempt"] = attempt
    print(f"\n[Node 4: build_and_audit] 第 {attempt} 轮确定性建表、靶向变异造数与 SQLite 沙箱对账...")

    spec = ctx.state["table_spec"]
    rules = ctx.state["bound_rules"]

    ddl, compiled_sqls = compile_ddl_and_validation_sql(spec, rules, approved_ids)
    ddl_uri = ctx.blobs.put("ddl_sql", {"ddl": ddl, "sqls": compiled_sqls})

    dataset = generate_golden_and_mutated_dataset(spec, rules, approved_ids)
    data_uri = ctx.blobs.put("synthetic_data", dataset)

    report = execute_sqlite_sandbox_audit(spec, ddl, compiled_sqls, dataset)
    xml_uri = ctx.blobs.put("safe_err_xml", report["feedback_xml"])

    print(
        f"  -> 沙箱对账结果: 黄金样本数={report['golden_total']} (误报={len(report['false_positives'])}), "
        f"靶向变异样本数={report['mutated_total']} (精准拦截={report['exact_matches_count']}, "
        f"漏检={len(report['false_negatives'])})"
    )

    if not report["all_passed"]:
        diag = report["false_negatives"][0]["diagnostic_hint"]
        print(f"  [沙箱发现漏检 -> 触发 go('bind_table_rules') 回边自愈] 诊断信息: {diag}")
        return go("bind_table_rules", {"sandbox_feedback": diag})

    print("  -> [100% 矩阵对账通过] 所有黄金样本 0 误报，所有靶向变异样本 100% 精准拦截！")
    return {
        "ddl_blob": ddl_uri,
        "data_blob": data_uri,
        "xml_blob": xml_uri,
        "summary": report,
    }


# ============================================================================
# 第五部分：主程序入口 —— 演示离线运行、人工审批断点续跑与 EventLog 因果账本
# ============================================================================

async def main() -> None:
    print("=" * 78)
    print("外管 1.4 版《6.1 单位基本情况表》— prodagent 架构（ScriptedLlm 离线全流程演示）")
    print("=" * 78)

    ledger = EventLog()
    blobs = BlobStore()
    bus = Bus()
    bus.add_tool_adjudicator(bus_field_whitelist_gatekeeper)
    ctx = RunContext(state={}, ledger=ledger, blobs=blobs, bus=bus)

    scripted_llm = build_scripted_llm_for_6_1()
    rule_binder_agent = Agent(
        name="bind_table_rules",
        model=scripted_llm,
        instruction="阅读 6.1 表数据字典与术语解释，调用算子工具完成参数赋值。",
        tools=[
            bind_enum_dict_operator,
            bind_cond_depend_operator,
            bind_checksum_algo_operator,
        ],
    )

    wf = Workflow()
    wf.add_node("parse_spec", parse_spec_node)
    wf.add_node("bind_table_rules", rule_binder_agent)
    wf.add_node("confirm_expanded", confirm_expanded_node)
    wf.add_node("build_and_audit", build_and_audit_node, terminal=True)

    wf.add_edge("parse_spec", "bind_table_rules")
    wf.add_edge("bind_table_rules", "confirm_expanded")
    wf.add_edge("confirm_expanded", "build_and_audit")
    wf.entry("parse_spec")

    status, payload = await wf.run("金融机构外汇业务数据采集规范(1.4版)_第六章_6.1.docx", ctx)

    if status == "INTERRUPTED":
        intr: InterruptSignal = payload
        print("\n" + "-" * 78)
        print(f"[Workflow 已挂起落盘] 等待人工输入: {intr.question}")
        human_decision = {"approved_expanded_ids": ["R_6_1_003", "R_6_1_005", "R_6_1_006"], "approver": "外汇合规岗_张工"}
        ledger.append("HUMAN_RESUMED", intr.resume_node, human_decision)
        ctx.state["_human_reply"] = human_decision
        print(f"[人工审批写入 EventLog] 批准人: {human_decision['approver']}，从断点 `{intr.resume_node}` 原样恢复...")
        print("-" * 78)

        status, final_bundle = await wf.run(intr.pending_input, ctx, start_node=intr.resume_node)

    ddl_obj = blobs.get(final_bundle["ddl_blob"])
    xml_text = blobs.get(final_bundle["xml_blob"])

    print("\n" + "=" * 78)
    print("【产物 1】确定性生成的报送表 DDL 与 6 条编译后检核 SQL")
    print("=" * 78)
    print(ddl_obj["ddl"])
    print("\n-- 编译生成的检核 SQL 清单：")
    for r_id, sql in sorted(ddl_obj["sqls"].items()):
        src_tag = ctx.state["bound_rules"][r_id]["rule_source"]
        print(f"[{r_id} | {src_tag}] {sql}")

    print("\n" + "=" * 78)
    print("【产物 2】模拟外管局接口反馈文件（<ERRRECORDS> XML 报文）")
    print("=" * 78)
    print(xml_text)

    print("\n" + "=" * 78)
    print(f"【产物 3】EventLog 不可变事件账本全轨迹（共 {len(ledger.events)} 条事实）")
    print("=" * 78)
    for ev in ledger.events:
        print(f"  #{ev.seq:02d} [{ev.kind:<20}] @ {ev.node:<18} | {json.dumps(ev.payload, ensure_ascii=False)[:85]}")


if __name__ == "__main__":
    asyncio.run(main())