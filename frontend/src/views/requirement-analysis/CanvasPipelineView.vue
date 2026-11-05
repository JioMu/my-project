<template>
  <div class="canvas-pipeline">
    <div class="page-header">
      <div class="header-left">
        <el-button size="small" plain @click="goBack">← 返回画布看板</el-button>
        <h2>🚀 画布独立流水线</h2>
      </div>
      <div class="header-right">
        <span v-if="proto" class="proto-title">{{ proto.title || ('任务 #' + protoId) }}</span>
        <el-button size="small" @click="loadDetail" :loading="loading">刷新</el-button>
      </div>
    </div>

    <div class="pipeline-layout">
      <!-- 左侧：画布列表 -->
      <div class="canvas-list-panel">
        <div class="panel-header">
          <h3>画布列表 ({{ pages.length }})</h3>
          <el-select v-model="folderFilter" placeholder="按文件夹筛选" clearable size="mini" style="width: 100%">
            <el-option label="全部文件夹" value="" />
            <el-option v-for="fg in folderGroups" :key="fg.path" :label="fg.path || '未分组'" :value="fg.path" />
          </el-select>
        </div>
        <div class="canvas-list">
          <div v-for="p in filteredPages" :key="p.id" 
               class="canvas-list-item"
               :class="{ active: activeCanvasId === p.id }"
               @click="selectCanvas(p)">
            <div class="item-header">
              <span class="page-no">P{{ p.page_no }}</span>
              <span class="page-name">{{ p.page_name || '未命名' }}</span>
              <el-tag :type="stageMeta(p.current_stage, p.stage_status).type" size="mini">
                {{ stageMeta(p.current_stage, p.stage_status).label }}
              </el-tag>
            </div>
            <div class="item-folder" v-if="p.folder">{{ p.folder }}</div>
            <el-progress :percentage="Math.round(p.current_stage / 4 * 100)" :stroke-width="4" />
          </div>
        </div>
      </div>

      <!-- 右侧：当前画布的流水线 -->
      <div class="pipeline-panel" v-if="activeCanvas">
        <div class="panel-header">
          <h3>{{ activeCanvas.page_name || '未命名画布' }} · 独立流水线</h3>
          <el-tag type="info" size="small">{{ activeCanvas.folder || '未分组' }}</el-tag>
        </div>

        <el-alert type="info" :closable="false" show-icon
          :title="`当前阶段：${stageMeta(activeCanvas.current_stage, activeCanvas.stage_status).label}`"
          description="本画布作为独立任务分支运行，以下操作只影响当前画布，互不干扰其它画布。" />

        <div class="stage-actions">
          <el-button size="small" :loading="busy" :disabled="isRunning" @click="runStage('struct')">① 结构化</el-button>
          <el-button size="small" :loading="busy" :disabled="isRunning" @click="runStage('clarify')">② 澄清</el-button>
          <el-button size="small" :loading="busy" :disabled="isRunning" @click="runStage('design')">③ 测试点合并</el-button>
          <el-button size="small" type="primary" :loading="busy" :disabled="isRunning" @click="runStage('generate-cases')">④ 生成用例</el-button>
        </div>

        <el-steps :active="activeCanvas.current_stage" align-center finish-status="success" class="pipeline-steps">
          <el-step title="结构化" />
          <el-step title="澄清" />
          <el-step title="测试点/用例" />
          <el-step title="冒烟" />
        </el-steps>

        <!-- 画布级产物：查看 / 编辑（保存后自动重算任务级合并视图） -->
        <CanvasArtifactCard class="stage-card" title="需求结构化（画布级摘要）" icon="📄" kind="text"
          field="page_summary" :raw="activeCanvas.page_summary"
          :proto-id="protoId" :page-id="activeCanvas.id"
          :class="{ 'stage-current': activeCanvas.current_stage === 1 }"
          @saved="loadDetail" />

        <CanvasArtifactCard class="stage-card" title="需求澄清（画布级）" icon="❓" kind="clarify"
          field="page_clarification" :raw="activeCanvas.page_clarification"
          :proto-id="protoId" :page-id="activeCanvas.id"
          :class="{ 'stage-current': activeCanvas.current_stage === 2 }"
          @saved="loadDetail" />

        <CanvasArtifactCard class="stage-card" title="测试点（画布级）" icon="🧩" kind="json"
          field="page_testpoints_json" :raw="activeCanvas.page_testpoints_json"
          :proto-id="protoId" :page-id="activeCanvas.id"
          :class="{ 'stage-current': activeCanvas.current_stage === 3 }"
          @saved="loadDetail" />

        <CanvasArtifactCard class="stage-card" title="测试用例（画布级）" icon="🧪" kind="json"
          field="page_testcases_json" :raw="activeCanvas.page_testcases_json"
          :proto-id="protoId" :page-id="activeCanvas.id"
          :class="{ 'stage-current': activeCanvas.current_stage === 3 }"
          @saved="loadDetail" />

        <CanvasArtifactCard class="stage-card" title="冒烟用例（画布级）" icon="🔥" kind="json"
          field="page_smoke_json" :raw="activeCanvas.page_smoke_json"
          :proto-id="protoId" :page-id="activeCanvas.id"
          :class="{ 'stage-current': activeCanvas.current_stage === 4 }"
          @saved="loadDetail" />
      </div>

      <div class="pipeline-panel empty" v-else>
        <el-empty description="请从左侧选择一个画布" />
      </div>
    </div>
  </div>
</template>

<script>
import request from '@/utils/api'
import CanvasArtifactCard from './CanvasArtifactCard.vue'

export default {
  name: 'CanvasPipelineView',
  components: { CanvasArtifactCard },
  data() {
    return {
      protoId: this.$route.params.id,
      proto: null,
      pages: [],
      activeCanvasId: null,
      loading: false,
      busy: false,
      folderFilter: '',
      pollTimer: null,
    }
  },
  computed: {
    activeCanvas() {
      return this.pages.find(p => p.id === this.activeCanvasId) || null
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
    filteredPages() {
      if (!this.folderFilter) return this.pages
      return this.pages.filter(p => (p.folder || '').trim() === this.folderFilter)
    },
    isRunning() {
      const protoBusy = Boolean(
        this.proto && ['extracting', 'structuring', 'clarifying', 'designing'].includes(this.proto.status)
      )
      const pageBusy = Array.isArray(this.pages) && this.pages.some(p => p && p.stage_status === 'extracting')
      return protoBusy || pageBusy
    },
  },
  mounted() {
    this.loadDetail()
  },
  beforeDestroy() {
    this.stopPoll()
  },
  methods: {
    base() {
      return '/requirement-analysis/api/modao'
    },
    stageMeta(stage, status) {
      if (status === 'failed') return { label: '失败', type: 'danger' }
      if (status === 'extracting') return { label: '运行中', type: 'warning' }
      if (status === 'needs_rerun') return { label: '需重跑', type: 'warning' }
      const map = {
        0: { label: '待读取', type: 'info' },
        1: { label: '已结构化', type: 'success' },
        2: { label: '已澄清', type: 'success' },
        3: { label: '已生成测试点', type: 'success' },
        4: { label: '已冒烟决策', type: 'success' },
      }
      return map[stage] || { label: '阶段' + stage, type: 'info' }
    },
    parseArr(s) {
      if (!s) return []
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
      this.loading = true
      try {
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
        this.proto = res.data
        this.pages = (res.data.pages || []).slice().sort((a, b) => (a.page_no || 0) - (b.page_no || 0))
        if (!this.activeCanvasId && this.pages.length) {
          this.activeCanvasId = this.pages[0].id
        }
        if (this.isRunning) {
          this.startPoll()
        } else {
          this.stopPoll()
        }
      } catch (e) {
        this.$message.error('加载失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.loading = false
      }
    },
    selectCanvas(p) {
      this.activeCanvasId = p.id
    },
    async runStage(action) {
      if (this.busy || !this.activeCanvas) return
      // ③④依赖①结构化摘要，缺失时先提示（否则需求输入为空，模型容易顺着知识库编无关内容）
      if (['design', 'generate-cases'].includes(action) && !(this.activeCanvas.page_summary || '').trim()) {
        try {
          await this.$confirm(
            `画布「${this.activeCanvas.page_name || '未命名'}」还没有①结构化摘要。\n\n` +
            '继续的话会用「本画布原文 + 已填澄清结论」兜底生成，建议先运行①结构化。仍要继续吗？',
            '该画布尚未结构化',
            { type: 'warning', confirmButtonText: '仍然继续', cancelButtonText: '先去结构化' }
          )
        } catch (e) {
          this.$message.info('已取消：请先运行「① 结构化」')
          return
        }
      }
      this.busy = true
      try {
        await request({
          url: `${this.base()}/${this.protoId}/${action}/`,
          method: 'post',
          data: { page_ids: [this.activeCanvas.id] },
        })
        this.$message.success(`已启动「${this.stageMeta(this.activeCanvas.current_stage, this.activeCanvas.stage_status).label}」独立运行`)
        this.startPoll()
      } catch (e) {
        this.$message.error('启动失败：' + (e.response?.data?.detail || e.message))
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
      this.$router.push(`/ai-generation/modao-board/${this.protoId}`)
    },
  },
}
</script>

<style scoped lang="scss">
.canvas-pipeline {
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
  
  .pipeline-layout {
    display: flex;
    gap: 20px;
    min-height: 600px;
  }
  
  .canvas-list-panel {
    width: 280px;
    flex-shrink: 0;
    border: 1px solid #e4e7ed;
    border-radius: 8px;
    background: #fff;
    overflow: hidden;
    
    .panel-header {
      padding: 12px 16px;
      border-bottom: 1px solid #ebeef5;
      h3 { margin: 0 0 8px; font-size: 14px; }
    }
    
    .canvas-list {
      max-height: 700px;
      overflow-y: auto;
    }
    
    .canvas-list-item {
      padding: 12px 16px;
      border-bottom: 1px solid #f0f0f0;
      cursor: pointer;
      transition: background 0.2s;
      
      &:hover { background: #f5f7fa; }
      &.active { background: #ecf5ff; border-left: 3px solid #409eff; }
      
      .item-header {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
        .page-no { font-size: 11px; color: #909399; }
        .page-name { font-weight: 600; font-size: 13px; flex: 1; }
      }
      .item-folder { font-size: 11px; color: #909399; margin-bottom: 6px; }
    }
  }
  
  .pipeline-panel {
    flex: 1;
    border: 1px solid #e4e7ed;
    border-radius: 8px;
    background: #fff;
    padding: 20px;
    overflow-y: auto;
    
    &.empty {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      h3 { margin: 0; font-size: 16px; }
    }
    
    .stage-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 16px 0;
    }
    
    .pipeline-steps {
      margin: 20px 0;
    }
    
    .stage-card {
      margin-bottom: 16px;
      &.stage-current { border: 1px solid #409eff; box-shadow: 0 0 8px rgba(64,158,255,.25); }
      
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
      
      .sub-block {
        margin-bottom: 12px;
        h5 { margin: 6px 0; color: #409eff; }
      }
    }
  }
}
</style>
