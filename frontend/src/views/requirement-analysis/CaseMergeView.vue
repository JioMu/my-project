<template>
  <div class="case-merge-view">
    <div class="page-header">
      <div class="header-left">
        <el-button size="small" plain @click="goBack">← 返回画布看板</el-button>
        <h2>📋 用例合并采纳</h2>
      </div>
      <div class="header-right">
        <span v-if="proto" class="proto-title">{{ proto.title || ('任务 #' + protoId) }}</span>
        <el-button size="small" :loading="loading" @click="loadDetail">刷新</el-button>
      </div>
    </div>

    <!-- 筛选器 -->
    <div class="filter-bar">
      <el-select v-model="folderFilter" placeholder="按文件夹筛选" clearable size="small" style="width: 200px">
        <el-option label="全部文件夹" value="" />
        <el-option v-for="fg in folderGroups" :key="fg.path" :label="fg.path || '未分组'" :value="fg.path" />
      </el-select>
      <el-select v-model="moduleFilter" placeholder="按模块筛选" clearable size="small" style="width: 180px">
        <el-option label="全部模块" value="" />
        <el-option v-for="m in moduleList" :key="m" :label="m" :value="m" />
      </el-select>
      <el-select v-model="priorityFilter" placeholder="按优先级筛选" clearable size="small" style="width: 140px">
        <el-option label="全部优先级" value="" />
        <el-option label="P0" value="P0" />
        <el-option label="P1" value="P1" />
        <el-option label="P2" value="P2" />
        <el-option label="P3" value="P3" />
      </el-select>
      <el-input v-model="keywordFilter" placeholder="搜索用例标题..." clearable size="small" style="width: 200px" />
      <el-checkbox v-model="showAdopted" size="small" class="show-adopted-toggle">显示已采纳</el-checkbox>
      <div class="filter-stats">
        共 {{ displayCases.length }} 条 / 已选 {{ selectedCases.length }} 条
        <template v-if="hiddenAdoptedCount">
          / 已采纳(已隐藏) <b>{{ hiddenAdoptedCount }}</b> 条
        </template>
        <template v-else-if="adoptedCount">
          / 其中已采纳 <b>{{ adoptedCount }}</b> 条
        </template>
      </div>
    </div>

    <!-- 批量操作 -->
    <div class="batch-actions">
      <el-checkbox v-model="selectAll" @change="onSelectAll">全选</el-checkbox>
      <el-button size="small" type="primary" :loading="adopting" :disabled="!selectedCases.length" @click="onAdoptSelected">
         采纳选中用例 ({{ selectedCases.length }})
      </el-button>
      <el-button size="small" type="success" :loading="adoptingAll" @click="onAdoptAll">
         采纳全部用例
      </el-button>
      <el-button size="small" type="warning" plain @click="openAdoptionDiff">
         🧠 AI 偏差对比 · 回流知识库
      </el-button>
      <el-button size="small" type="primary" plain @click="openCreate">
         ➕ 新增用例（人工补充）
      </el-button>
      <span class="batch-hint">
        单条 / 选中 / 全部均可采纳；若某条已入库，再次采纳会以最新内容覆盖更新（同项目+同模块+同标题视为同一条）。
      </span>
    </div>

    <!-- 用例列表 -->
    <el-table ref="table" :data="displayCases" size="small" border stripe max-height="600" @selection-change="onSelectionChange">
      <el-table-column type="selection" width="50" :selectable="isRowSelectable" />
      <el-table-column type="index" width="50" />
      <el-table-column label="关联项目" width="110" show-overflow-tooltip>
        <template #default="{ row }">{{ proto.project_name || '-' }}</template>
      </el-table-column>
      <el-table-column label="关联版本" width="100" align="center">
        <template #default="{ row }">
          <el-tag v-if="proto.version_name" size="mini">{{ proto.version_name }}</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column prop="module" label="功能模块" width="140" show-overflow-tooltip />
      <el-table-column label="测试点" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.testpoint || '-' }}</template>
      </el-table-column>
      <el-table-column label="用例标题" min-width="220">
        <template #default="{ row }">
          <span :class="{ 'row-adopted': adoptedKeys[row._key] }">{{ row.title || '（未命名）' }}</span>
          <el-tag v-if="row._manual" size="mini" type="warning" class="row-tag">人工补充</el-tag>
          <el-tag v-if="editedKeys[row._key]" size="mini" type="warning" class="row-tag">已编辑</el-tag>
          <el-tag v-if="adoptedKeys[row._key]" size="mini" type="success" class="row-tag">已采纳</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="priority" label="优先级" width="80" align="center">
        <template #default="{ row }">
          <el-tag :type="priorityType(row.priority)" size="mini">{{ row.priority || '-' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="type" label="类型" width="80" align="center" />
      <el-table-column label="步骤数" width="80" align="center">
        <template #default="{ row }">{{ row.stepsList.length }}</template>
      </el-table-column>
      <el-table-column label="来源画布" width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.sourceCanvas }}</template>
      </el-table-column>
      <el-table-column label="来源文件夹" width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.sourceFolder || '未分组' }}</template>
      </el-table-column>
      <el-table-column label="操作" width="210" align="center">
        <template #default="{ row }">
          <el-button size="mini" @click="openDetail(row, 'view')">详情</el-button>
          <el-button size="mini" type="warning" plain @click="openDetail(row, 'edit')">编辑</el-button>
          <el-button
            size="mini"
            type="success"
            :loading="!!adoptingKeys[row._key]"
            @click="onAdoptSingle(row)"
          >采纳</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <div class="empty-hint">
          <template v-if="hiddenAdoptedCount">
            当前筛选下的 <b>{{ hiddenAdoptedCount }}</b> 条用例已全部采纳并隐藏，避免重复采纳。
            <el-button link type="primary" size="small" @click="showAdopted = true">
              显示已采纳的用例
            </el-button>
          </template>
          <template v-else-if="!allCases.length">该原型还没有生成用例</template>
          <template v-else>当前筛选条件下没有用例</template>
        </div>
      </template>
    </el-table>

    <!-- 冒烟用例 -->
    <div v-if="filteredSmoke.length" class="smoke-section">
      <div class="smoke-header">
        <div class="smoke-header-left">
          <h4>🔥 冒烟用例 ({{ filteredSmoke.length }})</h4>
          <span class="smoke-header-tip">
            共 {{ filteredSmoke.length }} 条 / 待采纳 {{ displaySmoke.length }} 条
            <template v-if="selectedSmoke.length">/ 已选 {{ selectedSmoke.length }} 条</template>
          </span>
        </div>
        <div class="smoke-header-actions">
          <el-dropdown
            split-button
            type="primary"
            size="small"
            :loading="exportingSmoke"
            @click="exportZentaoSmoke('csv', 'utf-8')"
            @command="handleExportCommand"
          >
            📥 导出禅道 CSV (UTF-8)
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="csv-utf8">📥 导出禅道 CSV (UTF-8 编码) - 推荐</el-dropdown-item>
                <el-dropdown-item command="csv-gbk">📥 导出禅道 CSV (GBK 编码) - 适配GBK禅道</el-dropdown-item>
                <el-dropdown-item command="xlsx">📥 导出禅道 Excel (.xlsx)</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
      <el-table
        ref="smokeTable"
        :data="displaySmoke"
        size="small"
        border
        stripe
        max-height="400"
        @selection-change="onSmokeSelectionChange"
      >
        <el-table-column type="selection" width="50" />
        <el-table-column type="index" width="50" />
        <el-table-column label="关联项目" width="110" show-overflow-tooltip>
          <template #default="{ row }">{{ proto.project_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="关联版本" width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="proto.version_name" size="mini">{{ proto.version_name }}</el-tag>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="module" label="功能模块" width="140" show-overflow-tooltip />
        <el-table-column label="测试点" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.testpoint || '-' }}</template>
        </el-table-column>
        <el-table-column label="用例标题" min-width="240">
          <template #default="{ row }">
            <span :class="{ 'row-adopted': adoptedKeys[row._key] }">{{ row.title || '（未命名）' }}</span>
            <el-tag v-if="editedKeys[row._key]" size="mini" type="warning" class="row-tag">已编辑</el-tag>
            <el-tag v-if="adoptedKeys[row._key]" size="mini" type="success" class="row-tag">已采纳</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="priorityType(row.priority)" size="mini">{{ row.priority || '-' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="步骤数" width="80" align="center">
          <template #default="{ row }">{{ row.stepsList.length }}</template>
        </el-table-column>
        <el-table-column label="来源画布" width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.sourceCanvas }}</template>
        </el-table-column>
        <el-table-column label="来源文件夹" width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ row.sourceFolder || '未分组' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="210" align="center">
          <template #default="{ row }">
            <el-button size="mini" @click="openDetail(row, 'view')">详情</el-button>
            <el-button size="mini" type="warning" plain @click="openDetail(row, 'edit')">编辑</el-button>
            <el-button
              size="mini"
              type="success"
              :loading="!!adoptingKeys[row._key]"
              :disabled="!!adoptedKeys[row._key]"
              @click="onAdoptSingle(row)"
            >{{ adoptedKeys[row._key] ? '已采纳' : '采纳' }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-hint">
            <template v-if="hiddenAdoptedSmokeCount">
              当前筛选下的 <b>{{ hiddenAdoptedSmokeCount }}</b> 条冒烟用例已全部采纳并隐藏。
              <el-button link type="primary" size="small" @click="showAdopted = true">
                显示已采纳的用例
              </el-button>
            </template>
            <template v-else>当前筛选条件下没有冒烟用例</template>
          </div>
        </template>
      </el-table>
    </div>

    <!-- 详情 / 编辑弹窗 -->
    <el-dialog
      v-model="detailVisible"
      :title="detailMode === 'create' ? '➕ 新增用例（人工补充，确认后一并采纳）' : (detailMode === 'edit' ? '✏️ 编辑用例（可修改后直接采纳）' : '📄 用例详情（可直接修改后采纳）')"
      width="860px"
      top="5vh"
      :close-on-click-modal="false"
    >
      <div v-if="current" class="detail-source">
        <span>来源画布：<b>{{ current.sourceCanvas }}</b></span>
        <span>来源文件夹：<b>{{ current.sourceFolder || '未分组' }}</b></span>
        <span v-if="current.caseId">用例 ID：<b>{{ current.caseId }}</b></span>
        <span v-if="current._isSmoke">类型：<b>冒烟用例</b></span>
      </div>

      <el-form label-width="90px" size="small" class="detail-form">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="所属模块">
              <el-select
                v-model="form.module"
                filterable
                allow-create
                default-first-option
                placeholder="选择或输入模块名"
                size="small"
                style="width: 100%"
              >
                <el-option v-for="m in moduleOptions" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="优先级">
              <el-select v-model="form.priority" size="small" style="width: 100%">
                <el-option label="P0" value="P0" />
                <el-option label="P1" value="P1" />
                <el-option label="P2" value="P2" />
                <el-option label="P3" value="P3" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="用例类型" label-width="80px">
              <el-select v-model="form.type" filterable allow-create size="small" style="width: 100%">
                <el-option v-for="t in typeOptions" :key="t" :label="t" :value="t" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="用例标题">
          <el-input v-model="form.title" size="small" placeholder="[验证点] + [操作] + [结果]" />
        </el-form-item>

        <el-form-item label="前置条件">
          <el-input v-model="form.precondition" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" placeholder="账号状态、APP 状态、网络环境、数据准备…" />
        </el-form-item>

        <el-form-item label="操作步骤">
          <div class="steps-editor">
            <div v-for="(s, i) in form.steps" :key="i" class="step-row">
              <span class="step-no">{{ i + 1 }}</span>
              <el-input
                v-model="form.steps[i]"
                type="textarea"
                :autosize="{ minRows: 1, maxRows: 4 }"
                size="small"
                class="step-input"
              />
              <div class="step-ops">
                <el-button text size="small" :disabled="i === 0" @click="moveStep(i, -1)">↑</el-button>
                <el-button text size="small" :disabled="i === form.steps.length - 1" @click="moveStep(i, 1)">↓</el-button>
                <el-button text size="small" type="danger" @click="removeStep(i)">✕</el-button>
              </div>
            </div>
            <el-button size="small" plain class="add-step" @click="addStep">+ 添加步骤</el-button>
          </div>
        </el-form-item>

        <el-form-item label="预期结果">
          <el-input v-model="form.expected" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" placeholder="界面变化、数据变化、提示信息…" />
        </el-form-item>

        <el-form-item label="备注">
          <el-input v-model="form.notes" type="textarea" :autosize="{ minRows: 1, maxRows: 4 }" placeholder="测试要点、常见问题…" />
        </el-form-item>
      </el-form>

      <template #footer>
        <div class="dialog-footer">
          <span class="footer-hint">
            {{ detailMode === 'create'
              ? '「加入列表」仅暂存为人工补充，可与其他用例一起勾选后统一采纳；「新增并采纳」立即写入用例库'
              : '「保存并采纳」会按当前内容写入用例库（同项目+同模块+同标题去重）' }}
          </span>
          <div>
            <el-button size="small" @click="detailVisible = false">取消</el-button>
            <el-button size="small" :loading="saving" @click="saveEdit">
              {{ detailMode === 'create' ? '加入列表' : '保存' }}
            </el-button>
            <el-button
              size="small"
              type="primary"
              :loading="savingAdopt"
              @click="saveAndAdopt"
            >{{ detailMode === 'create' ? '新增并采纳' : '保存并采纳' }}</el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- AI 偏差对比：只读预览 + 人工勾选后才回流知识库 -->
    <el-dialog v-model="diffVisible" title="🧠 AI 偏差对比（人工确认后回流知识库）" width="860px" top="6vh">
      <div v-loading="diffLoading" class="diff-wrap">
        <div v-if="diffData && diffData.skipped" class="diff-empty">
          {{ diffData.reason || '暂无可对比的数据（需先完成 AI 生成与人工采纳）' }}
        </div>
        <template v-else-if="diffData">
          <el-alert
            type="info"
            show-icon
            :closable="false"
            :title="`AI 原始 ${diffData.diff.ai_n} 条 / 人工采纳后 ${diffData.diff.merged_n} 条`"
            :description="`人工补充 ${diffData.diff.missing} 条（其中人工标记 ${diffData.diff.manual_n || 0} 条）· 人工改写 ${diffData.diff.rewritten} 条 · 人工判定无效（未采纳）${diffData.diff.redundant} 条`"
          />
          <div class="diff-tip">
            已按模块整理为 {{ (diffEdit || []).length }} 条候选知识：人工补充合成「必须覆盖清单」、人工改写逐条保留、未采纳合成「勿再生成」负面清单。
            <b>可直接修改标题与正文、删除或取消勾选不需要的条目</b>，确认后才写入项目知识库。
          </div>
          <div class="diff-list">
            <div v-for="(it, idx) in diffEdit" :key="idx" class="diff-item">
              <div class="diff-head">
                <el-checkbox v-model="it.checked" />
                <el-tag size="mini" :type="diffTagType(it.ref.type)">
                  {{ diffTagText(it.ref.type) }}
                </el-tag>
                <el-tag v-if="it.manual" size="mini" type="success">人工标记</el-tag>
                <el-input v-model="it.title" size="mini" class="diff-title-input" placeholder="知识条目标题" />
                <el-button size="mini" type="text" @click="removeDiffItem(idx)">删除</el-button>
              </div>
              <el-input
                v-model="it.content"
                type="textarea"
                :rows="6"
                size="mini"
                class="diff-content-input"
                placeholder="知识条目正文"
              />
            </div>
            <div v-if="!(diffEdit || []).length" class="diff-empty">本轮没有可沉淀的偏差</div>
          </div>
        </template>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <span class="footer-hint">确认后写入项目知识库并被后续 AI 生成召回（未点确认不会写入）</span>
          <div>
            <el-button size="small" :loading="diffLoading" @click="openAdoptionDiff">重新对比</el-button>
            <el-button size="small" @click="diffVisible = false">取消</el-button>
            <el-button
              size="small"
              type="primary"
              :loading="backfilling"
              :disabled="!checkedCount"
              @click="submitBackfill"
            >确认写入 {{ checkedCount }} 条</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script>
import request from '@/utils/api'

const TYPE_OPTIONS = ['功能', '边界', '异常', 'UI', '兼容']

export default {
  name: 'CaseMergeView',
  data() {
    return {
      protoId: this.$route.params.id,
      proto: null,
      pages: [],
      loading: false,
      adopting: false,
      adoptingAll: false,
      folderFilter: '',
      moduleFilter: '',
      priorityFilter: '',
      keywordFilter: '',
      selectAll: false,
      selectedCases: [],
      // 详情 / 编辑
      detailVisible: false,
      detailMode: 'view',
      current: null,
      form: { module: '', title: '', priority: 'P2', type: '功能', precondition: '', steps: [], expected: '', notes: '' },
      saving: false,
      savingAdopt: false,
      // 人工新增用例（人工补充）：暂存后随「采纳选中」一并入库
      manualCases: [],
      manualSeq: 0,
      // 已采纳用例默认隐藏；勾选后一并展示（行仍不可再次勾选，按钮显示「已采纳」）
      showAdopted: false,
      // 行状态：key = `${pageId}#${index}`（冒烟加 smoke# 前缀）
      adoptedKeys: {},
      editedKeys: {},
      adoptingKeys: {},
      // AI 偏差对比（只读预览 → 人工整理/改写 → 确认后才回流知识库）
      diffVisible: false,
      diffLoading: false,
      diffData: null,
      diffEdit: [],
      backfilling: false,
      typeOptions: TYPE_OPTIONS,
      exportingSmoke: false,
      selectedSmoke: [],
    }
  },
  computed: {
    checkedCount() {
      return (this.diffEdit || []).filter(x => x.checked && x.title && x.content).length
    },
    folderGroups() {
      const map = new Map()
      for (const p of this.pages) {
        const folder = (p.folder || '').trim()
        if (!map.has(folder)) {
          map.set(folder, { path: folder, count: 0 })
        }
        map.get(folder).count++
      }
      return Array.from(map.values()).sort((a, b) => (a.path || '').localeCompare(b.path || ''))
    },
    allCases() {
      const out = []
      for (const p of this.pages) {
        const source = p.page_name || `P${p.page_no}`
        const folder = (p.folder || '').trim()
        this.parseArr(p.page_testcases_json).forEach((tc, idx) => {
          const inner = tc.testcase || tc
          out.push(this.normalizeCase(inner, tc, {
            key: `${p.id}#${idx}`,
            pageId: p.id,
            pageIndex: idx,
            sourceCanvas: source,
            sourceFolder: folder,
            isSmoke: false,
          }))
        })
      }
      // 人工补充用例排在最后，便于一起勾选采纳
      return out.concat(this.manualCases)
    },
    allSmoke() {
      const out = []
      for (const p of this.pages) {
        const source = p.page_name || `P${p.page_no}`
        const folder = (p.folder || '').trim()
        this.parseSmoke(p.page_smoke_json).forEach((sc, idx) => {
          const inner = sc.testcase || sc
          out.push(this.normalizeCase(inner, sc, {
            key: `smoke#${p.id}#${idx}`,
            pageId: p.id,
            pageIndex: idx,
            sourceCanvas: source,
            sourceFolder: folder,
            isSmoke: true,
          }))
        })
      }
      return out
    },
    moduleList() {
      const set = new Set()
      for (const c of this.allCases) {
        if (c.module) set.add(c.module)
      }
      return Array.from(set).sort()
    },
    moduleOptions() {
      const set = new Set(this.moduleList)
      if (this.form.module) set.add(this.form.module)
      return Array.from(set).sort()
    },
    filteredCases() {
      return this.allCases.filter(c => {
        if (this.folderFilter && c.sourceFolder !== this.folderFilter) return false
        if (this.moduleFilter && c.module !== this.moduleFilter) return false
        if (this.priorityFilter && c.priority !== this.priorityFilter) return false
        if (this.keywordFilter && !c.title.toLowerCase().includes(this.keywordFilter.toLowerCase())) return false
        return true
      })
    },
    filteredSmoke() {
      return this.allSmoke.filter(c => {
        if (this.folderFilter && c.sourceFolder !== this.folderFilter) return false
        if (this.moduleFilter && c.module !== this.moduleFilter) return false
        return true
      })
    },
    /** 已采纳（已入库）的用例默认隐藏，避免重复采纳；勾选「显示已采纳」后仍可查看 */
    displayCases() {
      return this.filteredCases.filter(
        c => this.showAdopted || !this.adoptedKeys[c._key])
    },
    displaySmoke() {
      return this.filteredSmoke.filter(
        c => this.showAdopted || !this.adoptedKeys[c._key])
    },
    adoptedCount() {
      // 仅统计当前普通用例中已采纳（已入库）的数量，排除冒烟用例 key
      return this.filteredCases.filter(c => this.adoptedKeys[c._key]).length
    },
    /** 当前被隐藏的已采纳条数（勾选「显示已采纳」后为 0） */
    hiddenAdoptedCount() {
      return this.showAdopted ? 0 : this.adoptedCount
    },
    hiddenAdoptedSmokeCount() {
      return this.showAdopted ? 0
        : this.filteredSmoke.filter(c => this.adoptedKeys[c._key]).length
    },
    currentKey() {
      return this.current ? this.current._key : ''
    },
  },
  mounted() {
    this.loadDetail()
  },
  methods: {
    base() {
      return '/requirement-analysis/api/modao'
    },
    priorityType(p) {
      const map = { 'P0': 'danger', 'P1': 'warning', 'P2': '', 'P3': 'info' }
      return map[p] || ''
    },
    parseArr(s) {
      if (!s) return []
      if (Array.isArray(s)) return s
      try {
        const v = JSON.parse(s)
        if (Array.isArray(v)) return v
        return Array.isArray(v.data) ? v.data : []
      } catch (e) {
        return []
      }
    },
    /** 冒烟 JSON 顶层是对象（{smoke_testcases: [...]}），需单独解析 */
    parseSmoke(s) {
      if (!s) return []
      if (Array.isArray(s)) return s
      try {
        const v = JSON.parse(s)
        if (Array.isArray(v)) return v
        if (v && typeof v === 'object') {
          for (const k of ['smoke_testcases', 'smoke_cases', 'smoke_list', 'testcases']) {
            if (Array.isArray(v[k])) return v[k]
          }
        }
        return []
      } catch (e) {
        return []
      }
    },
    toStepsList(steps) {
      if (typeof steps === 'string') {
        return steps.split(/\r?\n/).map(x => x.replace(/^\s*\d+[.、)]\s*/, '').trim()).filter(Boolean)
      }
      if (!Array.isArray(steps)) return []
      return steps.map(s => {
        if (s && typeof s === 'object') return String(s.content || s.action || s.step || '').trim()
        return String(s == null ? '' : s).trim()
      }).filter(Boolean)
    },
    normalizeCase(inner, wrapper, meta) {
      const d = (inner && typeof inner === 'object') ? inner : { title: String(inner || '') }
      const w = (wrapper && typeof wrapper === 'object') ? wrapper : {}
      return {
        _key: meta.key,
        _pageId: meta.pageId,
        _pageIndex: meta.pageIndex,
        _isSmoke: meta.isSmoke,
        caseId: d.id || w.id || '',
        module: w.module || d.module || d.feature_module || '未分组',
        title: d.title || d.name || d.case_name || '',
        priority: d.priority || w.priority || 'P2',
        type: d.type || w.type || '功能',
        precondition: d.precondition || d.preconditions || d.pre_condition || '',
        stepsList: this.toStepsList(d.steps || d.test_steps || d.testSteps),
        expected: d.expected || d.pass_criteria || d.expected_result || '',
        notes: d.notes || d.requirement_ref || '',
        testpoint: d.testpoint || d.test_point || d.source_testpoint || '',
        requirementRef: d.requirement_ref || '',
        sourceCanvas: meta.sourceCanvas,
        sourceFolder: meta.sourceFolder,
        sourcePageId: meta.pageId,
      }
    },
    async loadDetail() {
      this.loading = true
      try {
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
        this.proto = res.data
        this.pages = (res.data.pages || []).slice().sort((a, b) => (a.page_no || 0) - (b.page_no || 0))
        // 预填已采纳标记（后端按 项目+标题+模块 判定已入库用例），刷新后仍可隐藏已采纳行
        const keys = {}
        for (const k of (res.data.adopted_keys || [])) keys[k] = true
        this.adoptedKeys = keys
        this.selectedCases = []
      } catch (e) {
        this.$message.error('加载失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.loading = false
      }
    },
    onSelectionChange(selection) {
      this.selectedCases = selection
      this.selectAll = selection.length === this.filteredCases.length && this.filteredCases.length > 0
    },
    onSelectAll() {
      // 全选/取消全选通过表格的 toggleAllSelection 实现
      this.$refs.table && this.$refs.table.toggleAllSelection()
    },
    isRowSelectable(row) {
      return !this.adoptedKeys[row._key]
    },
    // ---------- 详情 / 编辑 ----------
    openDetail(row, mode) {
      this.current = row
      this.detailMode = mode || 'view'
      const edited = this.editedKeys[row._key]
      const src = edited || {
        module: row.module,
        title: row.title,
        priority: row.priority,
        type: row.type,
        precondition: row.precondition,
        steps: [...row.stepsList],
        expected: row.expected,
        notes: row.notes,
      }
      this.form = {
        module: src.module || '',
        title: src.title || '',
        priority: src.priority || 'P2',
        type: src.type || '功能',
        precondition: src.precondition || '',
        steps: (src.steps || []).length ? [...src.steps] : [''],
        expected: src.expected || '',
        notes: src.notes || '',
      }
      this.detailVisible = true
    },
    addStep() {
      this.form.steps.push('')
    },
    removeStep(i) {
      this.form.steps.splice(i, 1)
      if (!this.form.steps.length) this.form.steps.push('')
    },
    moveStep(i, delta) {
      const j = i + delta
      if (j < 0 || j >= this.form.steps.length) return
      const arr = this.form.steps
      const tmp = arr[i]
      arr[i] = arr[j]
      arr[j] = tmp
    },
    /** 收集弹窗表单内容（过滤空步骤） */
    collectForm() {
      const steps = this.form.steps.map(s => String(s || '').trim()).filter(Boolean)
      return {
        module: (this.form.module || '').trim() || '未分组',
        title: (this.form.title || '').trim(),
        priority: this.form.priority || 'P2',
        type: this.form.type || '功能',
        precondition: this.form.precondition || '',
        steps,
        expected: this.form.expected || '',
        notes: this.form.notes || '',
      }
    },
    // ---------- 人工补充（新增用例） ----------
    openCreate() {
      this.current = null
      this.detailMode = 'create'
      this.form = {
        module: this.moduleFilter || '',
        title: '',
        priority: 'P2',
        type: '功能',
        precondition: '',
        steps: [''],
        expected: '',
        notes: '',
      }
      this.detailVisible = true
    },
    /** 把表单内容包装成一条「人工补充」行，加入列表待采纳（尚未入库） */
    addManualCase(payload) {
      this.manualSeq += 1
      const row = {
        _key: `manual#${this.manualSeq}`,
        _manual: true,
        _pageId: null,
        _pageIndex: null,
        _isSmoke: false,
        caseId: '',
        module: payload.module,
        title: payload.title,
        priority: payload.priority,
        type: payload.type,
        precondition: payload.precondition,
        stepsList: payload.steps,
        expected: payload.expected,
        notes: payload.notes,
        testpoint: '',
        requirementRef: '',
        sourceCanvas: '人工补充',
        sourceFolder: '人工补充',
      }
      this.manualCases = this.manualCases.concat([row])
      return row
    },
    saveEdit() {
      const payload = this.collectForm()
      if (!payload.title) {
        this.$message.warning('用例标题不能为空')
        return
      }
      // 新增模式：「保存」= 暂存为人工补充行，等人工确认后统一采纳
      if (this.detailMode === 'create') {
        this.addManualCase(payload)
        this.$message.success('已加入列表（人工补充，尚未入库）')
        this.detailVisible = false
        return
      }
      if (!this.current) return
      this.editedKeys = { ...this.editedKeys, [this.current._key]: payload }
      this.$message.success('已保存修改（尚未采纳）')
    },
    async saveAndAdopt() {
      const payload = this.collectForm()
      if (!payload.title) {
        this.$message.warning('用例标题不能为空')
        return
      }
      this.savingAdopt = true
      try {
        // 新增模式：立即把这条人工补充写入用例库（并并入汇总）
        if (this.detailMode === 'create') {
          const row = this.addManualCase(payload)
          await this.postAdoptManual(row)
          this.detailVisible = false
          return
        }
        if (!this.current) return
        const key = this.current._key
        this.editedKeys = { ...this.editedKeys, [key]: payload }
        await this.postAdoptSingle(this.current)
        this.detailVisible = false
      } finally {
        this.savingAdopt = false
      }
    },
    /** 采纳人工补充用例：走 /adopt/ 的 cases（不带 _ref，后端据此识别为人工新增） */
    async postAdoptManual(row) {
      const payload = this.payloadForSubmit(row)
      this.adoptingKeys = { ...this.adoptingKeys, [row._key]: true }
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/adopt/`,
          method: 'post',
          data: { cases: [{ ...payload, _manual: true }] },
        })
        const d = res.data || {}
        this.adoptedKeys = { ...this.adoptedKeys, [row._key]: true }
        this.$message.success(d.detail || '人工补充用例已入库')
        await this.loadDetail()
        // 人工补充会改变采纳结果 → 偏差对比需立即重算（弹窗开着时自动刷新）
        if (this.diffVisible) this.openAdoptionDiff()
        return true
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
        return false
      } finally {
        const m = { ...this.adoptingKeys }
        delete m[row._key]
        this.adoptingKeys = m
      }
    },
    // ---------- 采纳 ----------
    /** 提交用 payload：
     *  - 普通用例未编辑 → 返回 null，后端按 {page_id, page_index} 从原始 JSON 取，避免字段丢失
     *  - 已编辑 or 冒烟用例（冒烟不在普通用例索引里，无法用 ref 定位）→ 返回完整内容
     */
    payloadForSubmit(row) {
      const e = this.editedKeys[row._key]
      // 人工补充行没有编辑缓存，但必须带完整内容提交（否则会退化成按 ref 定位、定位不到）
      if (!e && !row._isSmoke && !row._manual) return null
      const src = e || {
        module: row.module,
        title: row.title,
        priority: row.priority,
        type: row.type,
        precondition: row.precondition,
        steps: [...row.stepsList],
        expected: row.expected,
        notes: row.notes,
      }
      return {
        id: row.caseId,
        module: src.module,
        title: src.title,
        priority: src.priority,
        type: src.type,
        precondition: src.precondition,
        steps: src.steps,
        expected: src.expected,
        notes: src.notes,
        requirement_ref: row.requirementRef || '',
        is_smoke: !!row._isSmoke,
      }
    },
    caseRefOf(row) {
      // 冒烟用例不在普通用例索引中，不做 ref 定位（否则会错误命中同页普通用例）
      if (row._isSmoke) return null
      // 人工新增用例没有画布来源：必须返回 null，让后端识别为「人工补充」而非编辑某条 AI 用例
      if (row._pageId == null || row._pageIndex == null) return null
      return { page_id: row._pageId, page_index: row._pageIndex }
    },
    async postAdoptSingle(row) {
      const payload = this.payloadForSubmit(row)
      const body = { is_smoke: !!row._isSmoke }
      if (row._pageId != null) body.source_page_id = row._pageId
      const ref = this.caseRefOf(row)
      if (ref) body.case_ref = ref
      if (payload) body.case = payload
      this.adoptingKeys = { ...this.adoptingKeys, [row._key]: true }
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/adopt-single/`,
          method: 'post',
          data: body,
        })
        const d = res.data || {}
        this.adoptedKeys = { ...this.adoptedKeys, [row._key]: true }
        this.$message.success(d.detail || `已采纳「${row.title}」`)
        return true
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
        return false
      } finally {
        const m = { ...this.adoptingKeys }
        delete m[row._key]
        this.adoptingKeys = m
      }
    },
    async onAdoptSingle(row) {
      const ok = await this.postAdoptSingle(row)
      if (ok) this.selectedCases = this.selectedCases.filter(c => c._key !== row._key)
    },
    async onAdoptSelected() {
      if (!this.selectedCases.length) {
        this.$message.warning('请至少选择一个用例')
        return
      }
      this.adopting = true
      try {
        const cases = []
        const refs = []
        for (const row of this.selectedCases) {
          const payload = this.payloadForSubmit(row)
          // source_page_id：来源画布 id（含冒烟用例——其 _ref 被故意置空防误命中，
          // 后端采纳时用它把「画布:P{n} {name} / 文件夹:{folder}」写入用例 tags 供溯源筛选）
          const srcPageId = row._pageId != null ? row._pageId : undefined
          if (payload) {
            cases.push({ ...payload, _ref: this.caseRefOf(row) || undefined, source_page_id: srcPageId })
          } else {
            refs.push(this.caseRefOf(row))
          }
        }
        const res = await request({
          url: `${this.base()}/${this.protoId}/adopt/`,
          method: 'post',
          data: { cases, case_refs: refs },
        })
        const d = res.data || {}
        for (const row of this.selectedCases) {
          this.adoptedKeys = { ...this.adoptedKeys, [row._key]: true }
        }
        let msg = `已采纳 ${d.testcases || 0} 条用例`
        if (d.updated) msg += `，更新 ${d.updated} 条`
        if (d.skipped) msg += `，一致跳过 ${d.skipped} 条`
        if (d.failed) msg += `，失败 ${d.failed} 条`
        if (d.modules) msg += `，新建模块 ${d.modules} 个`
        this.$message.success(msg)
        this.selectedCases = []
        this.selectAll = false
        this.loadDetail()
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.adopting = false
      }
    },
    async onAdoptAll() {
      this.adoptingAll = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/adopt/`,
          method: 'post',
          data: { adopt_all: true },
        })
        const d = res.data || {}
        let msg = `已采纳 ${d.testcases || 0} 条用例`
        if (d.updated) msg += `，更新 ${d.updated} 条`
        if (d.skipped) msg += `，一致跳过 ${d.skipped} 条`
        if (d.modules) msg += `，新建模块 ${d.modules} 个`
        this.$message.success(msg)
        await this.loadDetail()
      if (this.diffVisible) this.openAdoptionDiff()
    } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.adoptingAll = false
      }
    },
    async openAdoptionDiff() {
      this.diffVisible = true
      this.diffLoading = true
      this.diffData = null
      this.diffEdit = []
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/adoption-diff/`,
          method: 'get',
        })
        const d = res.data || {}
        this.diffData = d
        // 后端已按模块聚合整理；这里转成可编辑副本，人工改完再确认写入
        this.diffEdit = (d.items || []).map(it => ({
          checked: it.default_checked !== false,
          title: it.title || '',
          content: it.content || '',
          category: it.category || 'test_focus',
          ref: it.ref,
          manual: !!it.manual,
        }))
      } catch (e) {
        this.$message.error('偏差对比失败：' + (e.response?.data?.detail || e.message))
        this.diffVisible = false
      } finally {
        this.diffLoading = false
      }
    },
    diffTagType(type) {
      return { missing: 'danger', rewritten: 'warning', redundant: 'info' }[type] || 'info'
    },
    diffTagText(type) {
      return {
        missing: '人工补充 → 测试重点',
        rewritten: '人工改写 → 注意事项',
        redundant: '人工判定无效 → 勿再生成',
      }[type] || type
    },
    removeDiffItem(idx) {
      this.diffEdit = (this.diffEdit || []).filter((_, i) => i !== idx)
    },
    async submitBackfill() {
      // 以人工整理后的文案为准提交（后端仍会按 ref 校验来源，防止写入无关内容）
      const items = (this.diffEdit || [])
        .filter(x => x.checked && (x.title || '').trim() && (x.content || '').trim())
        .map(x => ({
          title: x.title,
          content: x.content,
          category: x.category,
          ref: x.ref,
        }))
      if (!items.length) {
        this.$message.warning('没有勾选任何条目')
        return
      }
      this.backfilling = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/backfill-kb/`,
          method: 'post',
          data: { items },
        })
        const d = res.data || {}
        this.$message.success(d.detail || `已沉淀 ${d.created_count || 0} 条知识`)
        this.diffVisible = false
      } catch (e) {
        this.$message.error('回流失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.backfilling = false
      }
    },
    onSmokeSelectionChange(selection) {
      this.selectedSmoke = selection || []
    },
    handleExportCommand(cmd) {
      if (cmd === 'csv-utf8') this.exportZentaoSmoke('csv', 'utf-8')
      else if (cmd === 'csv-gbk') this.exportZentaoSmoke('csv', 'gbk')
      else if (cmd === 'xlsx') this.exportZentaoSmoke('xlsx', 'utf-8')
    },
    async exportZentaoSmoke(format = 'csv', encode = 'utf-8') {
      const targetList = this.selectedSmoke.length > 0 ? this.selectedSmoke : this.filteredSmoke
      if (!targetList || targetList.length === 0) {
        this.$message.warning('当前没有可导出的冒烟用例')
        return
      }

      this.exportingSmoke = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/export-smoke-zentao/`,
          method: 'post',
          data: {
            cases: targetList,
            format: format,
            encode: encode,
          },
          responseType: 'blob'
        })

        const blob = res.data
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        const rawTitle = (this.proto && this.proto.title) || (`任务#${this.protoId}`)
        const safeTitle = rawTitle.replace(/[\\/:*?"<>|]/g, '_')
        const ext = format === 'xlsx' ? 'xlsx' : 'csv'
        const encLabel = format === 'xlsx' ? '' : (encode === 'gbk' ? '_GBK' : '_UTF8')
        const fileName = `${safeTitle}_冒烟用例_禅道${encLabel}.${ext}`

        a.href = url
        a.download = fileName
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        window.URL.revokeObjectURL(url)

        if (format === 'csv') {
          const encUpper = encode.toUpperCase()
          this.$alert(
            `<div style="line-height: 1.8; font-size: 14px;">
              已成功下载文件：<b style="color: #409EFF;">${fileName}</b><br><br>
              <div style="background: #fdf6ec; border: 1px solid #faecd8; padding: 10px 14px; border-radius: 6px; color: #e6a23c; margin-bottom: 12px;">
                ⚠️ <b>导入无数据排查说明：</b><br>
                禅道「导入CSV」页面的【编码】下拉选项，<b>必须与当前文件编码完全一致</b>（否则禅道解析不到数据会提示无数据）！
              </div>
              <b>📌 禅道导入操作步骤：</b><br>
              1. 打开禅道系统：进入 <b>「测试」-&gt;「用例」-&gt; 点击右上角「...」或「导入」-&gt;「导入CSV」</b>；<br>
              2. <b>【关键】</b>在导入页面的<b>【编码】</b>下拉框中，请选择 <b>「${encUpper}」</b>；<br>
              3. 点击【选择文件】上传 <code>${fileName}</code>，点击保存即可顺利入库！
            </div>`,
            '禅道 CSV 导入操作指引',
            { dangerouslyUseHTMLString: true, type: 'success', confirmButtonText: '我知道了' }
          )
        } else {
          this.$message.success(`已成功导出 ${targetList.length} 条冒烟用例（Excel 格式）`)
        }
      } catch (err) {
        console.error('导出禅道用例失败:', err)
        this.$message.error('导出失败: ' + (err.response?.data?.detail || err.message || err))
      } finally {
        this.exportingSmoke = false
      }
    },
    goBack() {
      this.$router.push(`/ai-generation/modao-board/${this.protoId}`)
    },
  },
}
</script>

<style scoped lang="scss">
.case-merge-view {
  padding: 16px 20px 40px;

  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    .header-left {
      display: flex;
      align-items: center;
      gap: 12px;
      h2 { margin: 0; font-size: 18px; }
    }
    .proto-title { color: #606266; font-size: 13px; }
  }

  .filter-bar {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-bottom: 16px;
    padding: 12px 16px;
    background: #f5f7fa;
    border-radius: 8px;
    flex-wrap: wrap;
    .filter-stats { margin-left: auto; color: #606266; font-size: 13px; }
    .show-adopted-toggle { margin-left: 8px; }
  }

  .empty-hint {
    padding: 18px 0;
    color: #909399;
    font-size: 13px;
    line-height: 1.9;
    b { color: #e6a23c; }
  }

  .batch-actions {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-bottom: 16px;
    flex-wrap: wrap;
    .batch-hint { color: #909399; font-size: 12px; }
  }

  .row-adopted { color: #909399; text-decoration: line-through; }
  .row-tag { margin-left: 6px; }

  .smoke-section {
    margin-top: 24px;
    .smoke-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      .smoke-header-left {
        display: flex;
        align-items: center;
        gap: 12px;
        h4 { margin: 0; color: #303133; }
        .smoke-header-tip { color: #909399; font-size: 13px; }
      }
      .smoke-header-actions {
        display: flex;
        align-items: center;
        gap: 8px;
      }
    }
  }

  .detail-source {
    display: flex;
    gap: 20px;
    flex-wrap: wrap;
    margin-bottom: 14px;
    padding: 8px 12px;
    background: #f5f7fa;
    border-radius: 6px;
    font-size: 12px;
    color: #606266;
    b { color: #303133; }
  }

  .detail-form {
    :deep(.el-form-item) { margin-bottom: 14px; }
  }

  .steps-editor {
    width: 100%;
    .step-row {
      display: flex;
      align-items: flex-start;
      gap: 6px;
      margin-bottom: 6px;
      .step-no {
        width: 22px;
        line-height: 28px;
        text-align: right;
        color: #909399;
        font-size: 12px;
        flex: none;
      }
      .step-input { flex: 1; }
      .step-ops {
        flex: none;
        display: flex;
        align-items: center;
        :deep(.el-button) { padding: 4px; margin-left: 0; }
      }
    }
    .add-step { margin-left: 28px; }
  }

  .dialog-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    .footer-hint { color: #909399; font-size: 12px; text-align: left; }
  }

  .diff-wrap {
    min-height: 120px;
    .diff-list {
      margin-top: 12px;
      max-height: 46vh;
      overflow: auto;
    }
    .diff-item {
      padding: 10px 12px;
      margin-bottom: 8px;
      border: 1px solid #ebeef5;
      border-radius: 6px;
      background: #fafafa;
    }
    .diff-tip {
      margin-top: 10px;
      padding: 8px 10px;
      background: #f4f4f5;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.7;
      color: #606266;
    }
    .diff-head {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .diff-title-input { flex: 1; }
    .diff-content-input { margin-top: 8px; }
    .diff-content {
      margin: 8px 0 0;
      padding: 8px 10px;
      background: #fff;
      border-radius: 4px;
      font-size: 12px;
      line-height: 1.7;
      color: #606266;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .diff-empty {
      padding: 24px 0;
      text-align: center;
      color: #909399;
      font-size: 13px;
    }
  }
}
</style>
