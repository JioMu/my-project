<template>
  <div class="req-summary-viewer">
    <!-- 解析失败或非结构化内容降级展示 -->
    <template v-if="!summaryData">
      <div v-if="rawText" class="rsv-fallback">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="当前内容为纯文本格式（未能解析为标准需求模型），以代码视图呈现"
          class="rsv-fallback-alert"
        />
        <pre class="stage-pre">{{ rawText }}</pre>
      </div>
      <el-empty v-else description="暂无需求结构化内容" :image-size="60" />
    </template>

    <!-- 标准结构化视图 -->
    <template v-else>
      <!-- 1. 顶部：需求总览与版本目标 -->
      <div v-if="summaryData.requirement_overview || versionGoals.length" class="rsv-overview-card">
        <div v-if="summaryData.requirement_overview" class="rsv-overview-block">
          <div class="rsv-block-title">
            <span class="rsv-title-icon">📋</span>
            <span class="rsv-title-text">需求概述</span>
          </div>
          <div class="rsv-overview-text">
            {{ summaryData.requirement_overview }}
          </div>
        </div>

        <div v-if="versionGoals.length" class="rsv-goals-block">
          <div class="rsv-block-title">
            <span class="rsv-title-icon">🎯</span>
            <span class="rsv-title-text">版本核心目标</span>
            <span class="rsv-count-badge">({{ versionGoals.length }})</span>
          </div>
          <div class="rsv-goals-grid">
            <div v-for="(goal, gi) in versionGoals" :key="gi" class="rsv-goal-pill">
              <span class="goal-index">{{ gi + 1 }}</span>
              <span class="goal-text">{{ goal }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. 核心模块与功能分解 (Modules) -->
      <div v-if="modules.length" class="rsv-modules-section">
        <div class="rsv-section-header">
          <div class="rsv-section-title">
            <span class="rsv-title-icon">🧩</span>
            <span>功能模块拆分</span>
            <span class="rsv-count-tag">{{ modules.length }} 个模块</span>
          </div>
        </div>

        <div class="rsv-module-cards">
          <div
            v-for="(mod, mi) in modules"
            :key="mi"
            class="rsv-module-card"
            :class="'prio-border-' + (mod.priority || 'P1').toLowerCase()"
          >
            <!-- 模块头部 -->
            <div class="rmc-header">
              <div class="rmc-title-line">
                <span class="rmc-name">{{ mod.name || '未命名模块' }}</span>
                <el-tag
                  v-if="mod.priority"
                  size="small"
                  :type="prioTag(mod.priority)"
                  effect="dark"
                  class="rmc-prio-tag"
                >
                  {{ mod.priority }}
                </el-tag>
                <el-tag
                  v-if="mod.change_type"
                  size="small"
                  :type="changeTypeTag(mod.change_type)"
                  class="rmc-change-tag"
                >
                  {{ mod.change_type }}
                </el-tag>
                <span v-if="mod.priority_source" class="rmc-source-hint">
                  ({{ mod.priority_source }})
                </span>
              </div>
            </div>

            <!-- 模块描述 -->
            <div v-if="mod.description" class="rmc-desc">
              {{ mod.description }}
            </div>

            <!-- 核心功能点清单 -->
            <div v-if="getFeatures(mod).length" class="rmc-sub-block rmc-features-block">
              <div class="rmc-sub-title">
                <span class="sub-icon">🔑</span>
                <span>核心功能点 ({{ getFeatures(mod).length }})</span>
              </div>
              <div class="rmc-feature-items">
                <div
                  v-for="(feat, fi) in getFeatures(mod)"
                  :key="fi"
                  class="rmc-feature-item"
                >
                  <span class="feat-dot">✔</span>
                  <span class="feat-content">{{ feat }}</span>
                </div>
              </div>
            </div>

            <!-- 跨模块 / 系统影响 -->
            <div v-if="getImpacts(mod).length" class="rmc-sub-block rmc-impact-block">
              <div class="rmc-sub-title text-warning">
                <span class="sub-icon">🔗</span>
                <span>跨模块 / 关联系统影响 ({{ getImpacts(mod).length }})</span>
              </div>
              <ul class="rmc-bullet-list warning-list">
                <li v-for="(imp, ii) in getImpacts(mod)" :key="ii">
                  {{ imp }}
                </li>
              </ul>
            </div>

            <!-- 模块测试风险 -->
            <div v-if="getRisks(mod).length" class="rmc-sub-block rmc-risk-block">
              <div class="rmc-sub-title text-danger">
                <span class="sub-icon">⚠️</span>
                <span>测试分析与风险建议 ({{ getRisks(mod).length }})</span>
              </div>
              <ul class="rmc-bullet-list danger-list">
                <li v-for="(rk, ri) in getRisks(mod)" :key="ri">
                  {{ rk }}
                </li>
              </ul>
            </div>

            <!-- 原文标注与证据来源（折叠收纳） -->
            <el-collapse
              v-if="hasEvidence(mod)"
              class="rmc-collapse"
            >
              <el-collapse-item name="evidence">
                <template #title>
                  <div class="rmc-collapse-title">
                    <span class="sub-icon">📎</span>
                    <span>原型标注与逐字证据引用 ({{ getEvidence(mod).length }} 条)</span>
                  </div>
                </template>

                <!-- 来源位置标签 -->
                <div v-if="getLocations(mod).length" class="rmc-locations-row">
                  <span class="row-label">标注位置：</span>
                  <div class="location-tags">
                    <el-tag
                      v-for="(loc, li) in getLocations(mod)"
                      :key="li"
                      size="mini"
                      type="info"
                      class="loc-tag"
                    >
                      {{ loc }}
                    </el-tag>
                  </div>
                </div>

                <!-- 逐字引用列表 -->
                <div v-if="getEvidence(mod).length" class="rmc-quotes-list">
                  <div
                    v-for="(ev, ei) in getEvidence(mod)"
                    :key="ei"
                    class="rmc-quote-item"
                  >
                    <span v-if="ev.page" class="quote-page">[{{ ev.page }}]</span>
                    <span class="quote-text">“{{ ev.quote }}”</span>
                  </div>
                </div>

                <!-- 优先级判定理由 -->
                <div v-if="mod.priority_reason" class="rmc-prio-reason">
                  <span class="reason-label">定级依据：</span>
                  <span class="reason-text">{{ mod.priority_reason }}</span>
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </div>
      </div>

      <!-- 3. 业务主流程 (Business Flow) -->
      <div v-if="businessFlowSteps.length" class="rsv-flow-card">
        <div class="rsv-block-title">
          <span class="rsv-title-icon">🔄</span>
          <span class="rsv-title-text">业务主流程链路</span>
        </div>
        <div class="rsv-flow-steps">
          <div
            v-for="(step, si) in businessFlowSteps"
            :key="si"
            class="rsv-flow-step-item"
          >
            <div class="step-badge">{{ si + 1 }}</div>
            <div class="step-card">
              <span class="step-content">{{ step }}</span>
            </div>
            <div v-if="si < businessFlowSteps.length - 1" class="step-arrow">➔</div>
          </div>
        </div>
      </div>

      <!-- 4. 决策表 (Decision Tables - 若有) -->
      <div v-if="decisionTables.length" class="rsv-section-block">
        <div class="rsv-block-title">
          <span class="rsv-title-icon">📊</span>
          <span class="rsv-title-text">业务决策表 ({{ decisionTables.length }})</span>
        </div>
        <div v-for="(dt, dti) in decisionTables" :key="dti" class="rsv-dt-card">
          <div class="rsv-dt-head">
            <b>{{ dt.name || '决策表' }}</b>
            <span v-if="dt.related_module" class="rsv-dt-mod">（归属：{{ dt.related_module }}）</span>
          </div>
          <el-table
            v-if="dt.rows && dt.rows.length"
            :data="dt.rows"
            size="small"
            border
            stripe
            class="rsv-dt-table"
          >
            <el-table-column
              v-for="col in getDecisionTableColumns(dt)"
              :key="col"
              :prop="col"
              :label="col"
            />
          </el-table>
        </div>
      </div>

      <!-- 5. 待澄清问题预警 (Clarification Preview - 若阶段1识别出问题) -->
      <div v-if="clarificationCount > 0" class="rsv-clarify-preview-card">
        <div class="rcp-header">
          <div class="rcp-title-line">
            <span class="rcp-icon">❓</span>
            <span class="rcp-title">阶段1待确认点预警（共 {{ clarificationCount }} 项，可在【需求澄清】阶段逐项答复）</span>
          </div>
          <div class="rcp-tags">
            <el-tag v-if="ambiguousPoints.length" size="small" type="warning">歧义点 {{ ambiguousPoints.length }}</el-tag>
            <el-tag v-if="missingPoints.length" size="small" type="danger">缺失点 {{ missingPoints.length }}</el-tag>
            <el-tag v-if="boundaryPoints.length" size="small" type="info">边界点 {{ boundaryPoints.length }}</el-tag>
          </div>
        </div>
        <div class="rcp-items">
          <div
            v-for="(item, ii) in previewClarifyItems"
            :key="ii"
            class="rcp-item"
          >
            <el-tag size="mini" :type="item.tagType">{{ item.typeName }}</el-tag>
            <span class="rcp-item-issue">{{ item.issue }}</span>
            <span v-if="item.location" class="rcp-item-loc">({{ item.location }})</span>
          </div>
        </div>
      </div>

      <!-- 6. 术语与全局风险 (Glossary & Risks - 若有) -->
      <div v-if="glossary.length || globalRisks.length" class="rsv-meta-row">
        <div v-if="glossary.length" class="rsv-glossary-card">
          <div class="rsv-block-title">
            <span class="rsv-title-icon">📖</span>
            <span class="rsv-title-text">业务术语表</span>
          </div>
          <div class="rsv-glossary-list">
            <div v-for="(g, gi) in glossary" :key="gi" class="rsv-glossary-item">
              <span class="glossary-term">{{ g.term }}</span>
              <span class="glossary-def">{{ g.definition }}</span>
            </div>
          </div>
        </div>

        <div v-if="globalRisks.length" class="rsv-global-risks-card">
          <div class="rsv-block-title">
            <span class="rsv-title-icon">🛡️</span>
            <span class="rsv-title-text">全局风险建议</span>
          </div>
          <ul class="rsv-global-risk-list">
            <li v-for="(gr, gri) in globalRisks" :key="gri">{{ gr }}</li>
          </ul>
        </div>
      </div>
    </template>
  </div>
</template>

<script>
import { load as yamlLoad } from 'js-yaml'

export default {
  name: 'RequirementSummaryViewer',
  props: {
    /** 原始 YAML 字符串或已解析的对象 */
    raw: {
      type: [String, Object, null],
      default: '',
    },
  },
  computed: {
    rawText() {
      return typeof this.raw === 'string' ? this.raw : (this.raw ? JSON.stringify(this.raw, null, 2) : '')
    },
    summaryData() {
      if (!this.raw) return null
      if (typeof this.raw === 'object' && !Array.isArray(this.raw)) {
        return this.raw
      }
      const text = String(this.raw || '').trim()
      if (!text) return null

      // 清洗掉首尾的 markdown 代码块包裹 (如 ```yaml ... ```)
      let clean = text
      const codeBlockMatch = text.match(/^```(?:yaml|yml)?\s*[\r\n]+([\s\S]*?)(?:[\r\n]+```|$)/i)
      if (codeBlockMatch) {
        clean = codeBlockMatch[1].trim()
      } else if (clean.startsWith('```') && clean.endsWith('```')) {
        clean = clean.replace(/^```[^\r\n]*[\r\n]?/, '').replace(/[\r\n]?```$/, '').trim()
      }

      try {
        const obj = yamlLoad(clean)
        if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
          // 确认是需求结构化模型（至少具有 modules 或 requirement_overview 或 version_goals 等关键字段）
          if (obj.modules || obj.requirement_overview || obj.version_goals || obj.business_flow) {
            return obj
          }
        }
      } catch (e) {
        // 解析失败，降级为 null
      }
      return null
    },
    versionGoals() {
      if (!this.summaryData || !Array.isArray(this.summaryData.version_goals)) return []
      return this.summaryData.version_goals.filter(Boolean)
    },
    modules() {
      if (!this.summaryData || !Array.isArray(this.summaryData.modules)) return []
      return this.summaryData.modules.filter(m => m && typeof m === 'object')
    },
    businessFlowSteps() {
      if (!this.summaryData || !this.summaryData.business_flow) return []
      const bf = this.summaryData.business_flow
      if (Array.isArray(bf)) {
        return bf.map(x => String(x).trim()).filter(Boolean)
      }
      if (typeof bf === 'string') {
        const lines = bf.split(/[\r\n]+/).map(s => s.trim()).filter(Boolean)
        return lines.map(line => {
          // 去掉开头的 "1. "、"1、"、"- "
          return line.replace(/^(\d+[\.、]\s*|-\s*)/, '').trim()
        }).filter(Boolean)
      }
      return []
    },
    decisionTables() {
      if (!this.summaryData || !Array.isArray(this.summaryData.decision_tables)) return []
      return this.summaryData.decision_tables.filter(dt => dt && typeof dt === 'object')
    },
    glossary() {
      if (!this.summaryData || !Array.isArray(this.summaryData.glossary)) return []
      return this.summaryData.glossary.filter(g => g && g.term)
    },
    globalRisks() {
      if (!this.summaryData || !Array.isArray(this.summaryData.risks)) return []
      return this.summaryData.risks.filter(Boolean)
    },
    ambiguousPoints() {
      if (!this.summaryData || !Array.isArray(this.summaryData.ambiguous_points)) return []
      return this.summaryData.ambiguous_points.filter(p => p && typeof p === 'object')
    },
    missingPoints() {
      if (!this.summaryData || !Array.isArray(this.summaryData.missing_points)) return []
      return this.summaryData.missing_points.filter(p => p && typeof p === 'object')
    },
    boundaryPoints() {
      if (!this.summaryData || !Array.isArray(this.summaryData.boundary_points)) return []
      return this.summaryData.boundary_points.filter(p => p && typeof p === 'object')
    },
    clarificationCount() {
      return this.ambiguousPoints.length + this.missingPoints.length + this.boundaryPoints.length
    },
    previewClarifyItems() {
      const items = []
      this.ambiguousPoints.slice(0, 3).forEach(p => {
        items.push({
          typeName: '歧义点',
          tagType: 'warning',
          issue: p.issue || p.suggested_question || '待确认歧义',
          location: p.location,
        })
      })
      this.missingPoints.slice(0, 3).forEach(p => {
        items.push({
          typeName: '缺失点',
          tagType: 'danger',
          issue: p.scenario || p.basis || '流程信息缺失',
          location: p.impact,
        })
      })
      this.boundaryPoints.slice(0, 2).forEach(p => {
        items.push({
          typeName: '边界点',
          tagType: 'info',
          issue: p.issue || '功能边界未定义',
          location: p.impact,
        })
      })
      return items
    },
  },
  methods: {
    prioTag(prio) {
      const p = String(prio || '').toUpperCase()
      if (p === 'P0') return 'danger'
      if (p === 'P1') return 'warning'
      return 'info'
    },
    changeTypeTag(ct) {
      const m = { '新增': 'success', '修改': 'primary', '优化': '', '删除': 'danger' }
      return m[ct] !== undefined ? m[ct] : 'info'
    },
    getFeatures(mod) {
      if (!mod || !Array.isArray(mod.key_features)) return []
      return mod.key_features.map(f => {
        if (typeof f === 'string') return f
        if (f && typeof f === 'object') {
          // 新格式 key_features 用 text 字段（并带 F-xxx 稳定编号）：
          // {'id':'M1-F001','text':'...','involves_api':true,...}
          // 老格式是 title/name。缺了 text 会导致功能点在前端显示为空。
          const body = f.text || f.name || f.title || ''
          if (!body) return ''
          const id = typeof f.id === 'string' ? f.id : ''
          const note = f.desc || f.description
          const prefix = id ? `${id} ` : ''
          const suffix = note ? ` (${note})` : (f.involves_api === true ? ' (涉及接口)' : '')
          return prefix + body + suffix
        }
        return String(f)
      }).filter(Boolean)
    },
    getImpacts(mod) {
      if (!mod || !Array.isArray(mod.cross_module_impact)) return []
      return mod.cross_module_impact.filter(Boolean)
    },
    getRisks(mod) {
      if (!mod || !Array.isArray(mod.module_risks)) return []
      return mod.module_risks.filter(Boolean)
    },
    getLocations(mod) {
      if (!mod || !Array.isArray(mod.source_locations)) return []
      return mod.source_locations.filter(Boolean)
    },
    getEvidence(mod) {
      if (!mod || !Array.isArray(mod.source_evidence)) return []
      return mod.source_evidence.filter(ev => ev && (ev.quote || ev.page))
    },
    hasEvidence(mod) {
      return this.getLocations(mod).length > 0 || this.getEvidence(mod).length > 0 || !!mod.priority_reason
    },
    getDecisionTableColumns(dt) {
      if (!dt || !dt.rows || !dt.rows.length) return []
      const keys = new Set()
      dt.rows.forEach(r => {
        if (r && typeof r === 'object') {
          Object.keys(r).forEach(k => keys.add(k))
        }
      })
      return Array.from(keys)
    },
  },
}
</script>

<style scoped lang="scss">
.req-summary-viewer {
  display: flex;
  flex-direction: column;
  gap: 16px;
  color: #303133;
}

/* 降级展示 */
.rsv-fallback {
  .rsv-fallback-alert {
    margin-bottom: 12px;
  }
}

/* 1. 顶部总览卡片 */
.rsv-overview-card {
  background: #f7faff;
  border: 1px solid #d9ecff;
  border-radius: 8px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.rsv-block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
  color: #1d2129;
  margin-bottom: 8px;

  .rsv-title-icon {
    font-size: 16px;
  }

  .rsv-count-badge {
    font-size: 12px;
    color: #409eff;
    font-weight: 500;
  }
}

.rsv-overview-text {
  font-size: 13.5px;
  line-height: 1.7;
  color: #303133;
  white-space: pre-line;
  background: #ffffff;
  padding: 10px 14px;
  border-radius: 6px;
  border: 1px solid #ebeef5;
}

.rsv-goals-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 8px;
}

.rsv-goal-pill {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  background: #ffffff;
  border: 1px solid #e4e7ed;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.5;
  color: #4e5969;

  .goal-index {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    background: #409eff;
    color: #ffffff;
    border-radius: 50%;
    font-size: 11px;
    font-weight: bold;
    flex-shrink: 0;
    margin-top: 2px;
  }

  .goal-text {
    flex: 1;
    word-break: break-word;
  }
}

/* 2. 模块列表与卡片 */
.rsv-modules-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.rsv-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;

  .rsv-section-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 15px;
    font-weight: 600;
    color: #1d2129;

    .rsv-count-tag {
      font-size: 12px;
      font-weight: normal;
      background: #f2f3f5;
      padding: 2px 8px;
      border-radius: 10px;
      color: #86909c;
    }
  }
}

.rsv-module-cards {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.rsv-module-card {
  background: #ffffff;
  border: 1px solid #e4e7ed;
  border-left-width: 4px;
  border-radius: 8px;
  padding: 16px 18px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.03);
  display: flex;
  flex-direction: column;
  gap: 12px;
  transition: all 0.2s ease;

  &:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
  }

  &.prio-border-p0 {
    border-left-color: #f56c6c;
  }
  &.prio-border-p1 {
    border-left-color: #e6a23c;
  }
  &.prio-border-p2 {
    border-left-color: #909399;
  }
}

.rmc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;

  .rmc-title-line {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;

    .rmc-name {
      font-size: 15px;
      font-weight: 600;
      color: #1d2129;
    }

    .rmc-prio-tag {
      font-weight: 600;
      font-size: 11px;
    }

    .rmc-change-tag {
      font-size: 11px;
    }

    .rmc-source-hint {
      font-size: 12px;
      color: #86909c;
    }
  }
}

.rmc-desc {
  font-size: 13px;
  line-height: 1.6;
  color: #4e5969;
  background: #f8fafc;
  padding: 8px 12px;
  border-radius: 4px;
}

.rmc-sub-block {
  display: flex;
  flex-direction: column;
  gap: 6px;

  .rmc-sub-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    font-weight: 600;
    color: #303133;

    &.text-warning {
      color: #e6a23c;
    }
    &.text-danger {
      color: #f56c6c;
    }

    .sub-icon {
      font-size: 14px;
    }
  }
}

.rmc-features-block {
  .rmc-feature-items {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-left: 4px;
  }

  .rmc-feature-item {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: 13px;
    line-height: 1.5;
    color: #303133;

    .feat-dot {
      color: #67c23a;
      font-weight: bold;
      font-size: 12px;
      flex-shrink: 0;
      margin-top: 1px;
    }

    .feat-content {
      flex: 1;
      word-break: break-word;
    }
  }
}

.rmc-impact-block {
  background: #fdfaf5;
  border: 1px solid #faecd8;
  border-radius: 6px;
  padding: 10px 14px;
}

.rmc-risk-block {
  background: #fef8f8;
  border: 1px solid #fde2e2;
  border-radius: 6px;
  padding: 10px 14px;
}

.rmc-bullet-list {
  margin: 0;
  padding-left: 20px;
  font-size: 12.5px;
  line-height: 1.6;

  &.warning-list {
    color: #b88230;
  }
  &.danger-list {
    color: #c45656;
  }

  li {
    margin-bottom: 2px;
    word-break: break-word;
  }
}

.rmc-collapse {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  overflow: hidden;

  :deep(.el-collapse-item__header) {
    background-color: #fafbfc;
    padding: 0 12px;
    font-size: 12.5px;
    color: #606266;
    height: 38px;
    line-height: 38px;
  }

  :deep(.el-collapse-item__content) {
    padding: 12px 14px;
    background-color: #ffffff;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .rmc-collapse-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12.5px;
  }
}

.rmc-locations-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;

  .row-label {
    color: #909399;
    flex-shrink: 0;
  }

  .location-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  .loc-tag {
    font-size: 11px;
  }
}

.rmc-quotes-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rmc-quote-item {
  font-size: 12px;
  line-height: 1.5;
  background: #f8fafc;
  padding: 6px 10px;
  border-radius: 4px;
  color: #606266;
  border-left: 3px solid #dcdfe6;

  .quote-page {
    font-weight: 600;
    color: #409eff;
    margin-right: 6px;
  }

  .quote-text {
    font-style: italic;
  }
}

.rmc-prio-reason {
  font-size: 12px;
  line-height: 1.5;
  color: #909399;
  background: #fbfbfb;
  padding: 6px 10px;
  border-radius: 4px;

  .reason-label {
    font-weight: 600;
    color: #606266;
  }
}

/* 3. 业务流程链路卡片 */
.rsv-flow-card {
  background: #fafcff;
  border: 1px solid #e1eaf5;
  border-radius: 8px;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.rsv-flow-steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rsv-flow-step-item {
  display: flex;
  align-items: center;
  gap: 10px;

  .step-badge {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: #409eff;
    color: #ffffff;
    font-weight: bold;
    font-size: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }

  .step-card {
    flex: 1;
    background: #ffffff;
    border: 1px solid #dcdfe6;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 13px;
    color: #303133;
    line-height: 1.5;
  }

  .step-arrow {
    font-size: 14px;
    color: #909399;
    flex-shrink: 0;
  }
}

/* 4. 决策表 */
.rsv-section-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rsv-dt-card {
  background: #ffffff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;

  .rsv-dt-head {
    font-size: 13.5px;
    color: #303133;

    .rsv-dt-mod {
      color: #909399;
      font-size: 12px;
      font-weight: normal;
    }
  }

  .rsv-dt-table {
    width: 100%;
  }
}

/* 5. 待澄清预警卡片 */
.rsv-clarify-preview-card {
  background: #fdf6ec;
  border: 1px solid #faecd8;
  border-radius: 8px;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;

  .rcp-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;

    .rcp-title-line {
      display: flex;
      align-items: center;
      gap: 6px;
      font-weight: 600;
      font-size: 13.5px;
      color: #e6a23c;
    }

    .rcp-tags {
      display: flex;
      gap: 6px;
    }
  }

  .rcp-items {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .rcp-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    color: #606266;
    background: #ffffff;
    padding: 6px 10px;
    border-radius: 4px;
    border: 1px solid #faecd8;

    .rcp-item-issue {
      flex: 1;
      word-break: break-word;
    }

    .rcp-item-loc {
      color: #909399;
      font-size: 11px;
    }
  }
}

/* 6. 术语与全局风险 */
.rsv-meta-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 12px;
}

.rsv-glossary-card,
.rsv-global-risks-card {
  background: #ffffff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.rsv-glossary-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rsv-glossary-item {
  font-size: 12.5px;
  line-height: 1.5;
  display: flex;
  gap: 8px;

  .glossary-term {
    font-weight: 600;
    color: #409eff;
    flex-shrink: 0;
  }

  .glossary-def {
    color: #606266;
    word-break: break-word;
  }
}

.rsv-global-risk-list {
  margin: 0;
  padding-left: 20px;
  font-size: 12.5px;
  line-height: 1.6;
  color: #606266;

  li {
    margin-bottom: 3px;
    word-break: break-word;
  }
}
</style>
