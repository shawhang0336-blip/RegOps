"""Step 3：测试验证与生产交付层（Layer 6: Delivery，对应前端 03 /runs「测试与下载」）。

完整覆盖第三阶段三步闭环：
- `testing/`（3.1 造数 + 3.2 AB 沙箱真跑验证）：`mutations.py` 反例引擎、`stage_data.py` 黄金正例造数、`stage_audit.py` 变异反例用例、`sandbox_audit.py` Oracle AB 沙箱执行器、`nodes_back.py` 后半段验证与三路自愈节点；
- `workflow/`（全流程编排入口）：`entrypoints.py` 拼装 Layer 5 Step 2 节点 + Layer 6 Step 3 节点，驱动唯一 Scheduler 完成创建/确权/测试确认/自愈重试/重审/取消/删除/回放；
- `run_diff.py`：跨验证批次规则与生产 SQL 差异对比引擎；
- `production.py` / `production_service.py`（3.3 打包下载）：生产 DDL / 码表 / 校验规则 ZIP 包组装与交付门禁；
- `business_db.py`：业务库只读抽样探查连接器（`ReadOnlyBusinessDatabase`）。
"""
