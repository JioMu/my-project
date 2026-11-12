# 墨刀原型 Stage 0 双路分批抽取保序并发与防截断融合优化设计

- **日期**：2026-11-11
- **模块**：`apps/requirement_analysis/modao_extractor.py` (Stage 0 双路需求抽取与交叉融合)
- **状态**：已确认（Approved by User）

---

## 1. 背景与现状瓶颈

在对包含 28 个功能页面及数十个交互子状态的大型单文件 H5 原型（`hanakoi-v1.10.html`）执行 Stage 0 读取时，实测日志（任务 99 / 任务 100）暴露出 Stage 0 后半段（`dual_fusion_requirements`）存在显著串行等待与输出截断瓶颈：

1. **分批抽取串行阻塞（`_dual_extract_one`）**：
   - 虽然外层通过 `asyncio.gather` 并行了 DOM 路与视觉路，但 `_dual_extract_one` 内部使用串行 `for batch_no, batch in enumerate(batches, 1):` 循环逐批调用 LLM。
   - 在任务 100 中，视觉路切出 27 个批次，串行排队请求 DeepSeek 耗时 **8 分 36 秒**（占缓存命中场景下总耗时 11 分 26 秒的 **75.2%**）。
2. **缺少批次级手动终止检查与单批重试**：
   - `_dual_extract_one` 未接收 `prototype_id`，用户在前端点击“停止任务”时，排队中的批次无法及时中断。
   - 单批次偶发网络超时会直接抛异常中断整路，导致已跑完的其余批次作废。
3. **双路交叉融合单次超长截断（`_dual_fuse`）**：
   - 当 `dom_reqs`（81 条）+ `vis_reqs`（467 条）总计达 548 条时，单次请求融合输出到 `31,317` 字符超出 `max_tokens=16000` 上限被截断，导致 JSON 解析返回空（`parsed=0`），白耗 **1 分 12 秒** 且退化为未去重的简单并集相加。
4. **子状态视觉文本重复拼接膨胀（`_extract_singlefile_h5`）**：
   - 子状态截图（`st1 ~ st17`）中包含大量与该页主图完全重复的公共导航及右侧固定 PRD 说明栏文本，直接拼接导致单页 `vision_text` 膨胀数倍，从而切出过多冗余批次。

---

## 2. 核心设计方案

### 2.1 `_dual_extract_one`：共享信号量 + 槽位归位保序并发
- **共享并发限流**：在 `dual_fusion_requirements` 中创建共享信号量 `sem = asyncio.Semaphore(DUAL_EXTRACT_CONCURRENCY)`（默认 `5`），同时传入 DOM 路和视觉路的 `_dual_extract_one`，确保两路同时向 LLM 发起的总并发请求数严格 `<= 5`，避免触发 HTTP 429 限流。
- **100% 顺序保证（槽位归位）**：
  - 预先按批次总数初始化固定长度结果槽位：`batch_results: List[List[Dict]] = [[] for _ in range(len(batches))]`。
  - 每个批次协程跑完后仅写入自身下标槽位 `batch_results[idx] = parsed`。
  - `asyncio.gather` 全部完成后，按 `0 -> len(batches) - 1` 顺序展平合并：`requirements = [req for sub in batch_results for req in sub]`，保证最终需求顺序与原串行逻辑 100% 一致。
- **跨片标题保留**：当单页块 `block` 超过 10,000 字符被切分为多个 `piece` 时，若首行带有 `### 页面名` 标题，自动为第 2 片及后续分片冠以相同页面标题头，防止跨片丢失 `source_pages` 归属。

### 2.2 终止检查透传与单批退避重试
- **秒级终止响应**：将 `prototype_id` 透传至 `_dual_extract_one` 与 `_dual_fuse`，在每个批次**进入 `async with sem:` 前**及**获得信号量后**均调用 `_raise_if_terminated(prototype_id)`。
- **单批退避重试**：每个批次请求失败（或返回空解析结果但输入非空）时，自动退避 `2s` 重试 1 次；重试仍失败再按原契约抛出 `RuntimeError`。

### 2.3 `_dual_fuse`：防截断分批交叉融合
- **阈值判断**：设定单次融合安全阈值 `DUAL_FUSE_BATCH_LIMIT = 100`（`len(dom_reqs) + len(vis_reqs) <= 100` 时直接单次融合，保持原行为不变）。
- **按页面归组分批融合**：
  - 当总条数 `> 100` 时，按需求项的 `source_pages[0]`（或 `module`）将同一页面的 `dom_reqs` 与 `vis_reqs` 归入同一个桶，保证同一页面的双路需求必然在同一个融合批次内完成交叉比对。
  - 将各桶按累计条数 `<= 100` 打包为若干融合批次（`fuse_batches`），复用共享信号量 `sem` 并发调用单批融合逻辑，按槽位顺序归位合并。
  - 若某批融合输出仍解析失败，仅对该批降级为该批内的 `dom + vis` 并集，不影响其他批次的融合成果。

### 2.4 `_extract_singlefile_h5`：子状态视觉文本行级去重
- 在将子状态 `st_txt` 拼入页面 `vision_text` 前，以该页面已收录的 `vision_text` 行集合（`seen_vis_lines`）为基准，过滤掉完全重复的非空行，仅将子状态新增/变化的文本行追加到 `[状态:{st_name}]` 段落下。若子状态无新增文本行则不追加冗余段落。

---

## 3. 验收与测试标准

1. **单元测试覆盖**：
   - 验证 `_dual_extract_one` 在乱序返回（模拟后序 batch 先返回）时，最终输出的 `requirements` 顺序与批次顺序 100% 严格一致。
   - 验证 `dual_fusion_requirements` 两路并发受共享 `Semaphore` 限制，最大并发不超过设定值。
   - 验证 `prototype_id` 命中 `TERMINATE_REQUESTED` 时，并发批次立即中断并抛出 `ModaoTerminateError`。
   - 验证 `_dual_fuse` 在条数 `> 100` 时自动触发按页面分组分批融合，且单批失败时仅局部降级。
   - 验证子状态视觉文本拼接时自动剔除与主图完全重复的文本行。
2. **回归测试**：
   - 既有 `test_modao_workflow_safety` 与 `test_modao_generation_gate` 全部通过。
