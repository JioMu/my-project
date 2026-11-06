<template>
  <el-card class="artifact-card" :class="{ 'has-content': !!rawText }">
    <template #header>
      <div class="ac-head">
        <b>{{ icon }} {{ title }}</b>
        <div class="ac-actions">
          <template v-if="!editing">
            <!-- 需求结构化视图切换（可视化 vs YAML 原文） -->
            <el-radio-group
              v-if="isSummary && rawText"
              v-model="summaryViewMode"
              size="small"
              class="ac-view-toggle"
            >
              <el-radio-button label="visual">👁️ 可视化</el-radio-button>
              <el-radio-button label="raw">📜 YAML</el-radio-button>
            </el-radio-group>
            <span v-if="itemCount" class="ac-count">{{ itemCount }} {{ isSummary ? '个模块' : '条' }}</span>
            <el-button size="small" type="primary" plain @click="startEdit">✏️ 编辑</el-button>
          </template>
          <template v-else>
            <el-button size="small" @click="cancelEdit">取消</el-button>
            <el-button v-if="isJson || isSummary" size="small" @click="formatDraft">格式化</el-button>
            <el-button size="small" type="primary" :loading="saving" @click="saveDraft">💾 保存</el-button>
          </template>
        </div>
      </div>
    </template>

    <!-- 编辑态：原文编辑（澄清保留逐条视图入口） -->
    <div v-if="editing" class="ac-edit">
      <el-alert v-if="!jsonValid" type="error" :closable="false" show-icon
        title="当前内容不是合法 JSON，保存会被后端拒绝" />
      <el-alert v-if="!yamlValid" type="error" :closable="false" show-icon
        title="当前内容不是合法 YAML，保存后可能导致后续分析无法识别" />
      <div class="ac-tip">
        编辑的是<b>本画布</b>的产物；保存后任务级合并视图会自动重算。
      </div>
      <el-input v-model="draft" type="textarea" :autosize="{ minRows: 10, maxRows: 26 }" class="ac-textarea" />
    </div>

    <!-- 澄清：逐条视图 + 人工结论 -->
    <div v-else-if="kind === 'clarify'" class="ac-clarify">
      <template v-if="clarifyItems.length">
        <div v-for="(it, i) in clarifyItems" :key="i" class="ci-item">
          <div class="ci-head">
            <div class="ci-head-left">
              <el-tag size="mini" :type="typeTag(it.type)">{{ it.type || '待确认' }}</el-tag>
              <span class="ci-loc">{{ it.location || '未标注位置' }}</span>
              <el-tag v-if="it.evidence_valid === false" size="mini" type="warning">证据未匹配原文</el-tag>
              <el-tag v-if="(it.resolution || it.answer || '').trim()" size="mini" type="success">已填结论</el-tag>
            </div>
            <el-button
              size="mini"
              type="danger"
              plain
              class="ci-delete-btn"
              :loading="saving"
              @click="deleteClarifyItem(i)"
            >
              🗑️ 删除
            </el-button>
          </div>
          <div v-if="it.evidence" class="ci-row"><label>原文依据</label><span>{{ it.evidence }}</span></div>
          <div v-if="it.issue || it.uncertainty" class="ci-row"><label>问题</label><span>{{ it.issue || it.uncertainty }}</span></div>
          <div v-if="it.suggested_question" class="ci-row"><label>建议提问</label><span>{{ it.suggested_question }}</span></div>
          <div v-if="it.impact" class="ci-row"><label>影响范围</label><span>{{ it.impact }}</span></div>
          <el-input v-model="clarifyReplies[i]" type="textarea" :rows="2" class="ci-input"
            placeholder="填写人工结论（保存后写入该条 resolution，供后续用例生成引用）" />
        </div>
        <el-button size="small" type="primary" :loading="saving" class="ci-save" @click="saveClarify">💾 保存全部结论</el-button>
      </template>
      <el-empty v-else description="暂无澄清项（已确认无需澄清或已删除）" :image-size="60" />
    </div>

    <!-- 需求结构化可视化卡片视图 -->
    <div v-else-if="isSummary && summaryViewMode === 'visual'" class="ac-summary-visual">
      <RequirementSummaryViewer :raw="rawText" />
    </div>

    <!-- 结构化卡片视图（测试点 / 用例 / 冒烟 / 其它 JSON） -->
    <div v-else-if="structuredItems.length" class="ac-structured">
      <div v-for="(item, i) in structuredItems" :key="i" class="obj-card" :class="{ 'obj-expanded': isExpanded(i) }">
        <div class="obj-head" @click="toggleItem(i)">
          <div class="obj-head-left">
            <span class="obj-no">#{{ i + 1 }}</span>
            <span v-if="item.id" class="obj-id-tag">{{ item.id }}</span>
            <el-tag v-if="item.priority" size="mini" :type="item.priority === 'P0' ? 'danger' : (item.priority === 'P1' ? 'warning' : 'info')">
              {{ item.priority }}
            </el-tag>
          </div>
          <el-button
            size="mini"
            type="danger"
            plain
            class="obj-delete-btn"
            :loading="saving"
            @click.stop="deleteStructuredItem(i)"
          >
            🗑️ 删除
          </el-button>
        </div>
        <!-- 收起态：只显示 标题 / 描述 -->
        <div v-if="!isExpanded(i)" class="obj-rows">
          <div v-for="(row, j) in summaryRows(item)" :key="'s' + j" class="obj-row">
            <label>{{ row[0] }}</label><span>{{ row[1] }}</span>
          </div>
          <div v-if="hiddenRowCount(item) > 0" class="obj-toggle" @click="toggleItem(i)">
            展开全部字段（还有 {{ hiddenRowCount(item) }} 项）▾
          </div>
        </div>
        <!-- 展开态：全部字段 + 收起入口 -->
        <div v-else class="obj-rows">
          <div v-for="(row, j) in flatten(item)" :key="j" class="obj-row">
            <label>{{ row[0] }}</label><span>{{ row[1] }}</span>
          </div>
          <div class="obj-toggle" @click="toggleItem(i)">收起 ▴</div>
        </div>
      </div>
      <div v-if="itemCount > structuredItems.length" class="ac-more">
        仅展示前 {{ structuredItems.length }} 条，共 {{ itemCount }} 条，完整内容请点「编辑」查看
      </div>
    </div>

    <!-- 空状态：如果是 JSON 格式且已无内容，或者 rawText 解析为空数组 -->
    <div v-else-if="isJson && rawText && !parsedArray.length" class="ac-empty">
      <el-empty :description="'暂无' + title + '（已全部处理或已删除）'" :image-size="60" />
    </div>

    <pre v-else-if="rawText" class="stage-pre">{{ rawText }}</pre>
    <el-empty v-else description="尚未生成" :image-size="60" />
  </el-card>
</template>

<script>
import request from '@/utils/api'
import { load as yamlLoad, dump as yamlDump } from 'js-yaml'
import RequirementSummaryViewer from './RequirementSummaryViewer.vue'

/** 字段名 → 中文标签（未命中的字段直接显示原名） */
const FIELD_LABELS = {
  id: 'ID', case_id: '用例ID', module: '模块', feature_module: '功能模块',
  title: '标题', name: '名称', case_name: '用例名', description: '描述', desc: '描述',
  priority: '优先级', type: '类型', test_type: '测试类型', category: '分类',
  precondition: '前置条件', preconditions: '前置条件', pre_condition: '前置条件',
  steps: '步骤', test_steps: '步骤', expected: '预期结果', expected_result: '预期结果',
  pass_criteria: '通过标准', notes: '备注', remark: '备注', requirement_ref: '需求引用',
  location: '位置', evidence: '原文依据', issue: '问题', uncertainty: '不确定点',
  suggested_question: '建议提问', impact: '影响范围', resolution: '人工结论', answer: '结论',
  audit_kind: '审计类型', evidence_valid: '证据匹配', smoke: '冒烟', source: '来源',
  risk_level: '风险等级', tags: '标签', testcase: '用例', design_method: '设计方法',
  is_smoke: '冒烟用例', cover_points: '覆盖测试点', points: '测试点', content: '内容',
}

const MAX_STRUCTURED_ITEMS = 60

export default {
  name: 'CanvasArtifactCard',
  components: {
    RequirementSummaryViewer,
  },
  props: {
    /** 卡片标题 */
    title: { type: String, required: true },
    icon: { type: String, default: '' },
    /** 后端字段名（page_summary / page_clarification / ...） */
    field: { type: String, required: true },
    /** 原文 */
    raw: { type: [String, null], default: '' },
    /** text | clarify | json */
    kind: { type: String, default: 'json' },
    protoId: { type: [String, Number], required: true },
    pageId: { type: [String, Number], required: true },
  },
  data() {
    return {
      editing: false,
      draft: '',
      saving: false,
      clarifyItems: [],
      clarifyReplies: [],
      summaryViewMode: 'visual',
      /** 默认收起：记录已展开条目的下标 */
      expandedIndexes: [],
    }
  },
  computed: {
    rawText() {
      return String(this.raw || '')
    },
    isJson() {
      return String(this.field).endsWith('_json')
    },
    isSummary() {
      return this.field === 'page_summary' || this.field === 'requirement_summary' || this.kind === 'summary'
    },
    jsonValid() {
      if (!this.isJson) return true
      const t = (this.draft || '').trim()
      if (!t) return true
      try { JSON.parse(t); return true } catch (e) { return false }
    },
    yamlValid() {
      if (!this.isSummary) return true
      const t = (this.draft || '').trim()
      if (!t) return true
      try {
        let clean = t
        const m = clean.match(/^```(?:yaml|yml)?\s*[\r\n]+([\s\S]*?)(?:[\r\n]+```|$)/i)
        if (m) clean = m[1].trim()
        yamlLoad(clean)
        return true
      } catch (e) {
        return false
      }
    },
    summaryModulesCount() {
      if (!this.isSummary || !this.rawText) return 0
      try {
        let clean = this.rawText.trim()
        const m = clean.match(/^```(?:yaml|yml)?\s*[\r\n]+([\s\S]*?)(?:[\r\n]+```|$)/i)
        if (m) clean = m[1].trim()
        const obj = yamlLoad(clean)
        if (obj && Array.isArray(obj.modules)) return obj.modules.length
      } catch (e) {}
      return 0
    },
    /** 解析出的 JSON 数组（用于条数统计与结构化展示） */
    parsedArray() {
      if (Array.isArray(this.raw)) return this.raw
      const t = this.rawText.trim()
      if (!t) return []
      try {
        const v = JSON.parse(t)
        if (Array.isArray(v)) return v
        if (v && Array.isArray(v.smoke_testcases)) return v.smoke_testcases
        if (v && Array.isArray(v.data)) return v.data
      } catch (e) { /* 非纯 JSON */ }
      return []
    },
    structuredItems() {
      if (this.kind === 'clarify' || this.isSummary) return []
      return this.parsedArray.filter(x => x && typeof x === 'object')
        .slice(0, MAX_STRUCTURED_ITEMS)
    },
    itemCount() {
      if (this.kind === 'clarify') return this.clarifyItems.length
      if (this.isSummary) return this.summaryModulesCount
      return this.parsedArray.length
    },
  },
  watch: {
    raw: {
      immediate: true,
      handler() { this.parseClarify() },
    },
  },
  methods: {
    /** 收起态摘要行：只取 标题 / 描述 类字段 */
    summaryRows(item) {
      const keys = ['title', 'name', 'case_name', 'description', 'desc']
      const out = []
      for (const [k, v] of Object.entries(item || {})) {
        if (!keys.includes(k)) continue
        if (v === null || v === undefined || v === '') continue
        const label = this.lbl(k)
        if (Array.isArray(v)) {
          const txt = v.map(x => (x && typeof x === 'object' ? JSON.stringify(x) : String(x))).join(' / ')
          if (txt) out.push([label, txt])
        } else if (typeof v === 'object') {
          out.push(...this.flatten(v, label))
        } else {
          out.push([label, String(v)])
        }
      }
      if (!out.length) out.push(['内容', JSON.stringify(item).slice(0, 120)])
      return out
    },
    /** 收起时被隐藏的字段数（顶层） */
    hiddenRowCount(item) {
      const keys = ['title', 'name', 'case_name', 'description', 'desc']
      return Object.entries(item || {})
        .filter(([k, v]) => !keys.includes(k) && v !== null && v !== undefined && v !== '')
        .length
    },
    isExpanded(i) {
      return this.expandedIndexes.includes(i)
    },
    toggleItem(i) {
      const idx = this.expandedIndexes.indexOf(i)
      if (idx >= 0) this.expandedIndexes.splice(idx, 1)
      else this.expandedIndexes.push(i)
    },
    base() {
      return '/requirement-analysis/api/modao'
    },
    lbl(k) {
      return FIELD_LABELS[k] || k
    },
    typeTag(t) {
      const m = { '模糊点': 'warning', '缺失点': 'danger', '边界点': 'info' }
      return m[t] || ''
    },
    /** 从原文解析澄清数组：纯 JSON / ```json 代码块 / 首个 [...] 片段，三种策略兼容 */
    _parseClarifyArray(raw) {
      if (Array.isArray(raw)) return raw
      const text = String(raw || '')
      if (!text.trim()) return []
      try {
        const v = JSON.parse(text)
        if (Array.isArray(v) && v.length) return v
      } catch (e) { /* 继续下一种策略 */ }
      const m = text.match(/```(?:json|yaml|yml)?\s*\n([\s\S]*?)(?:```|$)/i)
      if (m) {
        try {
          const v = JSON.parse(m[1].trim())
          if (Array.isArray(v) && v.length) return v
        } catch (e) { /* noop */ }
      }
      const arrMatch = text.match(/\[[\s\S]*?\](?=\s*[\n]|$)/)
      if (arrMatch) {
        try {
          const v = JSON.parse(arrMatch[0])
          if (Array.isArray(v) && v.length) return v
        } catch (e) { /* noop */ }
      }
      // 容错兜底：若 JSON 列表被截断（末尾缺少 ]），尝试截取至最后一个完整对象的 '}' 并闭合
      const lastBrace = text.lastIndexOf('}')
      const firstBracket = text.indexOf('[')
      if (firstBracket !== -1 && lastBrace > firstBracket) {
        try {
          const salvaged = JSON.parse(text.slice(firstBracket, lastBrace + 1) + '\n]')
          if (Array.isArray(salvaged) && salvaged.length) return salvaged
        } catch (e) { /* noop */ }
      }
      return []
    },
    /** 把新数组文本写回原文：纯 JSON 整体替换；markdown 包裹时只替换其中的 JSON 片段 */
    _replaceClarifyJsonInRaw(raw, newText) {
      const text = String(raw || '')
      if (!text.trim()) return newText
      try {
        const v = JSON.parse(text)
        if (Array.isArray(v)) return newText
      } catch (e) { /* 走片段替换 */ }
      const m = text.match(/```(?:json|yaml|yml)?\s*\n([\s\S]*?)(?:```|$)/i)
      if (m) {
        return text.slice(0, m.index) + m[0].replace(m[1], () => newText) + text.slice(m.index + m[0].length)
      }
      const arrMatch = text.match(/\[[\s\S]*?\](?=\s*[\n]|$)/)
      if (arrMatch) {
        return text.slice(0, arrMatch.index) + newText + text.slice(arrMatch.index + arrMatch[0].length)
      }
      return newText
    },
    parseClarify() {
      if (this.kind !== 'clarify') return
      const items = this._parseClarifyArray(this.rawText)
      if (items.length && items[0] && typeof items[0] === 'object') {
        this.clarifyItems = items
        this.clarifyReplies = items.map(it => (it && (it.resolution || it.answer)) || '')
      } else {
        this.clarifyItems = []
        this.clarifyReplies = []
      }
    },
    /** 把任意层级的对象摊平成 [标签, 值] 行，兼容 {module, testcase:{...}} 这类嵌套结构 */
    flatten(obj, prefix = '') {
      const out = []
      for (const [k, v] of Object.entries(obj || {})) {
        if (v === null || v === undefined || v === '') continue
        if (k === 'evidence_valid' && v === true) continue
        const label = (prefix ? prefix + ' · ' : '') + this.lbl(k)
        if (Array.isArray(v)) {
          const txt = v.map(x => (x && typeof x === 'object' ? JSON.stringify(x) : String(x))).join(' / ')
          if (txt) out.push([label, txt])
        } else if (typeof v === 'object') {
          out.push(...this.flatten(v, label))
        } else {
          out.push([label, String(v)])
        }
      }
      return out
    },
    startEdit() {
      this.draft = this.rawText
      this.editing = true
    },
    cancelEdit() {
      this.editing = false
      this.draft = ''
    },
    formatDraft() {
      if (this.isSummary) {
        try {
          let clean = this.draft.trim()
          const m = clean.match(/^```(?:yaml|yml)?\s*[\r\n]+([\s\S]*?)(?:[\r\n]+```|$)/i)
          if (m) clean = m[1].trim()
          const obj = yamlLoad(clean)
          this.draft = yamlDump(obj, { indent: 2 })
          this.$message.success('YAML 已格式化')
        } catch (e) {
          this.$message.warning('无法格式化：当前内容不是合法 YAML')
        }
        return
      }
      try {
        this.draft = JSON.stringify(JSON.parse(this.draft), null, 2)
      } catch (e) {
        this.$message.warning('无法格式化：当前内容不是合法 JSON')
      }
    },
    saveDraft() {
      if (!this.jsonValid) {
        this.$message.error('内容不是合法 JSON，请先修正')
        return
      }
      if (!this.yamlValid) {
        this.$message.error('内容不是合法 YAML，请先修正')
        return
      }
      this.saveValue(this.draft).then(ok => { if (ok) this.editing = false })
    },
    saveClarify() {
      if (!this.clarifyItems.length) return
      const items = this.clarifyItems.map((it, i) => ({
        ...it,
        resolution: String(this.clarifyReplies[i] || '').trim(),
      }))
      const value = this._replaceClarifyJsonInRaw(
        this.rawText, JSON.stringify(items, null, 2))
      this.saveValue(value).then(ok => { if (ok) this.clarifyReplies = items.map(it => it.resolution) })
    },
    async deleteClarifyItem(index) {
      const it = this.clarifyItems[index]
      const loc = (it && (it.location || it.issue)) ? `「${it.location || it.issue}」` : '该澄清点'
      try {
        await this.$confirm(
          `确定要删除澄清点 ${loc} 吗？\n删除后该条目将不再参与后续测试设计与用例生成。`,
          '删除澄清点',
          {
            confirmButtonText: '确定删除',
            cancelButtonText: '取消',
            type: 'warning',
          }
        )
      } catch (e) {
        return
      }

      const newItems = this.clarifyItems.filter((_, i) => i !== index)
      const newReplies = this.clarifyReplies.filter((_, i) => i !== index)
      const itemsToSave = newItems.map((item, i) => ({
        ...item,
        resolution: String(newReplies[i] || '').trim(),
      }))
      const value = this._replaceClarifyJsonInRaw(
        this.rawText, JSON.stringify(itemsToSave, null, 2))
      const ok = await this.saveValue(value, '已成功删除该澄清点')
      if (ok) {
        this.clarifyItems = newItems
        this.clarifyReplies = newReplies
      }
    },
    async deleteStructuredItem(index) {
      const it = this.parsedArray[index]
      if (!it) return
      const name = (it.title || it.name || it.case_name || '').trim()
      const label = (it.id && name)
        ? `「${it.id} · ${name}」`
        : (name ? `「${name}」` : (it.id ? `「${it.id}」` : `第 ${index + 1} 项`))
      try {
        await this.$confirm(
          `确定要删除${this.title} ${label} 吗？\n删除后该条目将不再参与后续流程。`,
          `删除${this.title}`,
          {
            confirmButtonText: '确定删除',
            cancelButtonText: '取消',
            type: 'warning',
          }
        )
      } catch (e) {
        return
      }

      let newValue = ''
      try {
        const parsed = JSON.parse(this.rawText.trim())
        if (Array.isArray(parsed)) {
          const nextArr = parsed.filter((_, idx) => idx !== index)
          newValue = JSON.stringify(nextArr, null, 2)
        } else if (parsed && Array.isArray(parsed.smoke_testcases)) {
          parsed.smoke_testcases.splice(index, 1)
          newValue = JSON.stringify(parsed, null, 2)
        } else if (parsed && Array.isArray(parsed.data)) {
          parsed.data.splice(index, 1)
          newValue = JSON.stringify(parsed, null, 2)
        } else if (parsed && Array.isArray(parsed.testpoints)) {
          parsed.testpoints.splice(index, 1)
          newValue = JSON.stringify(parsed, null, 2)
        } else {
          this.$message.error('无法识别当前数据结构，请在编辑原文中手动调整')
          return
        }
      } catch (e) {
        this.$message.error('解析数据失败：' + e.message)
        return
      }

      await this.saveValue(newValue, `已成功删除${this.title} ${label}`)
    },
    async saveValue(value, successMsg) {
      this.saving = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/pages/${this.pageId}/edit/`,
          method: 'patch',
          data: { field: this.field, value },
        })
        const d = res.data || {}
        this.$message.success(successMsg || d.detail || '已保存')
        if (d.stale_stages && d.stale_stages.length) {
          this.$message.warning('该画布下游产物基于旧内容，建议重跑对应阶段')
        }
        const g = d.gate
        if (g && g.approved && g.fingerprint_matches === false) {
          this.$message.warning('内容已变更，原「批准生成」已失效，需重新批准后再生成最终用例')
        }
        this.$emit('saved', d)
        return true
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
        return false
      } finally {
        this.saving = false
      }
    },
  },
}
</script>

<style scoped lang="scss">
.artifact-card {
  margin-bottom: 14px;
  &.stage-current { border: 1px solid #409eff; box-shadow: 0 0 8px rgba(64,158,255,.25); }

  .ac-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .ac-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    .ac-view-toggle {
      margin-right: 4px;
    }
    .ac-count { color: #909399; font-size: 12px; }
  }
  .ac-summary-visual {
    width: 100%;
  }
  .ac-edit {
    .ac-tip { font-size: 12px; color: #909399; margin: 8px 0; }
    .ac-textarea :deep(textarea) {
      font-family: Consolas, Monaco, 'Courier New', monospace;
      font-size: 12px;
      line-height: 1.6;
    }
  }
  .stage-pre {
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 360px;
    overflow: auto;
    background: #fafafa;
    padding: 10px;
    border-radius: 6px;
    font-size: 12px;
    margin: 0;
  }

  /* 澄清逐条 */
  .ci-item {
    border: 1px solid #ebeef5;
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 10px;
    background: #fcfcfd;
    .ci-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 6px;
    }
    .ci-head-left {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      flex: 1;
    }
    .ci-delete-btn {
      margin-left: auto;
    }
    .ci-loc { font-size: 12px; color: #606266; font-weight: 600; }
    .ci-row {
      display: flex;
      gap: 8px;
      font-size: 12px;
      line-height: 1.7;
      label { flex: 0 0 60px; color: #909399; }
      span { flex: 1; color: #303133; word-break: break-word; }
    }
    .ci-input { margin-top: 8px; }
  }
  .ci-save { margin-bottom: 4px; }

  /* 结构化卡片 */
  .obj-card {
    border: 1px solid #ebeef5;
    border-radius: 6px;
    padding: 10px 12px;
    margin-bottom: 10px;
    background: #fcfcfd;
    transition: all 0.2s;
    &:hover {
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);
    }
    .obj-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
      padding-bottom: 6px;
      border-bottom: 1px dashed #ebeef5;
      .obj-head-left {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .obj-no { font-size: 13px; font-weight: bold; color: #909399; }
      .obj-id-tag {
        font-size: 11px;
        color: #409eff;
        background: #ecf5ff;
        border: 1px solid #d9ecff;
        border-radius: 4px;
        padding: 1px 6px;
        font-family: Consolas, Monaco, monospace;
      }
      .obj-delete-btn { margin-left: auto; }
    }
    .obj-rows { width: 100%; }
    .obj-row {
      display: flex;
      gap: 8px;
      font-size: 12px;
      line-height: 1.7;
      label { flex: 0 0 88px; color: #909399; }
      span { flex: 1; color: #303133; word-break: break-word; white-space: pre-wrap; }
    }
    .obj-toggle {
      margin-top: 6px;
      font-size: 12px;
      color: #409eff;
      cursor: pointer;
      user-select: none;
      &:hover { text-decoration: underline; }
    }
    .obj-head { cursor: pointer; }
  }
  .ac-more { font-size: 12px; color: #909399; }
}
</style>
