<template>
  <div class="canvas-board">
    <div class="page-header">
      <div class="header-left">
        <el-button size="small" plain @click="goBack">← 返回任务列表</el-button>
        <h2>📂 画布任务看板</h2>
      </div>
      <div class="header-right">
        <span v-if="proto" class="proto-title">{{ proto.title || ('原型 #' + protoId) }}</span>
        <el-button size="small" type="primary" plain @click="goToPipeline">📊 主流水线</el-button>
        <el-button size="small" type="success" plain @click="goToCaseMerge">🧪 用例集合</el-button>
        <el-button size="small" @click="loadDetail" :loading="loading">刷新</el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="board-tabs">
      <!-- ============ Tab1：画布任务看板（按画布独立分支） ============ -->
      <el-tab-pane label="画布任务看板" name="board">
        <!-- 文件夹筛选与全局控制条 -->
        <div class="folder-filter-bar" v-if="folderGroups.length">
          <div class="filter-controls">
            <el-select v-model="folderFilter" placeholder="按文件夹筛选" clearable size="small" style="width: 220px">
              <el-option label="全部文件夹" value="" />
              <el-option v-for="fg in folderGroups" :key="fg.path" :label="fg.path || '未分组'" :value="fg.path" />
            </el-select>
            <el-checkbox
              v-model="globalAllSelected"
              :indeterminate="globalIndeterminate"
              @change="onGlobalSelectAllChange"
              style="margin-left: 8px"
            >
              全选当前画布
            </el-checkbox>
            <el-button
              v-if="totalSelectedCount > 0"
              size="small"
              plain
              @click="onGlobalSelectAll(false)"
              style="margin-left: 8px"
            >
              清空勾选
            </el-button>
          </div>
          <span class="folder-count">
            已勾选 <b style="color: #409EFF">{{ totalSelectedCount }}</b> / 共 {{ filteredPages.length }} 个画布 ({{ folderGroups.length }} 个文件夹)
          </span>
        </div>

        <div v-if="!pages.length" class="empty-tip">
          <el-empty description="尚无画布数据，请先在主流程运行读取阶段" />
        </div>
        <div v-else class="folder-groups">
          <div v-for="fg in filteredFolderGroups" :key="fg.path || '__ungrouped__'" class="folder-group">
            <div class="folder-header">
              <div class="folder-title">
                <el-checkbox v-model="fg.allSelected" @change="onFolderSelectAll(fg)" :indeterminate="fg.indeterminate">
                  {{ fg.path || '未分组' }} ({{ fg.pages.length }})
                </el-checkbox>
              </div>
              <!-- 批量按钮的语义：作用于「本文件夹下被勾选的画布」。
                   一个都没勾时按钮整体隐藏（而不是置灰）：置灰的彩色按钮仍会被
                   误读成"点这一块＝执行这一块"，而实际执行范围取决于勾选，
                   隐藏能让"要执行就先勾画布"这件事变得没有歧义。
                   按钮上的数字就是本次会执行的画布数量，动手前即可核对。 -->
              <div class="folder-actions" v-if="fg.selectedCount">
                <span class="action-scope">将对勾选的 {{ fg.selectedCount }} 个画布执行：</span>
                <el-button size="mini" type="info" plain @click="onFolderBatchAction(fg, 'struct')">
                  🔧 结构化 ({{ fg.selectedCount }})
                </el-button>
                <el-button size="mini" type="primary" @click="onFolderBatchAction(fg, 'clarify')">
                  🔍 澄清 ({{ fg.selectedCount }})
                </el-button>
                <el-button size="mini" type="success" @click="onFolderBatchAction(fg, 'design')">
                  📐 测试设计 ({{ fg.selectedCount }})
                </el-button>
                <el-button size="mini" type="warning" @click="onFolderBatchAction(fg, 'generate-cases')">
                  📋 用例 ({{ fg.selectedCount }})
                </el-button>
              </div>
              <div class="folder-actions folder-actions--hint" v-else>
                <span class="action-scope action-scope--muted">勾选画布后可批量执行（当前未勾选）</span>
              </div>
            </div>
            <div class="canvas-grid">
              <el-card v-for="p in fg.pages" :key="p.id" class="canvas-card" shadow="hover"
                :class="{ 'is-failed': p.stage_status === 'failed', 'is-active': p.stage_status === 'extracting', 'is-selected': p.selected }">
                <el-image v-if="p.screenshot" class="card-thumb" :src="shotUrl(p.screenshot)"
                  :preview-src-list="shotPreviewList(p)" :initial-index="0" fit="contain" hide-on-click-modal lazy>
                  <template #error><div class="thumb-error">截图加载失败</div></template>
                </el-image>
                <div class="card-thumb thumb-empty" v-else>
                  <span>暂无截图</span>
                </div>
                <div class="card-head">
                  <div class="card-title">
                    <el-checkbox v-model="p.selected" @change="onCanvasSelect(p, fg)" style="margin-right: 8px" />
                    <span class="page-no">P{{ p.page_no }}</span>
                    <span class="page-name">{{ p.page_name || '未命名画布' }}</span>
                  </div>
                  <el-tag :type="stageMeta(p.current_stage, p.stage_status).type" size="small">
                    {{ stageMeta(p.current_stage, p.stage_status).label }}
                  </el-tag>
                </div>
                <el-progress class="card-progress" :percentage="Math.round(p.current_stage / 4 * 100)"
                  :status="p.stage_status === 'failed' ? 'exception' : ''" :stroke-width="8" />
                <div class="card-meta">
                  <span>识别摘要：{{ p.recognition_note || '无' }}</span>
                </div>
                <div v-if="p.error_message && p.stage_status === 'failed'" class="card-error">⚠ {{ p.error_message }}</div>
                <div class="card-actions">
                  <el-button type="primary" size="small" @click="openCanvas(p)">进入当前阶段</el-button>
                  <el-button size="small" @click="openCanvas(p)">查看产物</el-button>
                </div>
              </el-card>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <!-- ============ Tab2：跨画布用例集合 ============ -->
      <el-tab-pane label="用例集合" name="cases" lazy>
        <div class="cases-toolbar">
          <el-select v-model="caseFilter" placeholder="按画布筛选" clearable size="small" style="width: 260px">
            <el-option label="全部画布" value="all" />
            <el-option v-for="p in pages" :key="p.id" :label="(p.parent_name ? (p.parent_name + ' / ') : '') + (p.page_name || ('P' + p.page_no))"
              :value="p.id" />
          </el-select>
          <span class="cases-count">共 {{ filteredCases.length }} 条正式用例 / {{ filteredSmoke.length }} 条冒烟</span>
        </div>

        <el-empty v-if="!filteredCases.length && !filteredSmoke.length" description="尚无任何用例产物" />
        <div v-else class="cases-wrap">
          <el-collapse v-model="caseGroups" accordion>
            <el-collapse-item v-for="grp in groupedCases" :key="grp.key" :name="grp.key" :title="grp.title">
              <el-table :data="grp.items" size="small" border stripe max-height="520">
                <el-table-column type="index" width="48" />
                <el-table-column prop="module" label="模块" width="160" />
                <el-table-column label="用例标题" min-width="220">
                  <template #default="{ row }">{{ row.title || '未命名用例' }}</template>
                </el-table-column>
                <el-table-column prop="precondition" label="前置条件" min-width="160" show-overflow-tooltip />
                <el-table-column label="步骤数" width="90" align="center">
                  <template #default="{ row }">{{ row.steps || 0 }}</template>
                </el-table-column>
                <el-table-column label="来源画布" width="180" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.sourceCanvas }}</template>
                </el-table-column>
              </el-table>
            </el-collapse-item>
          </el-collapse>

          <div v-if="filteredSmoke.length" class="smoke-block">
            <h4>冒烟用例</h4>
            <el-table :data="filteredSmoke" size="small" border stripe max-height="360">
              <el-table-column type="index" width="48" />
              <el-table-column prop="module" label="模块" width="160" />
              <el-table-column label="用例标题" min-width="240">
                <template #default="{ row }">{{ row.title || '未命名用例' }}</template>
              </el-table-column>
              <el-table-column label="来源画布" width="200" show-overflow-tooltip>
                <template #default="{ row }">{{ row.sourceCanvas }}</template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 单画布详情抽屉 ============ -->
    <el-drawer :title="focused && ((focused.parent_name ? (focused.parent_name + ' / ') : '') + 'P' + focused.page_no + ' ' + (focused.page_name || '未命名画布')) + ' · 单画布流水线'"
      v-model="drawerVisible" size="62%" direction="rtl" @closed="focused = null">
      <div v-if="focused" class="canvas-detail" v-loading="_fullLoading" element-loading-text="正在加载画布详细产物...">
        <el-alert type="info" :closable="false" show-icon
          :title="`当前阶段：${stageMeta(focused.current_stage, focused.stage_status).label}`"
          :description="`单画布模式：每点击一次下方按钮只会对「P${focused.page_no} ${focused.page_name || '未命名画布'}」这一个画布生效，不影响其他画布（原型允许同名画布，以 P 页码区分）。`" />

        <div class="detail-actions">
          <el-button size="small" :loading="busy" @click="runCanvasStage('struct', focused)">🔧 结构化</el-button>
          <el-button size="small" :loading="busy" @click="runCanvasStage('clarify', focused)">🔍 澄清</el-button>
          <el-button size="small" :loading="busy" @click="runCanvasStage('design', focused)">📐 测试设计</el-button>
          <el-button size="small" type="primary" :loading="busy" @click="runCanvasStage('generate-cases', focused)">📋 生成用例</el-button>
        </div>

        <el-steps :active="focused.current_stage" align-center finish-status="success" class="detail-steps">
          <el-step title="读取" />
          <el-step title="通读" />
          <el-step title="澄清/设计" />
          <el-step title="用例" />
        </el-steps>

        <div v-if="focused.screenshot || (focused.screenshot_segments && parseArr(focused.screenshot_segments).length)" class="detail-shot">
          <h5>📷 画布截图</h5>
          <el-image
            :src="shotUrl(focused.screenshot)"
            :preview-src-list="shotPreviewList(focused)"
            fit="contain"
            style="width: 100%; max-height: 320px; background: #f5f7fa; border-radius: 6px;"
            :initial-index="0"
            hide-on-click-modal
          />
          <div v-if="parseArr(focused.screenshot_segments).length > 1" class="shot-segs">
            <span>该画布被拆分为 {{ parseArr(focused.screenshot_segments).length }} 段，点击图片可逐段查看</span>
          </div>
        </div>

        <!-- 画布级产物：查看 / 编辑（保存后自动重算任务级合并视图） -->
        <CanvasArtifactCard title="需求摘要与模块拆分" icon="📄" kind="text"
          field="page_summary" :raw="focused.page_summary"
          :proto-id="protoId" :page-id="focused.id"
          :class="{ 'stage-current': focused.current_stage === 1 }"
          @saved="onArtifactSaved" />

        <CanvasArtifactCard title="需求澄清" icon="❓" kind="clarify"
          field="page_clarification" :raw="focused.page_clarification"
          :proto-id="protoId" :page-id="focused.id"
          :class="{ 'stage-current': focused.current_stage === 2 }"
          @saved="onArtifactSaved" />

        <CanvasArtifactCard title="测试点" icon="🧩" kind="json"
          field="page_testpoints_json" :raw="focused.page_testpoints_json"
          :proto-id="protoId" :page-id="focused.id"
          :class="{ 'stage-current': focused.current_stage === 3 }"
          @saved="onArtifactSaved" />

        <CanvasArtifactCard title="正式用例" icon="🧪" kind="json"
          field="page_testcases_json" :raw="focused.page_testcases_json"
          :proto-id="protoId" :page-id="focused.id"
          :class="{ 'stage-current': focused.current_stage === 3 }"
          @saved="onArtifactSaved" />

        <CanvasArtifactCard title="冒烟用例" icon="🔥" kind="json"
          field="page_smoke_json" :raw="focused.page_smoke_json"
          :proto-id="protoId" :page-id="focused.id"
          :class="{ 'stage-current': focused.current_stage === 4 }"
          @saved="onArtifactSaved" />
      </div>
    </el-drawer>
  </div>
</template>

<script>
import request from '@/utils/api'
import CanvasArtifactCard from './CanvasArtifactCard.vue'

export default {
  name: 'ModaoCanvasBoard',
  components: { CanvasArtifactCard },
  data() {
    return {
      protoId: this.$route.params.id,
      proto: null,
      pages: [],
      activeTab: 'board',
      loading: false,
      busy: false,
      drawerVisible: false,
      focused: null,
      caseFilter: 'all',
      caseGroups: '',
      pollTimer: null,
      folderFilter: '',
      // 待写回服务端的勾选变更 {pageId: bool}；合并成一次请求。
      // 同时用于 loadDetail 时"不被轮询覆盖"，见 loadDetail 里的注释。
      pendingSelection: {},
      _selTimer: null,
      // 首屏走 ?light=1 轻量接口；这两个标记控制"是否已补拉过全量详情"，
      // 一旦补拉过，后续轮询也必须用全量，避免把抽屉/用例集合刚拿到的大字段清掉。
      _fullLoaded: false,
      _fullLoading: false,
      _polling: false,
      pollFailCount: 0,
    }
  },
  computed: {
    filteredCases() {
      return this.allCases.filter(c => this.caseFilter === 'all' || c.sourceCanvasId === this.caseFilter)
    },
    filteredSmoke() {
      return this.allSmoke.filter(c => this.caseFilter === 'all' || c.sourceCanvasId === this.caseFilter)
    },
    groupedCases() {
      const map = new Map()
      for (const c of this.filteredCases) {
        const key = c.module || '未分类'
        if (!map.has(key)) {
          map.set(key, {
            key,
            title: '📁 ' + key + `（${this.filteredCases.filter(x => (x.module || '未分类') === key).length} 条）`,
            items: [],
          })
        }
        map.get(key).items.push(c)
      }
      return Array.from(map.values())
    },
    allCases() {
      const out = []
      for (const p of this.pages) {
        const source = this.canvasLabel(p)
        for (const tc of this.parseArr(p.page_testcases_json)) {
          const inner = tc.testcase || tc
          out.push({
            module: tc.module || inner.module || '',
            title: inner.title || inner.name || inner.case_name || '',
            precondition: inner.precondition || inner.pre_condition || '',
            steps: Array.isArray(inner.steps) ? inner.steps.length : (inner.steps ? 1 : 0),
            sourceCanvas: source,
            sourceCanvasId: p.id,
          })
        }
      }
      return out
    },
    allSmoke() {
      const out = []
      for (const p of this.pages) {
        const source = this.canvasLabel(p)
        for (const tc of this.parseArr(p.page_smoke_json)) {
          const inner = tc.testcase || tc
          out.push({
            module: tc.module || inner.module || '',
            title: inner.title || inner.name || inner.case_name || '',
            sourceCanvas: source,
            sourceCanvasId: p.id,
          })
        }
      }
      return out
    },
    folderGroups() {
      const map = new Map()
      for (const p of this.pages) {
        const folder = (p.folder || '').trim()
        if (!map.has(folder)) {
          map.set(folder, { path: folder, pages: [], allSelected: false, indeterminate: false, selectedCount: 0 })
        }
        map.get(folder).pages.push(p)
      }
      for (const fg of map.values()) {
        const selected = fg.pages.filter(p => p.selected).length
        fg.selectedCount = selected
        fg.allSelected = selected === fg.pages.length && fg.pages.length > 0
        fg.indeterminate = selected > 0 && selected < fg.pages.length
      }
      return Array.from(map.values()).sort((a, b) => (a.path || '').localeCompare(b.path || ''))
    },
    filteredFolderGroups() {
      if (!this.folderFilter) return this.folderGroups
      return this.folderGroups.filter(fg => fg.path === this.folderFilter)
    },
    filteredPages() {
      if (!this.folderFilter) return this.pages
      return this.pages.filter(p => (p.folder || '').trim() === this.folderFilter)
    },
    totalSelectedCount() {
      return (this.filteredPages || []).filter(p => p.selected).length
    },
    globalAllSelected: {
      get() {
        const pages = this.filteredPages || []
        return pages.length > 0 && pages.every(p => p.selected)
      },
      set(val) {
        this.onGlobalSelectAll(val)
      }
    },
    globalIndeterminate() {
      const selected = this.totalSelectedCount
      const total = (this.filteredPages || []).length
      return selected > 0 && selected < total
    },
    isRunning() {
      const protoBusy = Boolean(
        this.proto && ['extracting', 'structuring', 'clarifying', 'designing'].includes(this.proto.status)
      )
      const pageBusy = Array.isArray(this.pages) && this.pages.some(p => p && p.stage_status === 'extracting')
      return protoBusy || pageBusy
    },
  },
  watch: {
    // 切到「用例集合」tab 时才需要逐页用例产物（allCases/allSmoke 会遍历
    // page_testcases_json / page_smoke_json），首屏 light 模式不含这些字段，
    // 因此在这里按需补拉全量详情。
    activeTab(val) {
      if (val === 'cases') this.ensureFullDetail()
    }
  },
  mounted() {
    // protoId 必须是数字 pk；非数字（如 'new'）说明入口错误，跳过 loadDetail 避免 404 报错
    if (this.protoId && !isNaN(Number(this.protoId))) {
      this.loadDetail()
    }
  },
  beforeDestroy() {
    this.stopPoll()
    // 防抖没到点就离开页面时，必须补一次写回，否则最后一次勾选会丢
    if (this._selTimer) {
      clearTimeout(this._selTimer)
      this._selTimer = null
      this.flushSelectionSave()
    }
  },
  methods: {
    base() {
      // ⚠️ 必须带 `/api` 前缀。后端真实路由是 `path('api/requirement-analysis/', ...)`，
      // 不带 /api 的形态**只有 log-viewer / log-raw 两个别名**，其余接口一律 404。
      //
      // 实测（容器内带鉴权，排除 SPA 兜底干扰）：
      //   /requirement-analysis/api/modao/80/clarify/      -> 404
      //   /api/requirement-analysis/api/modao/80/clarify/  -> 200  ← 正确
      //
      // ⚠️ 别用 `curl` 看不带 /api 的地址来判断"通不通"：nginx 对 GET 会返回
      // SPA 兜底（200 + index.html），看起来是通的，其实是假的。POST 才会露出 404。
      return '/requirement-analysis/api/modao'
    },
    shotUrl(rel) {
      if (!rel) return ''
      // 已经是 http(s)/data 开头（已拼好）就直接返回；否则按相对 MEDIA_ROOT 路径拼接
      if (/^(https?:|data:)/i.test(rel)) return rel
      let path = rel.replace(/^\/+/, '')
      // 后端模型存的是相对 MEDIA_ROOT 的路径（如 modao/<uuid>/page_1.jpg）
      if (!path.startsWith('media/')) path = 'media/' + path
      return '/' + path
    },
    shotPreviewList(p) {
      if (!p) return []
      if (p._previewList) return p._previewList
      // 优先使用分段截图（超长画布被拆成多段）；否则仅主图
      const segs = this.parseArr(p.screenshot_segments)
      const list = segs.length ? segs.map(s => this.shotUrl(s)) : (p.screenshot ? [this.shotUrl(p.screenshot)] : [])
      p._previewList = list
      return list
    },
    canvasLabel(p) {
      return (p.parent_name ? (p.parent_name + ' / ') : '') + (p.page_name || ('P' + p.page_no))
    },
    stageMeta(stage, status) {
      if (status === 'failed') return { label: '失败', type: 'danger' }
      if (status === 'extracting') return { label: '读取中', type: 'warning' }
      if (status === 'needs_rerun') return { label: '需重跑', type: 'warning' }
      const map = {
        0: { label: '待读取', type: 'info' },
        1: { label: '已通读', type: 'success' },
        2: { label: '已澄清', type: 'success' },
        3: { label: '已设计', type: 'success' },
        4: { label: '已生成', type: 'success' },
      }
      return map[stage] || { label: '阶段' + stage, type: 'info' }
    },
    parseArr(s) {
      if (!s) return []
      if (Array.isArray(s)) return s
      try {
        const v = JSON.parse(s)
        return Array.isArray(v) ? v : (v.data && Array.isArray(v.data) ? v.data : [])
      } catch (e) {
        return []
      }
    },
    pretty(s) {
      if (!s) return ''
      try {
        return JSON.stringify(JSON.parse(s), null, 2)
      } catch (e) {
        return s
      }
    },
    async loadDetail() {
      if (this._polling) return
      this._polling = true
      this.loading = true
      try {
        // 性能与防超时修复（2026-11-06）：
        // 轮询一律使用轻量模式（?light=1），仅跟踪画布状态和任务进度（约 112KB / 0.5s）。
        // 绝不反复轮询全量详情（6.8MB），彻底避免网络与 Socket 队列拥塞导致 300000ms 超时。
        // 全量业务产物（用例/测试点/摘要）由 ensureFullDetail() 按需拉取并缓存在内存中。
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get', params: { light: 1 } })

        // 合并 proto 基础状态字段，保留已有的全量重度字段
        this.proto = {
          ...this.proto,
          ...res.data,
          testcases_json: res.data.testcases_json !== undefined ? res.data.testcases_json : this.proto?.testcases_json,
          final_testpoints_json: res.data.final_testpoints_json !== undefined ? res.data.final_testpoints_json : this.proto?.final_testpoints_json,
          extracted_json: res.data.extracted_json !== undefined ? res.data.extracted_json : this.proto?.extracted_json,
          smoke_json: res.data.smoke_json !== undefined ? res.data.smoke_json : this.proto?.smoke_json,
        }

        const incoming = (res.data.pages || []).slice().sort((a, b) => (a.page_no || 0) - (b.page_no || 0))
        // 勾选状态以服务端为准（它已持久化），但用户刚点、还没写回完成的改动
        // 不能被轮询结果覆盖 —— 否则会出现"勾了又被弹回去"的抖动。
        const pending = this.pendingSelection
        const oldMap = new Map((this.pages || []).map(x => [x.id, x]))
        this.pages = incoming.map(p => {
          const old = oldMap.get(p.id)
          const targetSelected = (pending && Object.prototype.hasOwnProperty.call(pending, p.id)) ? pending[p.id] : p.selected
          if (old &&
              old.stage_status === p.stage_status &&
              old.current_stage === p.current_stage &&
              old.page_name === p.page_name &&
              old.selected === targetSelected &&
              old.error_message === p.error_message &&
              old.recognition_note === p.recognition_note &&
              old.screenshot === p.screenshot) {
            return old
          }
          return {
            ...old,
            ...p,
            // 保持可能已加载的重度业务产物不被 light 模式覆盖
            page_summary: p.page_summary !== undefined ? p.page_summary : (old ? old.page_summary : undefined),
            page_clarification: p.page_clarification !== undefined ? p.page_clarification : (old ? old.page_clarification : undefined),
            page_testpoints_json: p.page_testpoints_json !== undefined ? p.page_testpoints_json : (old ? old.page_testpoints_json : undefined),
            page_testcases_json: p.page_testcases_json !== undefined ? p.page_testcases_json : (old ? old.page_testcases_json : undefined),
            page_smoke_json: p.page_smoke_json !== undefined ? p.page_smoke_json : (old ? old.page_smoke_json : undefined),
            selected: targetSelected,
          }
        })
        if (this.focused && this.focused.id) {
          // 保持抽屉聚焦对象同步到更新后的新页面对象（保留已有的重字段）
          const fid = this.focused.id
          const found = (this.pages || []).find(x => x.id === fid)
          if (found) this.focused = found
        }
        this.pollFailCount = 0
        if (this.isRunning) {
          this.startPoll()
        } else {
          this.stopPoll()
        }
      } catch (e) {
        this.pollFailCount = (this.pollFailCount || 0) + 1
        if (this.pollFailCount >= 5) {
          this.stopPoll()
          this.$message.error('加载失败次数过多已停止轮询：' + (e.response?.data?.detail || e.message))
        } else {
          console.warn('[canvas-board] 轮询刷新失败:', e?.message)
        }
      } finally {
        this.loading = false
        this._polling = false
      }
    },
    /**
     * 按需补拉全量详情（含逐页用例产物）。
     *
     * 首屏走 light 模式省掉 1091KB 的 page_testcases_json / page_summary /
     * page_testpoints_json / page_smoke_json，只有这两处真正需要它们：
     *   ① 切到「用例集合」tab（allCases / allSmoke 要遍历逐页用例）
     *   ② 打开「进入当前阶段」抽屉（focused.page_* 要渲染各阶段产物）
     * 拉过一次就置 _fullLoaded，不重复请求。
     */
    async ensureFullDetail() {
      if (this._fullLoaded || this._fullLoading) return
      this._fullLoading = true
      try {
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
        const incoming = (res.data.pages || []).slice().sort((a, b) => (a.page_no || 0) - (b.page_no || 0))
        const pending = this.pendingSelection
        const oldMap = new Map((this.pages || []).map(x => [x.id, x]))
        this.pages = incoming.map(p => {
          const old = oldMap.get(p.id) || {}
          return {
            ...old,
            ...p,
            selected: (pending && Object.prototype.hasOwnProperty.call(pending, p.id)) ? pending[p.id] : p.selected,
          }
        })
        // 顶层字段（testcases_json/merged_testcases_json 等）也一并补上，
        // 但保留轻量阶段已有的 proto 结构，避免覆盖掉首屏正在用的字段
        this.proto = { ...this.proto, ...res.data }
        this._fullLoaded = true
        // 抽屉正开着时，把 focused 指向新对象，产物区域才能渲染出来
        if (this.focused) {
          const again = this.pages.find(p => p.id === this.focused.id)
          if (again) this.focused = again
        }
      } catch (e) {
        // 补拉失败不影响首屏查看，用户仍能看到画布卡片
        console.warn('[canvas-board] 补拉全量详情失败:', e?.message)
      } finally {
        this._fullLoading = false
      }
    },
    /**
     * 把勾选结果写回服务端。
     *
     * 为什么要持久化：勾选状态原本只活在当前页面的内存里，
     * 一旦刷新/重新进入，旧的勾选被丢弃、全部回到默认值，
     * 用户每次都要重新取消一遍不需要的画布。
     *
     * 合并写：用 300ms 防抖 + 累积变更集，连点多个画布只发一次请求，
     * 避免逐个点击打出一串请求。
     */
    queueSelectionSave(id, selected) {
      this.pendingSelection[id] = selected
      if (this._selTimer) clearTimeout(this._selTimer)
      this._selTimer = setTimeout(() => this.flushSelectionSave(), 300)
    },
    async flushSelectionSave() {
      this._selTimer = null
      const items = Object.entries(this.pendingSelection)
        .map(([id, selected]) => ({ id: Number(id), selected }))
      if (!items.length) return
      // 先清空待写集合：写入期间的再次点击会进入新的批次
      this.pendingSelection = {}
      try {
        await request({
          url: `${this.base()}/${this.protoId}/update-pages/`,
          method: 'post',
          data: { pages: items },
        })
      } catch (e) {
        this.$message.error('画布勾选状态保存失败：'
          + (e.response?.data?.detail || e.message))
      }
    },
    async openCanvas(p) {
      // 抽屉要渲染 page_summary / page_testpoints_json / page_testcases_json /
      // page_smoke_json 等逐页产物，首屏 light 模式没有这些字段 ⇒ 先补拉全量。
      // 已拉过（_fullLoaded）时 ensureFullDetail 会立即返回，无额外请求。
      this.focused = p
      this.drawerVisible = true
      if (!this._fullLoaded) {
        await this.ensureFullDetail()
      }
    },
    /** 画布产物保存后：刷新列表并保持抽屉停在同一个画布上（loadDetail 会替换 pages 数组） */
    async onArtifactSaved() {
      const id = this.focused && this.focused.id
      await this.loadDetail()
      if (id) {
        const fresh = this.pages.find(x => x.id === id)
        if (fresh) this.focused = fresh
      }
    },
    onGlobalSelectAllChange(val) {
      this.onGlobalSelectAll(val)
    },
    onGlobalSelectAll(selected) {
      const targetPages = this.filteredPages || []
      if (!targetPages.length) return
      for (const p of targetPages) {
        p.selected = selected
        this.pendingSelection[p.id] = selected
      }
      this.flushSelectionSave()
      this.$message.success(selected ? `已全部勾选 ${targetPages.length} 个画布` : '已全部取消勾选')
    },
    onFolderSelectAll(fg) {
      const selected = fg.allSelected
      for (const p of fg.pages) {
        p.selected = selected
        this.queueSelectionSave(p.id, selected)
      }
    },
    onCanvasSelect(p, fg) {
      const selected = fg.pages.filter(x => x.selected).length
      fg.selectedCount = selected
      fg.allSelected = selected === fg.pages.length && fg.pages.length > 0
      fg.indeterminate = selected > 0 && selected < fg.pages.length
      // 单画布勾选也要落库：这是"下次进来记住上次选择"的关键一步
      this.queueSelectionSave(p.id, !!p.selected)
    },
    async onFolderBatchAction(fg, action) {
      const selectedPages = fg.pages.filter(p => p.selected)
      if (!selectedPages.length) {
        this.$message.warning('请先勾选要运行的画布')
        return
      }
      const estimatedTokens = selectedPages.length * 500 * 1.5
      const actionLabels = { 'struct': '需求结构化', 'clarify': '需求澄清', 'design': '测试设计', 'generate-cases': '生成用例' }
      const actionLabel = actionLabels[action] || action

      const missingSummary = selectedPages.filter(p => !(p.page_summary || '').trim()).length
      const summaryWarn = (['design', 'generate-cases'].includes(action) && missingSummary)
        ? `\n\n⚠️ 其中 ${missingSummary} 个画布还没有①结构化摘要，将用「本画布原文+澄清结论」兜底生成（建议先跑结构化，模块拆分更准）。`
        : ''
      try {
        await this.$confirm(
          `将为文件夹「${fg.path || '未分组'}」下的 ${selectedPages.length} 个画布运行【${actionLabel}】，预计消耗 token 约 ${Math.round(estimatedTokens)} 个。${summaryWarn}\n\n是否继续？批量执行将按顺序串行排队。\n可随时点页面顶部「刷新」查看进度。`,
          `${actionLabel} · 批量确认`,
          {
            distinguishCancelAndClose: true,
            confirmButtonText: '继续批量',
            cancelButtonText: '取消',
            type: 'info',
          }
        )
        const ok = await this.runCanvasStage(action, { id: selectedPages.map(p => p.id) })
        // 只有接口真的成功才提示"已提交"，失败时 runCanvasStage 已弹过具体错误
        if (ok) {
          this.$message.success(`已为文件夹「${fg.path || '未分组'}」的 ${selectedPages.length} 个画布提交【${actionLabel}】，请稍候`)
        }
      } catch (err) {
        // 点「取消 / 关闭」确认框 = 什么都不做。
        //
        // ⚠️ 这里曾有两个 bug，都会造成"点了取消反而继续执行"：
        //   1) 形参命名为 action，遮蔽了外层入参 → 传下去的是 'cancel' → 拼出
        //      `.../80/cancel/` → 每台设备 404；
        //   2) 更严重的是**取消了还逐个画布发请求**（原意是想"退化成逐个执行"）——
        //      点「取消」的语义就是中止，绝不能再发任何执行请求。
        //      Element Plus 的 $confirm 在 distinguishCancelAndClose 下
        //      取消会 reject 出字符串 'cancel'，这里必须直接忽略。
        if (err !== 'cancel' && err !== 'close') {
          // 非取消类异常（极少见）才提示，避免静默失败
          this.$message.warning(`批量提交未执行：${err && err.message ? err.message : err}`)
        }
      }
    },
    /** 画布级前置校验：③测试设计/④生成用例依赖①结构化摘要，缺失时给出明确提示 */
    async ensureRequirementReady(page, action) {
      if (!['design', 'generate-cases'].includes(action)) return true
      if (Array.isArray(page.id)) return true            // 批量运行：不逐个确认，直接放行
      if ((page.page_summary || '').trim()) return true  // 已有结构化摘要
      const hasInput = (page.page_clarification || '').trim()
      try {
        await this.$confirm(
          `画布「${this.canvasLabel(page)}」还没有①结构化摘要（page_summary 为空）。\n\n` +
          (hasInput
            ? '继续的话，后端会用「本画布原文 + 已填澄清结论」兜底生成，模块拆分可能不如结构化后准确。'
            : '该画布也没有可用的原文/澄清内容，后端会直接中止生成（避免凭空编造无关用例）。') +
          '\n\n建议先点「🔧 结构化」，再生成测试点。仍要继续吗？',
          '该画布尚未结构化',
          { type: 'warning', confirmButtonText: '仍然继续', cancelButtonText: '先去结构化' }
        )
        return true
      } catch (e) {
        return false
      }
    },
    async runCanvasStage(action, page) {
      if (this.busy) return
      if (!(await this.ensureRequirementReady(page, action))) {
        if (!Array.isArray(page.id)) this.$message.info('已取消：请先运行「🔧 结构化」')
        return
      }
      this.busy = true
      try {
        const pageIds = Array.isArray(page.id) ? page.id : [page.id]
        await request({
          url: `${this.base()}/${this.protoId}/${action}/`,
          method: 'post',
          data: { page_ids: pageIds },
        })
        if (!Array.isArray(page.id)) {
          this.$message.success(`已为画布「${this.canvasLabel(page)}」提交${this.stageMeta(page.current_stage, page.stage_status).label}的运行请求`)
        }
        this.startPoll()
        // 返回成功标志：调用方（onFolderBatchAction）据此决定要不要提示"已提交"。
        // 之前这里把异常吞在内部，调用方照样弹"提交成功"，接口其实已经失败 →
        // 同屏出现"提交失败 404"和"提交成功"两条互相矛盾的消息，误导极强。
        return true
      } catch (e) {
        this.$message.error('提交失败：' + (e.response?.data?.detail || e.message))
        return false
      } finally {
        this.busy = false
      }
    },
    startPoll() {
      this.stopPoll()
      this.pollTimer = setInterval(() => this.loadDetail(), 3000)
    },
    stopPoll() {
      if (this.pollTimer) {
        clearInterval(this.pollTimer)
        this.pollTimer = null
      }
    },
    goBack() {
      this.$router.push('/ai-generation/modao-skill')
    },
    goToPipeline() {
      this.$router.push(`/ai-generation/modao-pipeline/${this.protoId}`)
    },
    goToCaseMerge() {
      this.$router.push(`/ai-generation/modao-case-merge/${this.protoId}`)
    },
  },
}
</script>

<style scoped lang="scss">
.canvas-board {
  padding: 16px 20px 40px;
  .page-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    .header-left {
      display: flex;
      align-items: center;
      gap: 12px;
      h2 { margin: 0; font-size: 18px; }
    }
    .proto-title { color: #606266; font-size: 13px; }
  }
  .canvas-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
    margin-top: 8px;
  }
  .canvas-card {
    &.is-failed { border-color: #f56c6c; }
    &.is-active { border-color: #e6a23c; }
    &.is-selected { border-color: #409eff; box-shadow: 0 0 0 2px rgba(64,158,255,.2); }
    .card-thumb {
      position: relative;
      display: block;
      width: 100%;
      height: 160px;
      border-radius: 6px;
      overflow: hidden;
      background: #f5f7fa;
      margin-bottom: 10px;
      cursor: zoom-in;
      :deep(img) { width: 100%; height: 100%; object-fit: contain; display: block; background: #fff; }
      .thumb-error {
        display: flex; align-items: center; justify-content: center;
        height: 100%; color: #c0c4cc; font-size: 12px;
      }
      &.thumb-empty {
        display: flex; align-items: center; justify-content: center;
        color: #c0c4cc; font-size: 12px; cursor: default;
      }
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 8px;
    }
    .card-title { display: flex; flex-direction: column; gap: 2px; }
    .page-no { font-size: 11px; color: #909399; }
    .page-name { font-weight: 600; font-size: 14px; }
    .card-progress { margin: 10px 0 6px; }
    .card-meta { font-size: 12px; color: #909399; }
    .card-error { font-size: 12px; color: #f56c6c; margin: 6px 0; }
    .card-actions { display: flex; gap: 8px; margin-top: 8px; }
  }
  .cases-toolbar {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 12px;
    .cases-count { color: #606266; font-size: 13px; }
  }
  .smoke-block { margin-top: 18px; h4 { margin: 0 0 8px; } }
  .canvas-detail {
    .detail-actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
    .detail-steps { margin: 12px 0 18px; }
    .detail-shot {
      margin-bottom: 14px;
      h5 { margin: 6px 0; color: #409eff; }
      .shot-segs { font-size: 12px; color: #909399; margin-top: 4px; }
    }
    .detail-stage { margin-bottom: 14px; &.stage-current { border: 1px solid #409eff; box-shadow: 0 0 8px rgba(64,158,255,.25); }
      .stage-pre { white-space: pre-wrap; word-break: break-word; max-height: 360px; overflow: auto; background: #fafafa; padding: 10px; border-radius: 6px; font-size: 12px; margin: 0; }
      .sub-block { margin-bottom: 12px; h5 { margin: 6px 0; color: #409eff; } }
    }
  }
  .empty-tip { padding: 40px 0; }

  .folder-filter-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 16px;
    margin-bottom: 16px;
    padding: 12px 16px;
    background: #f5f7fa;
    border-radius: 8px;
    .filter-controls {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .folder-count { color: #606266; font-size: 13px; }
  }

  .folder-groups {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .folder-group {
    border: 1px solid #e4e7ed;
    border-radius: 8px;
    padding: 16px;
    background: #fff;
  }
  .folder-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid #ebeef5;
    .folder-title {
      font-size: 14px;
      font-weight: 600;
      color: #303133;
    }
    .folder-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      // Element Plus 会给相邻按钮加 margin-left，与 gap 叠加会把这一行撑破，
      // 导致后面的按钮换行/被裁掉。这里清零，只保留 gap。
      :deep(.el-button + .el-button) {
        margin-left: 0;
      }
    }
    // 「本次将执行 N 个画布」的说明文字：把执行范围写在按钮旁边，
    // 避免用户误以为"点这一块 = 执行这一块画布"。
    .action-scope {
      font-size: 12px;
      color: #606266;
      margin-right: 4px;
      &--muted {
        color: #c0c4cc;
      }
    }
  }
}
</style>
