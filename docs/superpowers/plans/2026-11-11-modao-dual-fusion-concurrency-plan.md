# 墨刀 Stage 0 双路分批抽取保序并发与防截断融合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除墨刀 Stage 0 双路需求抽取（`_dual_extract_one`）的串行批次等待瓶颈、双路融合（`_dual_fuse`）的超长输出截断退化以及子状态视觉文本重复拼接膨胀，在 100% 保证输出顺序与业务语义不变的前提下将后半段耗时从近 10 分钟缩短至 1.5 分钟以内。

**Architecture:**
1. 共享信号量保序并发：在 `dual_fusion_requirements` 层创建共享 `asyncio.Semaphore(5)`，在 `_dual_extract_one` 中通过预分配固定下标槽位 `batch_results[idx]` 并发执行各批次并严格按原序归位；
2. 终止检查与单批退避重试：透传 `prototype_id` 在排队前后检测 `TERMINATE_REQUESTED`，并对单批次异常/空解析做 1 次退避重试；
3. 防截断分批交叉融合：在 `_dual_fuse` 中当 `len(dom_reqs) + len(vis_reqs) > 100` 时按页面归组打包（每批 <= 100 条）并发融合；
4. 子状态视觉文本行级去重：在 `_extract_singlefile_h5` 组装子状态 `vision_text` 时剔除与主图完全重复的文本行。

**Tech Stack:** Python 3.12, Asyncio, Django 4.2, Unittest.

## Global Constraints

- 严格保序：并发抽取与分批融合后的输出列表顺序必须与原串行批次顺序 100% 一致。
- 信号量共享：DOM 路与视觉路共用同一 `asyncio.Semaphore(DUAL_EXTRACT_CONCURRENCY)`，总并发数不超过 5。
- 终止响应：`ModaoTerminateError` 必须向上透传，不得被双路融合外层通用 `except Exception` 吞没。

---

### Task 1: `_dual_extract_one` 共享信号量保序并发、跨片标题保留、终止检查与单批重试

**Files:**
- Modify: `apps/requirement_analysis/modao_extractor.py:4762-4870`
- Test: `apps/requirement_analysis/tests/test_modao_workflow_safety.py`

**Interfaces:**
- `dual_fusion_requirements(self, pages, fusion_config, prototype_id=None)`
- `_dual_extract_one(self, aim_service, fusion_config, blocks, sem=None, prototype_id=None, path_label='extract')`

- [x] **Step 1: 编写保序并发、共享信号量限流、跨片标题与终止响应单元测试**
- [x] **Step 2: 运行测试确认旧代码未满足并发与保序/终止预期**
- [x] **Step 3: 实现 `_dual_extract_one` 槽位保序并发、重试与终止检查**
- [x] **Step 4: 运行测试验证通过**

---

### Task 2: `_dual_fuse` 超长需求按页面归组分批并发融合

**Files:**
- Modify: `apps/requirement_analysis/modao_extractor.py:4871-4910`
- Test: `apps/requirement_analysis/tests/test_modao_workflow_safety.py`

**Interfaces:**
- `_dual_fuse(self, aim_service, fusion_config, dom_reqs, vis_reqs, dom_len, sem=None, prototype_id=None)`

- [x] **Step 1: 编写 >100 条需求自动分批融合与局部降级单元测试**
- [x] **Step 2: 实现 `_dual_fuse` 分批融合与 `_dual_fuse_single_batch` 辅助方法**
- [x] **Step 3: 运行测试验证通过**

---

### Task 3: `_extract_singlefile_h5` 子状态视觉文本行级去重

**Files:**
- Modify: `apps/requirement_analysis/modao_extractor.py:2584-2605`
- Test: `apps/requirement_analysis/tests/test_modao_workflow_safety.py`

**Interfaces:**
- `_dedup_state_vision_text(base_text: str, state_text: str) -> str`

- [x] **Step 1: 编写子状态文本去重单元测试**
- [x] **Step 2: 实现 `_dedup_state_vision_text` 并在 `_extract_singlefile_h5` 中接入**
- [x] **Step 3: 运行全量安全与门禁测试验证通过**
