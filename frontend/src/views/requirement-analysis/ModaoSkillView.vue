<template>
  <div class="modao-skill">
    <div class="page-header">
      <h2>🧩 墨刀需求读取与用例生成</h2>
      <p class="subtitle">5 阶段引导式工作流：原型读取 → 需求通读 → 需求澄清 → 用例设计 → 冒烟执行</p>
      <div class="header-actions">
        <el-button size="small" @click="loadHistory" :loading="loadingHistory">刷新历史</el-button>
        <el-button v-if="protoId" size="small" type="success" plain @click="goCanvasBoard">🗂️ 画布任务看板</el-button>
        <el-button size="small" type="primary" plain @click="onNew">+ 新建流程</el-button>
      </div>
    </div>

    <div class="canvas-page-head">
      <el-button size="small" plain @click="goBackList">← 返回任务列表</el-button>
      <span v-if="proto" class="canvas-page-title">{{ proto.title || ('任务 #' + protoId) }}</span>
      <span v-else class="canvas-page-title">新建需求任务</span>
    </div>

    <el-steps :active="step" align-center finish-status="success" class="steps">
      <el-step title="原型接入" description="墨刀逐页读取" />
      <el-step title="需求通读" description="结构化摘要" />
      <el-step title="需求澄清" description="识别模糊/缺失/边界" />
      <el-step title="用例设计" description="测试点/风险/PCI/合并/用例" />
      <el-step title="冒烟执行" description="执行决策" />
    </el-steps>

    <!-- 阶段0：原型接入 -->
    <el-card v-if="step === 0" class="stage-card">
      <template #header><b>阶段0 · 原型接入</b></template>
      <el-form :model="form" label-width="110px">
        <el-form-item label="需求来源">
          <el-radio-group v-model="form.source_type">
            <el-radio value="modao">墨刀原型链接</el-radio>
            <el-radio value="webshare">webshare 页面树链接</el-radio>
            <el-radio value="text">需求文本/文档</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.source_type === 'modao'" label="需求地址">
          <el-input v-model="form.url" type="textarea" :rows="4"
            placeholder="每行一个墨刀原型链接。例如：&#10;https://modao.cc/proto/xxxx/sharing?view_mode=read_only&#10;（若是 webshare/原型页面树地址，请选择上方「webshare 页面树链接」并粘贴到对应框；系统也会自动识别）" />
          <div class="form-hint">💡 每个页面地址一行。多个地址会逐个抓取并合并；若只填<strong>一个目录地址</strong>，将尝试自动发现该目录下的所有页面（依赖原型页面的页面树结构）。</div>
        </el-form-item>
        <el-form-item v-if="form.source_type === 'webshare'" label="webshare 页面树链接">
          <el-input v-model="form.webshare_url" type="textarea" :rows="4"
            placeholder="每行一个 webshare 目录/页面树地址，系统将自动枚举该目录下的所有页面。例如：&#10;http://192.168.1.80/webshare/WNLProducts/01%20需求原型/50%20其他/AI搜索解答/" />
          <div class="form-hint">💡 webshare 为 hash 路由原型：填写目录地址后，系统自动识别左侧页面树并逐个读取所有子页面（best-effort）。若页面树未被识别，可改为把每个页面地址一行粘贴（同样支持）。</div>
        </el-form-item>
        <el-form-item v-if="form.source_type === 'modao'" label="登录Cookie">
          <el-input v-model="form.cookies" type="textarea" :rows="3"
            placeholder="可选：粘贴导出的 cookie JSON 以绕过登录墙（首次可用可见浏览器扫码导出）" />
        </el-form-item>
        <el-form-item v-if="form.source_type === 'text'" label="需求文本">
          <el-input v-model="form.requirement_text" type="textarea" :rows="8"
            placeholder="直接粘贴需求正文" />
        </el-form-item>
        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="任务标题" />
        </el-form-item>
        <el-form-item label="归属项目">
          <div style="display:flex;gap:8px;align-items:center;width:100%">
            <el-select v-model="form.project" placeholder="选择项目（用于同步功能模块）" filterable
              style="flex:1" :loading="loadingProjects" @change="onProjectChange">
              <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
            </el-select>
            <el-button size="small" type="primary" plain title="快速新建项目"
              @click="showQuickProject = true">+ 新建项目</el-button>
          </div>
        </el-form-item>
        <el-form-item v-if="form.project" label="关联版本">
          <div style="display:flex;gap:8px;align-items:center;width:100%">
            <el-select v-model="form.version" placeholder="选择版本（可选）" filterable style="flex:1">
              <el-option v-for="v in versions" :key="v.id" :label="v.name" :value="v.id" />
            </el-select>
            <el-button size="small" type="primary" plain title="快速新建版本"
              @click="showQuickVersion = true">+ 新建版本</el-button>
          </div>
        </el-form-item>
        <el-form-item v-if="form.source_type !== 'text'" label="双路融合">
          <el-switch v-model="form.dual_fusion" />
          <div class="form-hint">默认开启。DOM 与视觉分别逐页提取，再由 AI 分批交叉融合；任一路覆盖不足会阻断后续分析，避免生成不完整用例。</div>
        </el-form-item>
      </el-form>
      <el-alert v-if="proto && proto.status === 'failed'" type="error" :title="friendlyError(proto.error_message)"
        :description="proto.error_message" show-icon />
      <!-- 部分成功：读取超时但已保留已读画布。提示可继续读取补齐，而不是要求从头重跑。 -->
      <el-alert v-if="proto && proto.status === 'partial'" type="warning" :title="'读取未完成（已保留部分画布）'"
        :description="proto.error_message" show-icon />
      <el-button type="primary" :loading="busy" :disabled="isRunning || busy" @click="protoId ? onReExtract() : onCreate()">{{ protoId ? (proto.status === 'partial' ? '继续读取（补齐剩余画布）' : '重新读取当前任务') : form.source_type === 'modao' ? '开始读取墨刀' : form.source_type === 'webshare' ? '开始读取 webshare 页面树' : '保存需求文本' }}</el-button>
      <el-button v-if="proto && proto.status === 'extracting'" :loading="true">读取中…</el-button>
      <el-button v-if="proto && proto.status === 'extracting'" size="small" type="danger" plain :loading="terminating" @click="onTerminate">终止任务</el-button>
      <el-button v-if="proto && ['extracted', 'partial'].includes(proto.status) && proto.current_stage >= 0"
        type="success" @click="enterCanvasReview">✅ 已读取，校验画布模块</el-button>
      <el-button v-if="proto && canvasPages.length" size="small" type="warning" plain
        @click="openCanvasManage">✏️ 管理画布</el-button>
    </el-card>

    <!-- 画布/模块人工校验（阶段0读取后、需求结构化前）。
         运行中(structuring/clarifying/designing)也保持可见，便于选取/编辑其他画布；
         仅取消勾选已确认、或没有画布时隐藏。 -->
    <el-card v-if="proto && !canvasConfirmed && canvasPages.length && ['pending', 'extracted', 'partial', 'structuring', 'clarifying', 'designing', 'done'].includes(proto.status)"
      class="stage-card canvas-review-card">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <b>画布模块校验 · 选择参与需求结构化的画布</b>
          <el-button v-if="step > 0" size="small" type="primary" plain @click="canvasConfirmed = true; goStep(1)">↩ 跳过校验</el-button>
        </div>
      </template>
      <el-alert type="info" :closable="false" show-icon
        title="逐一核对每个画布的识别结果，取消勾选不需要的画布，或标记识别准确率。仅勾选的画布会进入需求结构化。" />
      <el-alert v-if="canContinueCanvas" type="warning" :closable="false" show-icon
        :title="`还有 ${incompleteCanvasCount} 个画布未走完所有阶段`"
        :description="'可点击「管理画布」勾选需继续的画布，再回到对应阶段点击「运行」按钮补跑；已完成的画布会自动跳过，不会重复消耗模型。'" />
      <div class="canvas-review-body">
        <!-- 左侧：画布模块列表 -->
        <div class="canvas-list">
          <div class="canvas-list-toolbar">
            <el-checkbox v-model="checkAll" :indeterminate="canvasIndeterminate" @change="toggleSelectAll">全选</el-checkbox>
            <span class="canvas-count">已选 {{ canvasSelectedCount }}/{{ canvasPages.length }}</span>
          </div>
          <div
            v-for="(c, idx) in canvasPages"
            :key="c.id"
            class="canvas-card"
            :class="{ active: canvasActiveId === c.id, unchecked: !c.selected, 'auto-filtered': isAutoFiltered(c) }"
            @click="canvasActiveId = c.id">
            <div class="canvas-card-head">
              <el-checkbox v-model="c.selected" @click.stop
                @update:model-value="onCanvasToggle(c)" />
              <span class="canvas-no">{{ String(idx + 1).padStart(2, '0') }}</span>
              <el-input v-model="c.page_name" size="small" class="canvas-name-input"
                @click.stop @input="markCanvasDirty(c)" placeholder="画布名" />
              <el-input v-model="c.parent_name" size="small" class="canvas-parent-input"
                @click.stop @input="markCanvasDirty(c)" placeholder="父级分组(可选)" />
              <el-tag v-if="c.duplicate_of_page" size="mini" type="info">重复</el-tag>
              <el-tag v-if="isAutoFiltered(c)" size="mini" type="info" class="auto-filter-tag">自动过滤</el-tag>
            </div>
            <div v-if="isAutoFiltered(c)" class="canvas-auto-filter-reason">{{ c.recognition_note }}</div>
            <div class="canvas-card-meta">
              <el-tag v-if="!c.visionAttempted" size="mini" type="warning">未视觉识别</el-tag>
              <el-tag v-else-if="c.visionSuccess" size="mini" type="success">视觉✓</el-tag>
              <el-tag v-else size="mini" type="danger">视觉✗</el-tag>
              <el-tag v-if="c.warnings && c.warnings.length" size="mini" type="warning">
                警告{{ c.warnings.length }}</el-tag>
              <el-tag size="mini" type="info">文本{{ (c.text || '').length }}字</el-tag>
              <el-tag size="mini" :type="canvasStageType(c)" effect="dark">画布·{{ canvasStageLabel(c) }}</el-tag>
            </div>
            <!-- 画布级流水线进度条（A2-b）：显示该画布已走到哪一步 -->
            <div class="canvas-card-progress" @click.stop>
              <el-steps :active="c.current_stage" align-center finish-status="success" size="mini">
                <el-step title="读取" />
                <el-step title="结构化" />
                <el-step title="澄清" />
                <el-step title="用例" />
                <el-step title="冒烟" />
              </el-steps>
            </div>
            <div class="canvas-card-accuracy" @click.stop>
              <el-select v-model="c.recognition_note" size="small" placeholder="识别准确率" @change="markCanvasDirty(c)">
                <el-option label="准确" value="准确" />
                <el-option label="基本准确" value="基本准确" />
                <el-option label="偏差较大" value="偏差较大" />
                <el-option label="需重读" value="需重读" />
              </el-select>
              <el-dropdown trigger="click" @command="cmd => onPageReset(c, cmd)" :disabled="isRunning">
                <el-button size="mini" type="warning" plain>↩ 回退 ▼</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="1">回退到「读取」(重读此画布)</el-dropdown-item>
                    <el-dropdown-item command="2">回退到「结构化」</el-dropdown-item>
                    <el-dropdown-item command="3">回退到「澄清」</el-dropdown-item>
                    <el-dropdown-item command="4">回退到「用例」</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-button size="mini" type="danger" plain @click="onCanvasDelete(c)">删除</el-button>
            </div>
          </div>
        </div>
        <!-- 右侧：画布预览 -->
        <div class="canvas-preview" v-if="canvasActive">
          <div class="canvas-preview-head">
            <b>{{ canvasFullName(canvasActive) }}</b>
            <span class="canvas-preview-no">第{{ canvasActive.page_no }}页</span>
          </div>
          <el-image
            v-if="activeShot"
            :src="mediaUrl(activeShot)"
            :preview-src-list="activeShots.map(s => mediaUrl(s))"
            fit="contain"
            class="canvas-preview-img"
            alt="画布截图" />
          <div v-else class="canvas-preview-noshot">无截图</div>
          <!-- 超长画布分段缩略图：可切换查看每一段 -->
          <div v-if="activeShots.length > 1" class="canvas-preview-segs">
            <div
              v-for="(s, si) in activeShots"
              :key="si"
              class="canvas-preview-seg"
              :class="{ active: si === activeShotIdx }"
              @click="activeShotIdx = si">
              <el-image :src="mediaUrl(s)" fit="cover" class="canvas-preview-seg-img" />
              <span class="canvas-preview-seg-no">{{ si + 1 }}</span>
            </div>
          </div>
          <div class="canvas-preview-text">
            <div class="canvas-preview-subtitle">识别文本</div>
            <pre class="md-box">{{ canvasActive.text || '（无文本）' }}</pre>
            <div v-if="canvasActive.annotations && canvasActive.annotations.length" class="canvas-preview-subtitle">注释/标注</div>
            <pre v-if="canvasActive.annotations && canvasActive.annotations.length" class="md-box">{{ canvasActive.annotations }}</pre>
            <div v-if="canvasActive.warnings && canvasActive.warnings.length" class="canvas-preview-subtitle">提取警告</div>
            <pre v-if="canvasActive.warnings && canvasActive.warnings.length" class="md-box warn">{{ canvasActive.warnings.join('\n') }}</pre>
          </div>
        </div>
        <div class="canvas-preview empty" v-else>请选择左侧画布查看预览</div>
      </div>
      <div class="canvas-review-actions">
        <el-button type="primary" :loading="canvasSaving" @click="saveCanvasReview">
          {{ isRunning ? '保存画布选择（流程运行中，重跑后生效）' : '保存校验结果并进入需求结构化' }}
        </el-button>
        <el-button @click="resetCanvasReview">重置</el-button>
      </div>
    </el-card>

    <!-- 阶段1：需求通读 -->
    <el-card v-if="step === 1 || (proto && proto.requirement_summary)" class="stage-card">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <b>阶段1 · 需求通读</b>
          <el-button v-if="step > 1" size="small" type="primary" plain @click="goStep(1)">↩ 回到此阶段</el-button>
          <el-button v-if="canvasPages.length" size="small" type="warning" plain @click="openCanvasManage">✏️ 管理画布</el-button>
        </div>
      </template>
      <el-button type="primary" :loading="busy" :disabled="isRunning || busy" @click="onStruct">运行需求结构化</el-button>
      <el-button v-if="proto && proto.requirement_summary" size="small" :loading="busy" :disabled="isRunning || busy" @click="onReStructAll">重新结构化全部</el-button>
      <el-alert v-if="proto && proto.status === 'structuring'" type="info" title="结构化中…" />
      <el-button v-if="proto && proto.status === 'structuring'" size="small" type="danger" plain :loading="terminating" @click="onTerminate">终止任务</el-button>
      <el-alert v-if="proto && proto.status === 'failed'" type="error" :title="friendlyError(proto.error_message)" :description="proto.error_message" show-icon />
      <el-alert v-if="proto && qualityGateSummary.length" type="warning" :closable="false" show-icon
        title="质量门禁提示（以下检查未通过，已保留用例/报告）"
        :description="qualityGateSummary.join('\n')" />
      <div v-if="proto && proto.requirement_summary" class="artifact">
        <div class="artifact-head">
          <h4>需求摘要</h4>
          <div class="artifact-actions">
            <el-radio-group
              v-if="editingField !== 'requirement_summary'"
              v-model="stage1ViewMode"
              size="small"
              class="stage1-view-toggle"
            >
              <el-radio-button label="visual">👁️ 可视化</el-radio-button>
              <el-radio-button label="raw">📜 YAML</el-radio-button>
            </el-radio-group>
            <el-button size="small" plain @click="beginEdit('requirement_summary')"
              :disabled="editingField && editingField !== 'requirement_summary'">✏️ 编辑</el-button>
            <el-button size="small" plain type="info"
              @click="onPreviewXmind" title="预览将要导出的 XMind 结构">👁️ 预览 XMind</el-button>
            <el-button size="small" plain type="warning" :loading="xmindExporting"
              @click="onExportXmind" title="导出可编辑的 XMind 图，便于精准理解需求">⬇️ 下载 XMind</el-button>
            <el-button size="small" plain type="warning" :loading="xmindImporting"
              @click="triggerXmindUpload" title="上传本地修改后的 XMind，覆盖结构化摘要（不联动下游）">⬆️ 上传 XMind</el-button>
            <input ref="xmindFile" type="file" accept=".xmind" style="display:none"
              @change="onXmindFileChange" />
          </div>
        </div>
        <el-dialog title="XMind 预览（将要导出的结构）" :visible.sync="xmindPreviewVisible" width="560px"
          @closed="xmindPreviewTree = []">
          <el-tree v-if="xmindPreviewTree.length" :data="xmindPreviewTree" :props="{ label: 'label', children: 'children' }"
            node-key="label" default-expand-all :expand-on-click-node="false" class="xmind-preview-tree" />
          <p v-else class="xmind-preview-empty">暂无可预览的内容</p>
          <div slot="footer">
            <el-button size="small" @click="xmindPreviewVisible = false">关闭</el-button>
            <el-button size="small" type="warning" :loading="xmindExporting" @click="onExportXmind">确认无误，下载</el-button>
          </div>
        </el-dialog>
        <div v-if="editingField !== 'requirement_summary'" class="stage1-summary-container">
          <RequirementSummaryViewer v-if="stage1ViewMode === 'visual'" :raw="proto.requirement_summary" />
          <pre v-else class="md-box">{{ proto.requirement_summary }}</pre>
        </div>
        <template v-else>
          <el-input v-model="editBuffer" type="textarea" :rows="12" />
          <div class="edit-actions">
            <el-button size="small" type="primary" @click="saveEdit">保存修改</el-button>
            <el-button size="small" @click="cancelEdit">取消</el-button>
          </div>
        </template>
        <modao-qa :proto-id="protoId" :stage="1" :qa-log="qaLogFor(1)" />
      </div>
      <el-alert v-if="proto && proto.feature_module_info" type="success" :closable="false"
        :title="`已同步功能模块：${proto.feature_module_info.name}`" show-icon />
      <el-button v-if="proto && proto.requirement_summary" type="success" @click="onConfirm(1)">确认摘要，进入阶段2</el-button>
    </el-card>

    <!-- 阶段2：需求澄清（确认后也保留入口，可随时回看/修改，默认折叠） -->
    <el-card v-if="step === 2 || (proto && proto.clarification_log)" class="stage-card">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <b>阶段2 · 需求澄清
            <el-tag v-if="stage2Confirmed" size="mini" type="success">已确认</el-tag>
          </b>
          <div style="display:flex;align-items:center;gap:8px">
            <el-button v-if="step > 2" size="small" type="primary" plain @click="goStep(2)">↩ 回到此阶段</el-button>
            <el-button size="small" plain @click="stage2Collapsed = !stage2Collapsed">
              {{ stage2Collapsed ? '▼ 展开' : '▲ 收起' }}
            </el-button>
          </div>
        </div>
      </template>
      <div v-show="!stage2Collapsed">
      <el-button type="primary" :loading="busy" :disabled="isRunning || busy" @click="onClarify">识别澄清问题</el-button>
      <el-alert v-if="proto && proto.status === 'clarifying'" type="info" title="澄清分析中…" />
      <el-button v-if="proto && proto.status === 'clarifying'" size="small" type="danger" plain :loading="terminating" @click="onTerminate">终止任务</el-button>
      <el-alert v-if="proto && proto.status === 'failed'" type="error" :title="friendlyError(proto.error_message)" :description="proto.error_message" show-icon />
      <el-alert v-if="proto && qualityGateSummary.length" type="warning" :closable="false" show-icon
        title="质量门禁提示（以下检查未通过，已保留用例/报告）"
        :description="qualityGateSummary.join('\n')" />
      <div v-if="proto && proto.clarification_log" class="artifact">
        <div class="artifact-head">
          <h4>澄清清单</h4>
          <el-button size="small" plain @click="toggleRawEdit"
            :disabled="editingField && editingField !== 'clarification_log'">
            {{ editingField === 'clarification_log' ? '✏️ 编辑逐条' : '✏️ 编辑原文' }}
          </el-button>
        </div>

        <!-- 逐条填写模式 -->
        <template v-if="clarifyParsed && editingField !== 'clarification_log'">
          <template v-if="clarifyItems.length">
            <div v-for="(item, i) in clarifyItems" :key="i" class="clarify-item">
              <div class="clarify-item-head">
                <div class="clarify-head-left">
                  <el-tag size="mini" :type="typeTag(item.type)">{{ item.type || '问题' }}</el-tag>
                  <el-tag v-if="item.priority" size="mini" type="warning">{{ item.priority }}</el-tag>
                  <span v-if="item.location" class="clarify-loc">📍 {{ item.location }}</span>
                </div>
                <el-button size="mini" type="danger" plain class="clarify-delete-btn" @click="deleteClarifyItem(i)">🗑️ 删除</el-button>
              </div>
              <div class="clarify-issue">{{ item.issue }}</div>
              <div v-if="item.suggested_question" class="clarify-suggest">
                <b>建议提问：</b>{{ item.suggested_question }}
              </div>
              <div v-if="item.impact" class="clarify-impact">影响范围：{{ item.impact }}</div>
              <el-input v-model="clarifyReplies[i]" type="textarea" :rows="2" class="mt"
                :placeholder="item.resolution ? '' : '填写与产品确认后的回复/结论…'" />
            </div>
            <el-button size="small" type="primary" class="mt"
              @click="saveClarifyReplies">💾 保存全部回复</el-button>
          </template>
          <el-empty v-else description="暂无澄清项（已确认无需澄清或已删除）" :image-size="60" />
        </template>

        <!-- 原文/未解析模式 -->
        <template v-else>
          <pre v-if="editingField !== 'clarification_log'" class="md-box">{{ proto.clarification_log }}</pre>
          <template v-else>
            <el-input v-model="editBuffer" type="textarea" :rows="12" />
            <div class="edit-actions">
              <el-button size="small" type="primary" @click="saveEdit">保存修改</el-button>
              <el-button size="small" @click="cancelEdit">取消</el-button>
            </div>
          </template>
        </template>
        <modao-qa :proto-id="protoId" :stage="2" :qa-log="qaLogFor(2)" />
      </div>
      <el-alert v-if="clarificationUnresolvedCount > 0" type="warning" :closable="false" show-icon
        :title="`还有 ${clarificationUnresolvedCount} 条位置/需求问题未填写人工结论，不能进入最终用例生成流程`" />
      <el-button v-if="proto && proto.clarification_log && !stage2Confirmed" type="success"
        :disabled="clarificationUnresolvedCount > 0" @click="onConfirm(2)">已与产品确认，进入阶段3</el-button>
      <el-alert v-else-if="stage2Confirmed" type="success" :closable="false" show-icon
        title="阶段2 已确认。澄清内容仍可在上方逐条修改 / 编辑原文；修改后原生成批准会失效，需重新批准生成用例。" />
      </div>
    </el-card>

    <!-- 阶段3：用例设计 -->
    <el-card v-if="step === 3 || (proto && (proto.final_testpoints_json || proto.module_split || proto.risks_json || proto.pci_json))" class="stage-card">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
          <b>阶段3 · 用例设计（含风险/PCI/三路合并/质量自检）</b>
          <div style="display:flex;align-items:center;gap:8px">
            <el-button v-if="step > 3" size="small" type="primary" plain @click="goStep(3)">↩ 回到此阶段</el-button>
            <el-button size="small" type="primary" plain @click="openEditDialog()">✏️ 编辑产物</el-button>
          </div>
        </div>
      </template>
      <div class="stage3-actions">
        <el-button type="primary" :loading="busy" :disabled="isRunning || busy" @click="onDesign">① 运行测试点合并</el-button>
        <el-button v-if="proto && (proto.status === 'testpoints_ready' || proto.status === 'done') && proto.final_testpoints_json"
          type="success" :loading="busy" @click="onGenerateCases">② 确认测试点，生成测试用例</el-button>
      </div>
      <el-alert v-if="proto && proto.status === 'designing'" type="info" :closable="false" show-icon
        title="AI 处理中（测试点合并 或 用例生成）…请稍候" />
      <el-button v-if="proto && proto.status === 'designing'" size="small" type="danger" plain :loading="terminating" @click="onTerminate">终止任务</el-button>
      <el-alert v-if="proto && proto.status === 'testpoints_ready'" type="warning" :closable="false" show-icon
        title="第一段完成：测试点已合并。请在下方「合并测试点」中查看/编辑确认后，点击「② 确认测试点，生成测试用例」" />
      <el-alert v-if="proto && proto.final_testpoints_json && generationGateReasons.length" type="warning"
        :closable="false" show-icon title="最终测试用例尚未获准生成"
        :description="generationGateReasons.join('；')" />
      <el-alert v-if="proto && proto.status === 'failed'" type="error" :title="friendlyError(proto.error_message)" :description="proto.error_message" show-icon />
      <el-alert v-if="proto && qualityGateSummary.length" type="warning" :closable="false" show-icon
        title="质量门禁提示（以下检查未通过，已保留用例/报告）"
        :description="qualityGateSummary.join('\n')" />

      <!-- 产物筛选标签页 -->
      <div v-if="hasAnyArtifact" class="stage3-filter-bar">
        <el-radio-group v-model="stage3ActiveTab" size="small" class="stage3-tabs">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button v-if="proto && proto.module_split" value="module_split">模块拆分</el-radio-button>
          <el-radio-button v-if="proto && proto.risks_json" value="risks_json">风险识别</el-radio-button>
          <el-radio-button v-if="proto && proto.pci_json" value="pci_json">待确认问题 PCI</el-radio-button>
          <el-radio-button v-if="proto && proto.final_testpoints_json" value="final_testpoints_json">合并测试点</el-radio-button>
          <el-radio-button v-if="proto && proto.testcases_json" value="testcases_json">测试用例</el-radio-button>
          <el-radio-button v-if="proto && proto.smoke_json" value="smoke_json">冒烟用例</el-radio-button>
          <el-radio-button v-if="proto && proto.quality_report_json" value="quality_report_json">质量报告</el-radio-button>
        </el-radio-group>
      </div>

      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'module_split') && proto && proto.module_split" class="artifact">
        <div class="artifact-head-row">
          <h4>模块拆分</h4>
          <el-button size="mini" type="text" @click="openEditDialog('module_split')">✏️ 编辑</el-button>
        </div>
        <pre class="md-box">{{ proto.module_split }}</pre>
      </div>
      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'risks_json') && proto && proto.risks_json" class="artifact">
        <div class="artifact-head-row">
          <h4>风险识别 (R1~R5) <el-tag size="mini" type="info">{{ mainPageParsedRisks.length }} 条</el-tag></h4>
          <el-button size="mini" type="text" @click="openEditDialog('risks_json')">✏️ 编辑</el-button>
        </div>
        <!-- 结构化卡片 -->
        <div v-if="mainPageParsedRisks.length" class="main-page-risk-list">
          <div v-for="(risk, i) in mainPageParsedRisks" :key="'mr'+i" class="main-page-risk-card">
            <div class="main-page-card-head">
              <el-tag v-if="risk._module" size="mini" type="success" effect="plain">{{ risk._module }}</el-tag>
              <span class="generic-card-index">{{ risk.id || ('R' + (i+1)) }}</span>
              <el-tag size="mini" :type="priTag(risk.impact)">{{ risk.impact || '-' }}</el-tag>
              <el-tag v-if="risk.category" size="mini" type="info">{{ risk.category }}</el-tag>
            </div>
            <div class="main-page-card-desc">{{ risk.description || '' }}</div>
            <div v-if="risk.related_testpoints && risk.related_testpoints.length" class="main-page-card-extra">
              <b>关联测试点：</b>{{ risk.related_testpoints.join(', ') }}
            </div>
            <div v-if="riskAnswer(risk)" class="pci-sub-a-wrap risk-mitigation-wrap">
              <label class="pci-sub-a-label">已确认处置：</label>
              <div class="pci-sub-a-text">{{ riskAnswer(risk) }}</div>
            </div>
          </div>
        </div>
        <pre v-else class="json-box">{{ pretty(proto.risks_json) }}</pre>
      </div>
      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'pci_json') && proto && proto.pci_json" class="artifact">
        <div class="artifact-head-row">
          <h4>待确认问题 PCI (Q1~Q5) <el-tag size="mini" type="warning">{{ mainPageFlatPci.length }} 个子问题</el-tag></h4>
          <div style="display:flex;align-items:center;gap:8px;margin-left:auto">
            <span class="pci-stat-inline">筛选：</span>
            <el-radio-group v-model="mainPagePciFilter" size="mini">
              <el-radio-button value="all">全部 ({{ mainPagePciStats.all }})</el-radio-button>
              <el-radio-button value="blocker">blocker ({{ mainPagePciStats.blocker }})</el-radio-button>
              <el-radio-button value="high">high ({{ mainPagePciStats.high }})</el-radio-button>
              <el-radio-button value="medium">medium ({{ mainPagePciStats.medium }})</el-radio-button>
              <el-radio-button value="low">low ({{ mainPagePciStats.low }})</el-radio-button>
            </el-radio-group>
            <el-button size="mini" type="text" @click="openEditDialog('pci_json')">✏️ 编辑</el-button>
          </div>
        </div>
        <!-- 结构化卡片：每个子问题一张卡 -->
        <div v-if="mainPageFlatPci.length" class="main-page-pci-list">
          <div v-for="(item, idx) in mainPageFlatPci" :key="'mp'+item.pciIndex+'_'+item.subIndex"
               class="main-page-pci-card">
            <div class="main-page-card-head">
              <el-tag v-if="item.pci._module" size="mini" type="success" effect="plain">{{ item.pci._module }}</el-tag>
              <span class="generic-card-index">{{ item.pci.id || ('#' + (item.pciIndex+1)) }}</span>
              <el-tag size="mini" :type="priTag(item.pci.impact)">{{ item.pci.impact || '-' }}</el-tag>
              <el-tag size="mini" :type="item.pci.type === 'Q2' ? 'danger' : 'warning'">{{ item.pci.type || 'Q' }}</el-tag>
              <span class="pci-sub-qbadge">Q{{ item.subIndex + 1 }}</span>
            </div>
            <div v-if="item.pci.blocked_scenarios && item.pci.blocked_scenarios.length" class="main-page-card-extra">
              <b>🚫 阻塞场景：</b>
              <el-tag v-for="(bs, bi) in item.pci.blocked_scenarios" :key="bi" size="mini" class="ml-xs">{{ bs }}</el-tag>
            </div>
            <div class="pci-sub-q"><span class="pci-q-badge">问</span>{{ item.question }}</div>
            <div v-if="item.answer" class="pci-sub-a-wrap">
              <label class="pci-sub-a-label">A：</label>
              <div class="pci-sub-a-text">{{ item.answer }}</div>
            </div>
          </div>
        </div>
        <pre v-else class="json-box">{{ pretty(proto.pci_json) }}</pre>
      </div>
      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'final_testpoints_json') && proto && proto.final_testpoints_json" class="artifact">
        <div class="artifact-head-row">
          <h4>合并去重后测试点 <el-tag size="mini" type="info">{{ mainPageTestpoints.length }} 条</el-tag></h4>
          <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
            <el-button size="mini" type="text" @click="openEditDialog('final_testpoints_json')">✏️ 编辑</el-button>
            <el-button v-if="proto && proto.status !== 'designing'" size="mini" type="success"
              :loading="busy" @click="onGenerateCases">✅ 确认测试点，生成用例</el-button>
          </div>
        </div>
        <div v-if="mainPageTestpoints.length" class="main-page-tp-list">
          <div v-for="(tp, i) in mainPageTestpoints" :key="'mtp'+i" class="main-page-tp-card">
            <div class="main-page-card-head">
              <span class="generic-card-index">{{ i + 1 }}</span>
              <b v-if="tp.title">{{ tp.title }}</b>
              <el-button size="mini" type="danger" plain style="margin-left:auto"
                :loading="removingTpIndex === i" @click="removeMainTpItem(i)">🗑 删除</el-button>
            </div>
            <div class="main-page-card-desc">{{ tp.description || tp.issue || '' }}</div>
          </div>
        </div>
        <pre v-else class="json-box">{{ pretty(proto.final_testpoints_json) }}</pre>
      </div>
      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'testcases_json') && proto && proto.testcases_json" class="artifact">
        <div class="artifact-head-row">
          <h4>测试用例（{{ mainPageTestcases.total }} 条）</h4>
          <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
            <el-button size="mini" type="text" @click="openEditDialog('testcases_json')">✏️ 编辑</el-button>
            <el-button size="mini" type="success" :loading="adoptLoading"
              @click="adoptToLibrary(false)">📥 采纳到用例库</el-button>
          </div>
        </div>
        <div v-if="mainPageTestcases.groups.length" class="main-page-tc-wrap">
          <div v-for="(group, gi) in mainPageTestcases.groups" :key="'mtc'+gi" class="main-page-tc-group">
            <h5 class="main-page-tc-group-title">📦 {{ group.module }}（{{ group.items.length }} 条）</h5>
            <div v-for="(tc, ti) in group.items" :key="ti" class="main-page-tc-card">
              <div class="main-page-card-head">
                <span class="generic-card-index">{{ tc.id || ('#' + (ti+1)) }}</span>
                <b>{{ tc.title || ('用例 #' + (ti+1)) }}</b>
                <el-tag size="mini" :type="priTag(tc.priority)">{{ tc.priority || '-' }}</el-tag>
                <el-tag size="mini" :type="tc.type === 'UI' ? '' : 'warning'">{{ tc.type || '功能' }}</el-tag>
                <el-button v-if="!adoptedCaseMap['tc-'+gi+'-'+ti]" size="extra-mini" type="success" plain
                  @click.stop="adoptSingleCase(tc._rawIndex !== undefined ? tc._rawIndex : tc.index ?? ti, 'tc-'+gi+'-'+ti)">📥 采纳</el-button>
                <span v-else class="adopted-badge">✅ 已采纳</span>
              </div>
              <el-alert v-if="tc.steps_missing || tc.steps === '【步骤缺失，请补充操作步骤】'" type="error" :closable="false" show-icon title="步骤缺失，请补充操作步骤" style="margin-bottom:8px" />
              <div v-if="tc.precondition || tc.preconditions" class="main-page-card-extra"><b>前置：</b>{{ tc.precondition || tc.preconditions }}</div>
              <ol v-if="Array.isArray(tc.steps) && tc.steps.length" class="main-page-tc-steps"><li v-for="(s, si) in tc.steps" :key="si">{{ s }}</li></ol>
              <div v-else-if="tc.steps" class="main-page-card-extra"><b>步骤：</b>{{ tc.steps }}</div>
              <div v-if="tc.expected" class="main-page-card-extra"><b>预期：</b><span class="tc-expected">{{ tc.expected }}</span></div>
            </div>
          </div>
        </div>
        <pre v-else class="json-box">{{ pretty(proto.testcases_json) }}</pre>
      </div>
      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'smoke_json') && proto && proto.smoke_json" class="artifact">
        <div class="artifact-head-row">
          <h4>冒烟用例（{{ mainPageSmoke.total }} 条）</h4>
          <el-button size="mini" type="text" @click="openEditDialog('smoke_json')">✏️ 编辑</el-button>
        </div>
        <div v-if="mainPageSmoke.groups.length" class="main-page-tc-wrap">
          <div v-for="(group, gi) in mainPageSmoke.groups" :key="'msm'+gi" class="main-page-tc-group">
            <h5 class="main-page-tc-group-title">📦 {{ group.module }}（{{ group.items.length }} 条）</h5>
            <div v-for="(tc, ti) in group.items" :key="ti" class="main-page-tc-card">
              <div class="main-page-card-head">
                <span class="generic-card-index">{{ tc.id || ('#' + (ti+1)) }}</span>
                <b>{{ tc.title || ('用例 #' + (ti+1)) }}</b>
                <el-tag size="mini" :type="priTag(tc.priority)">{{ tc.priority || '-' }}</el-tag>
                <el-tag size="mini" :type="tc.type === 'UI' ? '' : 'warning'">{{ tc.type || '功能' }}</el-tag>
                <el-tag v-if="tc.is_smoke" size="mini" type="success">冒烟</el-tag>
                <el-button v-if="!adoptedCaseMap['sm-'+gi+'-'+ti]" size="extra-mini" type="success" plain
                  @click.stop="adoptSingleCase(tc._rawIndex !== undefined ? tc._rawIndex : tc.index ?? ti, 'sm-'+gi+'-'+ti)">📥 采纳</el-button>
                <span v-else class="adopted-badge">✅ 已采纳</span>
              </div>
              <div v-if="tc.precondition || tc.preconditions" class="main-page-card-extra"><b>前置：</b>{{ tc.precondition || tc.preconditions }}</div>
              <ol v-if="Array.isArray(tc.steps) && tc.steps.length" class="main-page-tc-steps"><li v-for="(s, si) in tc.steps" :key="si">{{ s }}</li></ol>
              <div v-else-if="tc.steps" class="main-page-card-extra"><b>步骤：</b>{{ tc.steps }}</div>
              <div v-if="tc.expected" class="main-page-card-extra"><b>预期：</b><span class="tc-expected">{{ tc.expected }}</span></div>
            </div>
          </div>
        </div>
        <pre v-else class="json-box">{{ pretty(proto.smoke_json) }}</pre>
      </div>
      <div v-if="(stage3ActiveTab === 'all' || stage3ActiveTab === 'quality_report_json') && proto && proto.quality_report_json" class="artifact">
        <div class="artifact-head-row">
          <h4>质量自检报告</h4>
          <el-button size="mini" type="text" @click="openEditDialog('quality_report_json')">✏️ 编辑</el-button>
        </div>
        <div v-if="mainPageQuality" class="main-page-quality">
          <div v-for="(val, key) in mainPageQuality" :key="key" class="main-page-quality-row">
            <span class="main-page-quality-key">{{ qualityKeyLabel(key) }}</span>
            <span v-if="typeof val === 'object'" class="main-page-quality-val">
              <pre class="quality-json">{{ typeof val === 'string' ? val : JSON.stringify(val, null, 2) }}</pre>
            </span>
            <span v-else class="main-page-quality-val">{{ val }}</span>
          </div>
        </div>
        <pre v-else class="json-box">{{ pretty(proto.quality_report_json) }}</pre>
      </div>

      <div v-if="proto && proto.excel_path" class="artifact">
        <el-button type="warning" @click="onDownload">⬇️ 下载 Excel（按模块分 sheet）</el-button>
      </div>
      <modao-qa v-if="proto && proto.testcases_json" :proto-id="protoId" :stage="3" :qa-log="qaLogFor(3)" />
      <el-alert v-if="proto && proto.feature_module_info" type="info" :closable="false"
        title="确认后将自动把各子模块、测试点、用例同步到功能模块体系（阶段1 顶层模块的子模块）" show-icon />
      <el-alert v-else-if="proto && proto.status === 'done' && !proto.project" type="warning" :closable="false"
        title="未关联项目/版本，确认后不会同步功能模块（可在阶段0补充项目后重跑）" show-icon />
      <el-button v-if="proto && proto.status === 'done'" type="success" @click="onConfirm(3)">确认用例，进入阶段4</el-button>

      <!-- A2-c：画布级用例采纳面板（按选中子集分别走流程，各自用例，分别/一起采纳） -->
      <div v-if="proto && proto.pages && canvasWithCases.length" class="artifact canvas-adopt-panel">
        <div class="artifact-head-row">
          <h4>🗂️ 按画布采纳用例（分别生成、可分别/一起采纳）</h4>
          <div style="margin-left:auto;display:flex;gap:8px;align-items:center">
            <el-button size="mini" type="primary" :loading="approvePagesLoading"
              @click="approveAllPages">✅ 一起采纳全部已生成画布</el-button>
          </div>
        </div>
        <div v-for="pg in canvasWithCases" :key="'cap'+pg.id" class="canvas-adopt-item">
          <div class="canvas-adopt-item-head">
            <b>📄 {{ pg.page_name }}</b>
            <span class="canvas-adopt-count">（{{ pageCaseCount(pg) }} 条用例）</span>
            <el-tag v-if="pg.page_approved" size="mini" type="success">已采纳</el-tag>
            <el-button v-else size="extra-mini" type="success" plain :loading="approvePagesLoading"
              @click.stop="approvePage(pg)">✅ 采纳此画布</el-button>
          </div>
          <div v-if="pageCaseGroups(pg).length" class="main-page-tc-wrap" style="margin-top:6px">
            <div v-for="(group, gi) in pageCaseGroups(pg)" :key="'pcg'+gi" class="main-page-tc-group">
              <h5 class="main-page-tc-group-title">📦 {{ group.module }}（{{ group.items.length }} 条）</h5>
              <div v-for="(tc, ti) in group.items" :key="ti" class="main-page-tc-card">
                <div class="main-page-card-head">
                  <span class="generic-card-index">{{ tc.id || ('#' + (ti+1)) }}</span>
                  <b>{{ tc.title || ('用例 #' + (ti+1)) }}</b>
                  <el-tag size="mini" :type="priTag(tc.priority)">{{ tc.priority || '-' }}</el-tag>
                  <el-tag size="mini" :type="tc.type === 'UI' ? '' : 'warning'">{{ tc.type || '功能' }}</el-tag>
                </div>
                <ol v-if="Array.isArray(tc.steps) && tc.steps.length" class="main-page-tc-steps"><li v-for="(s, si) in tc.steps" :key="si">{{ s }}</li></ol>
              </div>
            </div>
          </div>
        </div>
        <el-alert v-if="proto.merged_testcases_json" type="success" :closable="false" show-icon
          :title="`已采纳汇总：${mergedCaseCount} 条跨画布去重用例（merged_testcases_json）`" style="margin-top:10px" />
      </div>

      <el-divider v-if="proto && proto.testcases_json" content-position="left">一键采纳到用例库</el-divider>
      <div v-if="proto && proto.testcases_json" class="adopt-box">
        <el-alert type="info" :closable="false" show-icon
          :title="proto.project ? `将按「项目：${proto.project_name || proto.project}」自动创建功能模块并入库` : '请先在阶段0关联项目，否则无法入库'" />
        <div class="adopt-row">
          <el-checkbox v-model="adoptIncludeSmoke">同时采纳冒烟用例（自动标记「冒烟」标签）</el-checkbox>
          <el-button type="primary" :loading="adoptLoading"
            @click="adoptToLibrary(adoptIncludeSmoke)">📥 一键采纳全部用例到用例库</el-button>
        </div>
      </div>
    </el-card>

    <!-- 阶段3 产物编辑弹窗 -->
    <el-dialog title="编辑用例设计产物" v-model="editDialogVisible" width="900px" append-to-body top="3vh">
      <div class="edit-dlg-head">
        <span>选择产物：</span>
        <el-select v-model="editField" style="width: 200px">
          <el-option v-for="f in editFields" :key="f.value" :label="f.label" :value="f.value" />
        </el-select>
        <el-switch v-model="editRawMode" active-text="原文模式" inactive-text="结构化视图"
          style="margin-left:16px" />
      </div>

      <!-- 结构化视图 -->
      <div v-if="!editRawMode" class="structured-view">
        <!-- 测试用例：按模块分组卡片 -->
        <div v-if="(editField === 'testcases_json' || editField === 'smoke_json') && parsedTestcases.length">
          <div v-for="(group, gi) in parsedTestcases" :key="gi" class="tc-group">
            <h5 class="tc-group-title">📦 {{ group.module || '未分组' }}（{{ group.items.length }} 条）</h5>
            <div v-for="(tc, ti) in group.items" :key="ti" class="tc-card">
              <div class="tc-card-head">
                <span class="tc-id" v-if="tc.id">{{ tc.id }}</span>
                <b>{{ tc.title || ('用例 #' + (ti+1)) }}</b>
                <el-tag size="mini" :type="priTag(tc.priority)">{{ tc.priority || '-' }}</el-tag>
                <el-tag size="mini" :type="tc.type === 'UI' ? '' : 'warning'">{{ tc.type || '功能' }}</el-tag>
                <el-tag v-if="tc.is_smoke" size="mini" type="success">冒烟</el-tag>
              </div>
              <el-alert v-if="tc.steps_missing || tc.steps === '【步骤缺失，请补充操作步骤】'" type="error" :closable="false" show-icon title="步骤缺失，请补充操作步骤" style="margin-bottom:8px" />
              <div class="tc-card-body">
                <div v-if="tc.precondition || tc.preconditions"><b>前置条件：</b>{{ tc.precondition || tc.preconditions }}</div>
                <ol v-if="Array.isArray(tc.steps) && tc.steps.length" class="tc-steps">
                  <li v-for="(s, si) in tc.steps" :key="si">{{ s }}</li>
                </ol>
                <div v-else-if="tc.steps"><b>步骤：</b>{{ tc.steps }}</div>
                <div v-if="tc.expected"><b>预期结果：</b><span class="tc-expected">{{ tc.expected }}</span></div>
                <div v-if="tc.requirement_ref" class="tc-ref"><b>需求引用：</b>{{ tc.requirement_ref }}</div>
                <div v-if="tc.notes" class="tc-notes"><b>备注：</b>{{ tc.notes }}</div>
              </div>
              <el-input v-model="group.items[ti]._raw" type="textarea" :rows="2"
                placeholder="查看/编辑原始 JSON…" class="mt" />
            </div>
          </div>
        </div>

        <!-- 合并测试点：卡片列表 -->
        <div v-else-if="editField === 'final_testpoints_json' && tpEditItems.length">
          <div class="tp-toolbar" style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
            <el-button size="small" type="success" @click="addTpItem">➕ 新增测试点</el-button>
            <span class="hint-text">共 {{ tpEditItems.length }} 条测试点</span>
          </div>
          <div v-for="(tp, i) in tpEditItems" :key="i" class="tp-card">
            <div class="tp-card-head" style="display:flex;align-items:center;gap:8px">
              <b>{{ tp.title || ('测试点 #' + (i+1)) }}</b>
              <el-button size="mini" type="danger" plain style="margin-left:auto" @click="removeTpItem(i)">🗑 删除</el-button>
            </div>
            <p>{{ tp.description || tp.issue || '' }}</p>
            <el-input v-model="tpEditItems[i]._raw" type="textarea" :rows="2"
              placeholder="修改此条内容…" />
          </div>
        </div>

        <!-- 待确认问题 PCI：拆解为独立子问题卡片（一个问题一张卡，永远扁平） -->
        <div v-else-if="editField === 'pci_json' && flatPciItems.length" class="generic-json-list">
          <!-- 弹窗内 PCI 优先级筛选（从主卡片进入时继承当前筛选） -->
          <div class="mt" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
            <span>筛选：</span>
            <el-radio-group v-model="pciEditFilter" size="mini">
              <el-radio-button value="all">全部 ({{ flatPciItems.length }})</el-radio-button>
              <el-radio-button value="blocker">blocker ({{ pciStats.blocker }})</el-radio-button>
              <el-radio-button value="high">high ({{ pciStats.high }})</el-radio-button>
              <el-radio-button value="medium">medium ({{ pciStats.medium }})</el-radio-button>
              <el-radio-button value="low">low ({{ pciStats.low }})</el-radio-button>
            </el-radio-group>
            <span class="hint-text">仅显示 {{ displayedPciItems.length }} 条子问题</span>
          </div>
          <div v-for="(item, idx) in displayedPciItems" :key="'sq' + item.pciIndex + '_' + item.subIndex"
               class="pci-card pci-sub-card" :class="{ 'pci-sub-confirmed': item.sub.confirmed, 'pci-dup': item.pci._pci_dup }">
            <el-alert v-if="item.pci._pci_dup" type="warning" :closable="false" show-icon
              :title="`疑似与另一 PCI 重复（相似度 ${item.pci._pci_dup_similarity || '-'}），请核对是否可合并`" style="margin-bottom:8px" />
            <div class="pci-card-head">
              <el-tag v-if="item.pci._module" size="mini" type="success" effect="plain">{{ item.pci._module }}</el-tag>
              <span class="generic-card-index">{{ item.pci.id || ('#' + (item.pciIndex+1)) }}</span>
              <el-tag size="mini" :type="priTag(item.pci.impact)">{{ item.pci.impact || '-' }}</el-tag>
              <el-tag size="mini" :type="item.pci.type === 'Q2' ? 'danger' : 'warning'">{{ item.pci.type || 'Q' }}</el-tag>
              <span class="pci-sub-qbadge">Q{{ item.subIndex + 1 }}</span>
              <span v-if="item.pci.source" class="pci-source">{{ item.pci.source }}</span>
            </div>
            <div v-if="item.pci.blocked_scenarios && item.pci.blocked_scenarios.length" class="pci-blocked">
              <b>🚫 阻塞场景：</b>
              <span v-for="(bs, bi) in item.pci.blocked_scenarios" :key="bi" class="pci-bs-tag">{{ bs }}</span>
            </div>
            <div class="pci-sub-q">
              <span class="pci-q-badge">问</span>{{ item.sub.question }}
            </div>
            <div class="pci-sub-a-wrap" :class="{ 'pci-ai-draft': !!item.sub._aiDraft }">
              <label class="pci-sub-a-label">{{ item.sub._aiDraft ? 'A（AI 草稿）：' : 'A：' }}</label>
              <el-input v-model="item.sub.answer" type="textarea" :rows="3"
                :class="'pci-answer-input' + (item.sub._aiDraft ? ' pci-ai-answer' : '')"
                placeholder="AI 解答草稿，可编辑修改..."
                @input="clearAiDraft(item.sub)" />
            </div>
            <div class="pci-sub-foot">
              <el-checkbox v-model="item.sub.confirmed" size="small">已确认</el-checkbox>
              <el-button v-if="!item.sub.answer" size="mini" type="text" :loading="item.sub._loading"
                @click="askSingleSubQ(item.pciIndex, item.subIndex)">单独解答此题</el-button>
            </div>
          </div>

          <div class="mt" style="display:flex;gap:8px;align-items:center">
            <el-button size="small" type="primary" @click="savePciReplies">保存全部回复</el-button>
            <el-button size="small" type="success" :loading="allPciLoading" @click="splitAllPci">AI 全部解答</el-button>
            <el-button size="small" type="warning" plain @click="resetAllPciSplit">重置拆解</el-button>
            <el-divider direction="vertical" />
            <el-button size="small" type="info" plain @click="batchConfirmByImpact('low')">一键确认 low ({{ pciStats.low }})</el-button>
            <el-button size="small" type="warning" plain @click="batchConfirmByImpact('medium')">一键确认 medium ({{ pciStats.medium }})</el-button>
            <el-popconfirm title="将所有 low/medium 子问题标记为已确认并填充默认答案？" @confirm="batchConfirmLowMedium">
              <template #reference>
                <el-button size="small" type="warning" plain>批量确认 low+medium ({{ pciStats.low + pciStats.medium }})</el-button>
              </template>
            </el-popconfirm>
            <el-button size="small" :loading="allPciLoading" @click="splitAndAutoConfirm">AI 解答并自动确认</el-button>
            <span class="hint-text">
              共 {{ flatPciItems.length }} 个子问题（{{ parsedPciItems.length }} 条 PCI）
              <span class="pci-stat-inline">| blocker: {{ pciStats.blocker }} / high: {{ pciStats.high }} / medium: {{ pciStats.medium }} / low: {{ pciStats.low }}</span>
            </span>
          </div>
        </div>

        <!-- 风险识别：展开 risk_points 每条为独立卡片 + 回复框 -->
        <div v-else-if="editField === 'risks_json' && parsedRiskItems.length" class="generic-json-list">
          <div v-for="(risk, i) in parsedRiskItems" :key="i" class="risk-card">
            <div class="risk-card-head">
              <el-tag v-if="risk._module" size="mini" type="success" effect="plain">{{ risk._module }}</el-tag>
              <span class="generic-card-index">{{ risk.id || ('R' + (i+1)) }}</span>
              <el-tag size="mini" :type="priTag(risk.impact)">{{ risk.impact || '-' }}</el-tag>
              <el-tag v-if="risk.category" size="mini" type="info">{{ risk.category }}</el-tag>
            </div>
            <div class="risk-card-desc">{{ risk.description || '' }}</div>
            <div v-if="risk.related_testpoints && risk.related_testpoints.length" class="risk-related">
              <b>关联测试点：</b>{{ risk.related_testpoints.join(', ') }}
            </div>
            <div class="pci-reply-section">
              <label class="pci-reply-label">✍️ 风险处置 / 缓解措施：</label>
              <el-input v-model="riskReplies[i]" type="textarea" :rows="2"
                placeholder="填写风险应对策略（如：接受/规避/转移/缓解）…" />
            </div>
            <el-input v-model="parsedRiskItems[i]._raw" type="textarea" :rows="2" class="mt"
              placeholder="修改此条原始 JSON…" />
          </div>
          <el-button size="small" type="primary" class="mt" @click="saveRiskReplies">💾 保存全部回复</el-button>
        </div>

        <!-- 需求澄清：逐条问题 + 人工结论输入框 -->
        <div v-else-if="editField === 'clarification_log' && clarifyEditItems.length" class="generic-json-list">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span class="hint-text">共 {{ clarifyEditItems.length }} 条澄清项</span>
            <el-button size="mini" type="danger" plain @click="clearAllClarifyResolutions">清空全部结论</el-button>
          </div>
          <div v-for="(item, i) in clarifyEditItems" :key="i" class="clarify-item">
            <div class="clarify-item-head">
              <div class="clarify-head-left">
                <el-tag size="mini" :type="typeTag(item.type)">{{ item.type || '问题' }}</el-tag>
                <el-tag v-if="item.priority" size="mini" type="warning">{{ item.priority }}</el-tag>
                <span v-if="item.location" class="clarify-loc">📍 {{ item.location }}</span>
              </div>
              <el-button size="mini" type="danger" plain class="clarify-delete-btn" @click="deleteClarifyEditItem(i)">🗑️ 删除</el-button>
            </div>
            <div class="clarify-issue">{{ item.issue || item.suggested_question || '' }}</div>
            <div v-if="item.suggested_question" class="clarify-suggest"><b>建议提问：</b>{{ item.suggested_question }}</div>
            <div v-if="item.impact" class="clarify-impact">影响范围：{{ item.impact }}</div>
            <el-input v-model="clarifyEditItems[i].resolution" type="textarea" :rows="2" class="mt"
              placeholder="填写与产品确认后的回复/结论…" />
            <el-input v-model="clarifyEditItems[i]._raw" type="textarea" :rows="2" class="mt mono-font"
              placeholder="高级：修改此条原始 JSON…" />
          </div>
          <el-button size="small" type="primary" class="mt" @click="saveClarifyEditItems">💾 保存全部结论</el-button>
        </div>

        <!-- 其他产物：通用 JSON 数组卡片视图 -->
        <div v-else-if="parsedJsonArray.length" class="generic-json-list">
          <div v-for="(item, i) in parsedJsonArray" :key="i" class="generic-json-card">
            <div class="generic-card-head">
              <span class="generic-card-index">#{{ i + 1 }}</span>
              <span v-if="item.module" class="generic-card-module"><el-tag size="mini" type="info">{{ item.module }}</el-tag></span>
              <span v-if="item.title" class="generic-card-title">{{ item.title }}</span>
              <span v-else-if="item.id" class="generic-card-id">{{ item.id }}</span>
            </div>
            <div class="generic-card-body">
              <div v-for="(val, key) in displayFields(item)" :key="key" class="generic-field">
                <span class="generic-field-key">{{ fieldLabel(key) }}</span>
                <span class="generic-field-val">{{ formatFieldValue(val) }}</span>
              </div>
            </div>
            <el-input v-model="parsedJsonArray[i]._raw" type="textarea" :rows="3"
              class="mt" placeholder="修改此条 JSON…" />
          </div>
        </div>

        <div v-else class="empty-hint">
          {{ editField === 'requirement_summary'
             ? '需求摘要为 Markdown 文本，请切换到上方「原文模式」编辑'
             : '当前产物为空或格式无法解析（可切换到「原文模式」编辑）' }}
        </div>
      </div>

      <!-- 原文模式 -->
      <div v-else class="raw-mode">
        <el-input v-model="editFieldValue" type="textarea" :rows="18" class="mono-font" />
      </div>

      <template #footer>
        <el-button @click="closeEditDialog">取消</el-button>
        <el-button size="small" type="warning" plain @click="revertEditField">放弃修改</el-button>
        <el-button type="primary" :disabled="!editFieldValue.trim()" @click="saveEditDialog">💾 保存修改</el-button>
      </template>
    </el-dialog>

    <!-- 阶段4：冒烟执行 -->
    <el-card v-if="step === 4 || (proto && (proto.smoke_json || proto.testcases_json))" class="stage-card">
      <template #header>
        <div style="display:flex;align-items:center;justify-content:space-between">
          <b>阶段4 · 冒烟执行决策</b>
          <el-button v-if="step > 4" size="small" type="primary" plain @click="goStep(4)">↩ 回到此阶段</el-button>
        </div>
      </template>
      <p>请在 APP 端按冒烟用例清单逐项执行，记录 Pass / Fail / Block 后告知结果：</p>
      <el-radio-group v-model="smokeDecision">
        <el-radio value="passed">✅ 冒烟通过（进入全量测试）</el-radio>
        <el-radio value="rejected">❌ 冒烟失败（生成驳回邮件）</el-radio>
      </el-radio-group>
      <el-input v-if="smokeDecision === 'rejected'" v-model="rejectEmail" type="textarea" :rows="5"
        placeholder="驳回邮件正文（失败用例、环境、收件人等）" class="mt" />
      <div class="mt">
        <el-button type="primary" :disabled="!smokeDecision" @click="onSmoke">提交决策</el-button>
      </div>
      <el-alert v-if="proto && proto.stage4_decision" type="success"
        :title="'已记录决策：' + (proto.stage4_decision === 'passed' ? '冒烟通过' : '冒烟驳回')" show-icon />
    </el-card>

    <!-- 快速新建项目弹窗 -->
    <el-dialog v-model="showQuickProject" title="新建项目" width="480px" append-to-body>
      <el-form label-width="80px">
        <el-form-item label="项目名称">
          <el-input v-model="quickProjectName" placeholder="输入项目名称" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="quickProjectDesc" type="textarea" :rows="3" placeholder="可选：项目描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showQuickProject = false">取消</el-button>
        <el-button type="primary" :disabled="!quickProjectName.trim()" @click="createQuickProject">创建</el-button>
      </template>
    </el-dialog>

    <!-- 快速新建版本弹窗 -->
    <el-dialog v-model="showQuickVersion" title="新建版本" width="480px" append-to-body>
      <el-form label-width="80px">
        <el-form-item label="版本名称">
          <el-input v-model="quickVersionName" placeholder="输入版本号/名称，如 V1.0.0" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showQuickVersion = false">取消</el-button>
        <el-button type="primary" :disabled="!quickVersionName.trim()" @click="createQuickVersion">创建</el-button>
      </template>
    </el-dialog>

    <!-- 画布管理（二级页面）：随时勾选/改名/删除，显示每条画布参与到的整体阶段 -->
    <el-dialog v-model="canvasManageVisible" title="管理画布 · 选择参与后续工作流的画布" width="720px" append-to-body
      @open="onCanvasManageOpen">
      <el-alert type="info" :closable="false" show-icon
        title="勾选的画布会参与需求结构化与后续用例生成；取消勾选则从工作流中排除（已产生的产物不受影响）。保存后若已运行过结构化，需回到阶段1重新运行以纳入变更。" />
          <div class="canvas-list" style="max-height:460px;margin-top:12px">
        <div class="canvas-list-toolbar">
          <el-checkbox v-model="checkAll" :indeterminate="canvasIndeterminate" @click.stop>全选</el-checkbox>
          <span class="canvas-count">已选 {{ canvasSelectedCount }}/{{ canvasPages.length }}</span>
        </div>
        <div
          v-for="(c, idx) in canvasPages"
          :key="c.id"
          class="canvas-card"
          :class="{ active: canvasActiveId === c.id, unchecked: !c.selected, 'auto-filtered': isAutoFiltered(c) }"
          @click="canvasActiveId = c.id">
          <div class="canvas-card-head">
            <el-checkbox v-model="c.selected" @click.stop
              @update:model-value="onCanvasToggle(c)" />
            <span class="canvas-no">{{ String(idx + 1).padStart(2, '0') }}</span>
            <el-input v-model="c.page_name" size="small" class="canvas-name-input"
              @click.stop @input="markCanvasDirty(c)" placeholder="画布名" />
            <el-tag v-if="c.duplicate_of_page" size="mini" type="info">重复</el-tag>
            <el-tag v-if="isAutoFiltered(c)" size="mini" type="info" class="auto-filter-tag">自动过滤</el-tag>
            <el-tag size="mini" :type="canvasStageTag(c)">{{ canvasStageOf(c) }}</el-tag>
          </div>
          <div v-if="isAutoFiltered(c)" class="canvas-auto-filter-reason">{{ c.recognition_note }}</div>
          <div class="canvas-card-meta">
            <el-tag v-if="!c.visionAttempted" size="mini" type="warning">未视觉识别</el-tag>
            <el-tag v-else-if="c.visionSuccess" size="mini" type="success">视觉✓</el-tag>
            <el-tag v-else size="mini" type="danger">视觉✗</el-tag>
            <el-tag v-if="c.warnings && c.warnings.length" size="mini" type="warning">警告{{ c.warnings.length }}</el-tag>
            <el-tag size="mini" type="info">文本{{ (c.text || '').length }}字</el-tag>
          </div>
        </div>
      </div>
      <template #footer>
        <el-button @click="canvasManageVisible = false">取消</el-button>
        <el-button type="primary" :loading="canvasManageSaving" @click="saveCanvasManage">💾 保存画布选择</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script>
import request from '@/utils/api'
import { load as yamlLoad } from 'js-yaml'
import ModaoQA from './ModaoQA.vue'
import RequirementSummaryViewer from './RequirementSummaryViewer.vue'

export default {
  name: 'ModaoSkillView',
  components: { ModaoQA, RequirementSummaryViewer },
  props: {
    // 二级路由 modao-canvas/:id 传入的任务 id；无则为新建空态
    id: { type: [String, Number], default: null },
  },
  data() {
    return {
      step: 0,
      protoId: null,
      proto: null,
      stage1ViewMode: 'visual',
      canvasConfirmed: false,
      canvasActiveId: null,
      activeShotIdx: 0,
      canvasSaving: false,
      // 画布管理二级弹窗（随时打开，勾选/改名/删除，不影响主流程阶段机）
      canvasManageVisible: false,
      canvasManageSaving: false,
      busy: false,
      terminating: false,
      xmindExporting: false,
      xmindImporting: false,
      xmindPreviewVisible: false,
      xmindPreviewTree: [],
      pollTimer: null,
      // 轮询连续失败次数：达到上限自动停止，防止接口持续 500 时把后端打死
      pollFailCount: 0,
      smokeDecision: '',
      rejectEmail: '',
      history: [],
      isAdmin: false,
      loadingHistory: false,
      expandedRows: [],
      // 编辑态
      editingField: null,
      editBuffer: '',
      // 阶段2 澄清清单逐条填写
      clarifyItems: [],
      clarifyReplies: [],
      clarifyParsed: false,
      // 阶段2 卡片折叠（确认后默认收起，仍可展开修改）
      stage2Collapsed: false,
      stage2Init: false,
      // 编辑弹窗内「需求澄清」逐条编辑
      clarifyEditItems: [],
      // 阶段3 产物编辑弹窗
      editDialogVisible: false,
      editField: 'testcases_json',
      editFieldValue: '',
      editRawMode: false,
      // 合并测试点结构化编辑：稳定工作数组（v-model 绑定它，删除/修改才能落库）
      tpEditItems: [],
      removingTpIndex: -1,  // 主页面删除中的测试点索引（用于按钮 loading）
      // PCI / Risks 逐条回复
      pciReplies: [],
      riskReplies: [],
      pciLoading: [],
      pciSubQuestions: [],   // 每条 PCI 拆解出的子问题数组
      flatPciItemsList: [],  // 弹窗 PCI 子问题扁平列表（避免 computed 写自身依赖）
      allPciLoading: false,  // 全部拆解按钮 loading
      editFields: [
        { value: 'requirement_summary', label: '需求摘要（阶段1）' },
        { value: 'clarification_log', label: '需求澄清（阶段2）' },
        { value: 'module_split', label: '模块拆分' },
        { value: 'risks_json', label: '风险识别' },
        { value: 'pci_json', label: '待确认问题 PCI' },
        { value: 'final_testpoints_json', label: '合并测试点' },
        { value: 'testcases_json', label: '测试用例' },
        { value: 'smoke_json', label: '冒烟用例' },
        { value: 'quality_report_json', label: '质量自检报告' },
      ],
      projects: [],
      versions: [],
      loadingProjects: false,
      syncMsg: '',
      // 历史列表筛选
      filterProject: null,
      filterVersion: null,
      // 快速新建
      showQuickProject: false,
      showQuickVersion: false,
      quickProjectName: '',
      quickProjectDesc: '',
      quickVersionName: '',
      form: {
        source_type: 'modao',
        url: '',
        webshare_url: '',
        cookies: '',
        requirement_text: '',
        title: '墨刀需求梳理',
        project: null,
        version: null,
        dual_fusion: true,
      },
      // 一键采纳到用例库
      adoptLoading: false,
      approvePagesLoading: false,
      adoptIncludeSmoke: false,
      // 逐条采纳追踪（key 为 "gi-ti" 或 "smoke-gi-ti"，value 为 true=已采纳）
      adoptedCaseMap: {},
      // 阶段3产物筛选标签页
      stage3ActiveTab: 'all',
      // 主卡片 PCI 优先级筛选（all / blocker / high / medium / low）
      mainPagePciFilter: 'all',
      // 弹窗 PCI 优先级筛选（从主卡片进入时继承）
      pciEditFilter: 'all',
    }
  },
  beforeUnmount() {
    if (this.pollTimer) clearInterval(this.pollTimer)
  },
  watch: {
    'proto.clarification_log'() { this.parseClarify() },
    // 切换任务时重置阶段2 折叠态（避免沿用上一个任务的展开/收起状态）
    protoId() {
      this.stage2Collapsed = false
      this.stage2Init = false
      this.clarifyEditItems = []
    },
    // 切换编辑字段前，先把当前字段的未保存内容存为草稿
    editField(newVal, oldVal) {
      if (oldVal && oldVal !== newVal) {
        this.saveDraft(oldVal)
      }
      this.onEditFieldChange()
    },
    // 从原文模式切回结构化视图时，确保 PCI 子问题卡片重新生成
    editRawMode(newVal) {
      if (!newVal && this.editField === 'pci_json') {
        this.$nextTick(() => this.recomputeFlatPciItems())
      }
    },
    // 切换画布时重置分段截图预览到第一段
    canvasActiveId() {
      this.activeShotIdx = 0
    },
    // editFieldValue 变化（打开字段/切换/放弃修改/恢复草稿）时，重建 PCI 扁平列表为真实响应式对象，
    // 保证输入框 v-model 绑定持久化对象。注意：用户在结构化视图内输入 answer 不会改动 editFieldValue，
    // 因此不会误触发重建，输入不会被打断。
    editFieldValue() {
      if (this.editField === 'pci_json' && !this.editRawMode) {
        this.recomputeFlatPciItems()
      }
      // 需求澄清：原文变化（保存后/放弃修改后）重建逐条数组
      if (this.editField === 'clarification_log' && !this.editRawMode) {
        this.buildClarifyEditItems()
      }
    },
  },
  methods: {
    // ---------- 画布模块人工校验 ----------
    mediaUrl(path) {
      if (!path) return ''
      if (/^https?:\/\//.test(path)) return path
      return '/media/' + String(path).replace(/^\/?media\//, '')
    },
    // 编辑态（selected/_dirty/_delete）必须落在 proto.pages 原始响应式对象上，
    // 否则会被 canvasPages computed 的临时拷贝吞掉，导致保存时读不到用户改动。
    _rawPage(id) {
      const pages = (this.proto && this.proto.pages) || []
      return pages.find(p => p.id === id) || null
    },
    enterCanvasReview() {
      this.canvasConfirmed = false
      if (this.canvasPages.length && !this.canvasActiveId) {
        this.canvasActiveId = this.canvasPages[0].id
      }
      this.$message.info('请逐一核对画布识别结果，勾选参与需求结构化的画布')
    },
    toggleSelectAll(val) {
      ;((this.proto && this.proto.pages) || []).forEach(p => {
        p.selected = val
        p._dirty = true
      })
    },
    onCanvasToggle(c) {
      const rp = this._rawPage(c.id)
      if (rp) {
        rp.selected = c.selected
        rp._dirty = true
      }
    },
    markCanvasDirty(c) {
      const rp = this._rawPage(c.id)
      if (rp) {
        rp._dirty = true
        // 同步可能的改名（v-model 先改 c.page_name 再触发 @input）
        if (c.page_name !== undefined && c.page_name !== rp.page_name) rp.page_name = c.page_name
      }
    },
    // ---------- A2-b 画布级进度展示与回退 ----------
    canvasStageLabel(c) {
      const st = c.current_stage || 0
      const labels = ['待读取', '已结构化', '已澄清', '已生成测试点', '已冒烟决策']
      let label = labels[st] || '待读取'
      if (c.stage_status === 'needs_rerun') label = '需重跑·' + label
      if (c.stage_status === 'failed') label = '失败·' + label
      return label
    },
    // 完整层级展示名：父级分组 + 画布名（如「前置付费页-横版」）。
    // 墨刀只读分享页 DOM 不渲染分组层级，父级由用户在校验视图手动填写；
    // 留空则只显示画布名。提取时 page_name 已保持纯画布名、parent_name 独立存储。
    canvasFullName(c) {
      if (!c) return '未命名画布'
      const pn = (c.parent_name || '').trim()
      const nm = c.page_name || '未命名画布'
      return pn ? `${pn}-${nm}` : nm
    },
    canvasStageType(c) {
      if (c.stage_status === 'failed') return 'danger'
      if (c.stage_status === 'needs_rerun') return 'warning'
      if (c.current_stage >= 4) return 'success'
      if (c.current_stage >= 1) return 'primary'
      return 'info'
    },
    async onPageReset(c, stage) {
      try {
        await this.$confirm(
          `确认将画布「${c.page_name}」回退到阶段${Number(stage) - 1}并标记需重跑？` +
          `该画布后续的全局产物（结构化/用例等）将清空，需重新运行对应阶段。`,
          '画布回退', { type: 'warning' }
        )
      } catch (e) {
        return
      }
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/page-reset/`,
          method: 'post',
          data: { page_id: c.id, stage: Number(stage) },
        })
        this.proto = res.data.proto
        await this.refresh()
        if (Number(stage) === 1) {
          // 回退到「读取」即意味着要重读此画布；只重读选中的这张，其余画布保持不变。
          this.$message.info(`画布「${c.page_name}」已回退，开始重新读取…`)
          await this.onReExtract([c.page_name])
        } else {
          this.$message.success(`画布「${c.page_name}」已回退到阶段${Number(stage) - 1}，请重新运行对应阶段`)
        }
      } catch (e) {
        this.$message.error('回退失败：' + (e.response?.data?.detail || e.message))
      }
    },
    // ---------- 画布管理二级弹窗 ----------
    openCanvasManage() {
      this.canvasActiveId = this.canvasActiveId || (this.canvasPages[0] && this.canvasPages[0].id)
      this.canvasManageVisible = true
    },
    onCanvasManageOpen() {
      // 打开时确保真实画布对象有 _dirty 标记（未改动过则置 false，便于保存时统一判断）
      ;((this.proto && this.proto.pages) || []).forEach(p => { if (p._dirty === undefined) p._dirty = false })
    },
    async saveCanvasManage() {
      const rawPages = (this.proto && this.proto.pages) || []
      if (!rawPages.some(c => c.selected && !c._delete)) {
        this.$message.warning('至少需勾选一个画布')
        return
      }
      this.canvasManageSaving = true
      try {
        const pages = rawPages
          .filter(c => c._dirty)
          .map(c => ({
            id: c.id,
            selected: c.selected,
            page_name: c.page_name,
            parent_name: c.parent_name || '',
            recognition_note: c.recognition_note || '',
            delete: !!c._delete,
          }))
        await request({ url: `${this.base()}/${this.protoId}/update-pages/`, method: 'post', data: { pages } })
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
        this.proto = res.data
        this.canvasManageVisible = false
        // 若已运行过结构化（有摘要），提示需重跑以纳入画布变更
        if (this.proto && this.proto.requirement_summary) {
          this.$message.success('画布选择已保存；如需纳入结构化，请回到阶段1重新运行需求结构化')
        } else {
          this.$message.success('画布选择已保存')
        }
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.canvasManageSaving = false
      }
    },
    // 画布参与到的整体阶段（X 方案：单 proto 内无画布独立 stage，显示其随整体进度到达的阶段）
    canvasStageOf(c) {
      const p = this.proto
      if (!p) return '未读取'
      if (!c.selected) return '未纳入'
      const stage = p.current_stage || 0
      const st = p.status
      const map = ['原型接入', '需求通读', '需求澄清', '用例设计', '冒烟执行']
      let label = map[stage] || '进行中'
      if (['extracting', 'structuring', 'clarifying', 'designing'].includes(st)) label = '进行中'
      if (st === 'done') label = '已完成'
      if (st === 'failed') label = '失败'
      if (st === 'partial') label = '部分读取'
      return label
    },
    canvasStageTag(c) {
      const label = this.canvasStageOf(c)
      if (label === '未纳入') return 'info'
      if (label === '已完成') return 'success'
      if (label === '失败') return 'danger'
      if (label === '进行中') return 'warning'
      return ''
    },
    onCanvasDelete(c) {
      this.$confirm(`确认删除画布「${c.page_name || '未命名'}」？该画布将不参与后续流程。`, '提示', {
        type: 'warning'
      }).then(() => {
        const rp = this._rawPage(c.id)
        if (rp) {
          rp._delete = true
          rp._dirty = true
          rp.selected = false
        }
        this.$message.success('将在保存时删除该画布')
      }).catch(() => {})
    },
    async saveCanvasReview() {
      if (this.canvasSaving) return
      const rawPages = (this.proto && this.proto.pages) || []
      if (!rawPages.some(c => c.selected && !c._delete)) {
        this.$message.warning('至少需勾选一个画布')
        return
      }
      this.canvasSaving = true
      try {
        const running = this.isRunning
        const pages = rawPages
          .filter(c => c._dirty)
          .map(c => ({
            id: c.id,
            selected: c.selected,
            page_name: c.page_name,
            parent_name: c.parent_name || '',
            recognition_note: c.recognition_note || '',
            delete: !!c._delete,
          }))
        await request({ url: `${this.base()}/${this.protoId}/update-pages/`, method: 'post', data: { pages } })
        // 重新拉取最新 proto（pages 已更新）
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
        this.proto = res.data
        if (running) {
          // 结构化/澄清/用例设计运行中：只保存画布选择，不跳转、不打断当前流程；
          // 变更将在下次重新运行结构化时纳入。
          this.$message.success('画布选择已保存；当前流程运行中，重新运行结构化后纳入变更')
        } else {
          this.canvasConfirmed = true
          this.goStep(1)
          this.$message.success('画布校验已保存，进入需求结构化')
        }
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.canvasSaving = false
      }
    },
    resetCanvasReview() {
      ;((this.proto && this.proto.pages) || []).forEach(p => { p._dirty = false; p._delete = false })
      this.$message.info('已重置本地修改')
    },
    // ---------- PCI 批量操作 ----------
    // 按影响级别批量确认 PCI 子问题
    // 答案优先级：后端 suggested_answer > resolution_condition（需过滤非答案）> 智能默认答案
    batchConfirmByImpact(impact) {
      // 判定一段文本是否像"答案"而非"问题描述复述"
      const looksLikeAnswer = (text) => {
        if (!text || !text.trim()) return false
        const t = text.trim()
        // ❌ 以"确认""需要""请确认""待"开头 → 多半是问题复述
        if (/^(确认|需要|请确认|待|需).{0,4}(所有?|是否|什么|哪些|如何|该)/.test(t)) return false
        // ❌ 超过60字且包含原问题关键词 → 大概率是复述
        if (t.length > 60 && /(确认|验证|检查|明确|定义|处理方案)/.test(t)) return false
        // ✅ 包含具体处置词 → 像答案
        if (/(采用|使用|选择|默认|显示|返回|跳转|延后|记录|忽略|按|取值)/.test(t)) return true
        return t.length <= 50 // 短文本倾向于答案
      }

      const defaultAnswers = {
        low: '低风险，延后到测试复盘阶段统一处理。',
        medium: '中风险，在测试执行过程中并行验证即可。',
        high: '高风险，需在测试设计阶段重点覆盖。',
        blocker: '阻断项，必须在测试执行前解决。',
      }
      let count = 0
      ;(this.flatPciItems || []).forEach(item => {
        if ((item.pci && item.pci.impact) === impact || (!item.pci?.impact && impact === 'low')) {
          if (item.sub) {
            // 优先用后端 suggested_answer（prompt #7 新增字段）
            const suggested = item.pci.suggested_answer && item.pci.suggested_answer.trim()
            const rc = item.pci.resolution_condition && item.pci.resolution_condition.trim()
            if (!item.sub.answer.trim()) {
              if (suggested && looksLikeAnswer(suggested)) {
                item.sub.answer = suggested
              } else if (rc && looksLikeAnswer(rc)) {
                item.sub.answer = rc
              } else {
                item.sub.answer = defaultAnswers[impact] || defaultAnswers.low
              }
            }
            item.sub.confirmed = true
            count++
          }
        }
      })
      if (count > 0) {
        this.$message.success(`已自动确认 ${count} 个 ${impact} 级别子问题`)
        this.flatPciItemsList = [...this.flatPciItemsList]
      } else {
        this.$message.info(`没有找到 ${impact} 级别的未确认子问题`)
      }
    },
    // 批量确认所有 low + medium 子问题
    batchConfirmLowMedium() {
      this.batchConfirmByImpact('low')
      this.batchConfirmByImpact('medium')
    },
    // AI 解答全部 PCI 后自动确认 medium/low 级别
    async splitAndAutoConfirm() {
      this.allPciLoading = true
      try {
        // 1) 先 AI 解答（复用现有逻辑）
        await Promise.all(this.parsedPciItems.map((_, i) =>
          (this.pciSubQuestions[i] && this.pciSubQuestions[i].length)
            ? Promise.resolve()
            : this.splitAndAnswerPci(i)
        ))
        // 2) 自动确认 medium / low
        let autoCount = 0
        ;(this.flatPciItems || []).forEach(item => {
          if (item.sub && !item.sub.confirmed) {
            const level = (item.pci && item.pci.impact) || 'low'
            if (level === 'low' || level === 'medium') {
              item.sub.confirmed = true
              autoCount++
            }
          }
        })
        this.$message.success(`AI 解答完成，已自动确认 ${autoCount} 个 medium/low 级子问题（blocker/high 请人工审核）`)
      } catch (e) {
        /* 单条错误已在内部处理 */
      } finally {
        this.allPciLoading = false
        this.recomputeFlatPciItems()
      }
    },
    // ⚠️ 必须带 `/api` 前缀（后端真实路由为 `api/requirement-analysis/`，
    // 不带 /api 的形态只有 log-viewer / log-raw 两个别名，其余接口 404）。
    base() { return '/requirement-analysis/api/modao' },
    statusLabel(s) {
      const m = { pending: '待处理', extracting: '读取中', extracted: '已读取', partial: '部分读取', structuring: '结构化中', clarifying: '澄清中', designing: '用例设计中', done: '已完成', failed: '失败' }
      return m[s] || s
    },
    statusType(s) {
      const m = { pending: 'info', extracting: 'warning', extracted: 'success', partial: 'warning', structuring: 'warning', clarifying: 'warning', designing: 'warning', done: 'success', failed: 'danger' }
      return m[s] || 'info'
    },
    stageLabel(n) {
      const m = { 0: '原型接入', 1: '需求通读', 2: '需求澄清', 3: '用例设计', 4: '冒烟执行' }
      return m[n] || ''
    },
    toggleExpand(row) {
      const idx = this.expandedRows.indexOf(row.id)
      if (idx >= 0) {
        this.expandedRows.splice(idx, 1)
      } else {
        // 若无日志数据，先从详情接口拉取
        if (!row.work_log && !row.error_message) {
          request({ url: `${this.base()}/${row.id}/`, method: 'get' }).then(res => {
            const target = this.history.find(h => h.id === row.id)
            if (target) Object.assign(target, res.data)
          }).catch(() => {})
        }
        this.expandedRows.push(row.id)
      }
    },
    pretty(jsonStr) {
      try { return JSON.stringify(JSON.parse(jsonStr), null, 2) } catch (e) { return jsonStr }
    },
    countOf(jsonStr) {
      try { return JSON.parse(jsonStr).length } catch (e) { return 0 }
    },
    friendlyError(raw) {
      // 把后端抛出的技术性错误文案转成中文友好提示，便于用户快速判断根因并决策
      if (!raw) return '任务执行失败，请查看下方详情或重试'
      const m = String(raw)
      if (/Insufficient Balance|余额不足|402/.test(m)) return 'AI 模型余额不足，请联系管理员充值后重试'
      if (/401|Unauthorized|Invalid API|鉴权失败|API key/.test(m)) return 'AI 模型鉴权失败，请检查密钥配置'
      if (/503|too busy|Service Unavailable|服务繁忙/.test(m)) return 'AI 模型服务繁忙，请稍后重试'
      if (/peer closed|incomplete chunked read|RemoteProtocolError|连接中断|Connection reset|TransportError/.test(m)) return 'AI 服务连接中断（供应商网络抖动），已自动重试仍失败，请稍后重试'
      if (/Timeout|超时|CancelledError/.test(m)) return 'AI 调用超时或被取消，请稍后重试或精简原型'
      if (/Connection refused|无法连接|getaddrinfo|Name or service not known/.test(m)) return '无法连接 AI 服务地址，请检查网络与代理配置'
      return '任务执行失败：' + (m.length > 100 ? m.slice(0, 100) + '…' : m)
    },
    startPoll() {
      if (this.pollTimer) clearInterval(this.pollTimer)
      // 每次开始新一轮轮询都重置失败计数
      this.pollFailCount = 0
      this.pollTimer = setInterval(() => this.refresh(), 3000)
    },
    stopPoll() {
      if (this.pollTimer) { clearInterval(this.pollTimer); this.pollTimer = null }
    },
    async refresh() {
      if (!this.protoId) return
      try {
        const res = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
        // 保留用户在画布列表上的本地未保存改动（_dirty）：运行期间允许继续选取/勾选/改名，
        // 不被轮询刷新整体覆盖丢失；等服务端数据回来后再按 id 合并回原始对象。
        const localDirty = (this.canvasPages || [])
          .filter(c => c._dirty)
          .map(c => ({ id: c.id, selected: c.selected, page_name: c.page_name, parent_name: c.parent_name, recognition_note: c.recognition_note }))
        this.proto = res.data
        if (localDirty.length) {
          const pages = (this.proto && this.proto.pages) || []
          localDirty.forEach(d => {
            const t = pages.find(p => p.id === d.id)
            if (t) {
              if (d.selected !== undefined) t.selected = d.selected
              if (d.page_name !== undefined) t.page_name = d.page_name
              if (d.parent_name !== undefined) t.parent_name = d.parent_name
              if (d.recognition_note !== undefined) t.recognition_note = d.recognition_note
              t._dirty = true
            }
          })
        }
        this.parseClarify()
        // 首次加载：已确认过阶段2 且当前不在阶段2 时默认折叠（保留入口但不占版面）
        if (!this.stage2Init) {
          this.stage2Init = true
          if (this.stage2Confirmed && this.step !== 2) this.stage2Collapsed = true
        }
        const s = res.data.status
        // 任务从「运行中」转入「失败/部分成功」时，弹出明显的错误提示——否则轮询结果容易被用户错过
        if ((s === 'failed' || s === 'partial') && this._prevProtoStatus
            && this._prevProtoStatus !== s
            && ['extracting', 'structuring', 'clarifying', 'designing'].includes(this._prevProtoStatus)) {
          this.$message.error(this.friendlyError(res.data.error_message))
        }
        this._prevProtoStatus = s
        // 成功了一次就清零失败计数，避免偶发抖动累积到上限
        this.pollFailCount = 0
        if (!['extracting', 'structuring', 'clarifying', 'designing'].includes(s)) {
          this.stopPoll()
          this.busy = false
        }
      } catch (e) {
        // ⚠️ 这里原来是无条件 `catch (e) { /* ignore */ }` —— 静默吞掉所有错误，
        // 导致接口持续 500 时轮询永远不会停止。2026-10-22 生产事故：
        // modao/89 每 3 秒 500 一次、多个标签页并发，把 MySQL 连接打满到 151 上限，
        // 之后连登录接口都连不上库，形成「越 500 越轮询」的死循环。
        // 现在加连续失败上限：超过阈值就停轮询并明确告知用户，绝不静默续命。
        this.pollFailCount = (this.pollFailCount || 0) + 1
        const MAX_POLL_FAILS = 5
        if (this.pollFailCount >= MAX_POLL_FAILS) {
          this.stopPoll()
          this.busy = false
          const status = e && e.response && e.response.status
          this.$message.error(
            status
              ? `服务端连续 ${MAX_POLL_FAILS} 次请求失败（HTTP ${status}），已停止自动刷新。请稍后手动刷新页面重试。`
              : `网络异常，已连续 ${MAX_POLL_FAILS} 次无法获取任务状态，已停止自动刷新。请检查网络后手动刷新页面。`
          )
        }
      }
    },
    goStep(n) { this.step = n },
    // ---------- 内容编辑 ----------
    beginEdit(field) {
      this.editingField = field
      this.editBuffer = this.proto[field] || ''
    },
    cancelEdit() {
      this.editingField = null
      this.editBuffer = ''
    },
    async saveEdit() {
      if (!this.editingField) return
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: this.editingField, value: this.editBuffer },
        })
        this.proto[this.editingField] = this.editBuffer
        this.$message.success('已保存修改')
        this.editingField = null
        this.editBuffer = ''
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      }
    },
    // ---------- 阶段2 澄清清单：逐条填写回复 ----------
    /** 从原文解析澄清数组：纯 JSON / ```json 代码块 / 首个 [...] 片段，三种策略兼容 LLM 输出 */
    _parseClarifyArray(raw) {
      const text = String(raw || '')
      if (!text.trim()) return []
      // 策略1：直接解析为 JSON 数组
      try {
        const v = JSON.parse(text)
        if (Array.isArray(v) && v.length) return v
      } catch (e) { /* 非纯 JSON，继续下一种策略 */ }
      // 策略2：提取 ```json 或 ```yaml 代码块内容再解析（兼容有无结束 ``` 标记）
      const m = text.match(/```(?:json|yaml|yml)?\s*\n([\s\S]*?)(?:```|$)/i)
      if (m) {
        try {
          const v = JSON.parse(m[1].trim())
          if (Array.isArray(v) && v.length) return v
        } catch (e) { /* noop */ }
      }
      // 策略3：全文找首个 [...] JSON 数组（LLM 可能在前后加了说明文字）
      const arrMatch = text.match(/\[[\s\S]*?\](?=\s*[\n]|$)/)
      if (arrMatch) {
        try {
          const v = JSON.parse(arrMatch[0])
          if (Array.isArray(v) && v.length) return v
        } catch (e) { /* noop */ }
      }
      return []
    },
    /** 把新数组文本写回原文：纯 JSON 整体替换；markdown/说明文字包裹时只替换其中的 JSON 片段 */
    _replaceClarifyJsonInRaw(raw, newText) {
      const text = String(raw || '')
      if (!text.trim()) return newText
      try {
        const v = JSON.parse(text)
        if (Array.isArray(v)) return newText
      } catch (e) { /* 非纯 JSON，走片段替换 */ }
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
      const items = this._parseClarifyArray((this.proto && this.proto.clarification_log) || '')
      if (items.length && items[0] && typeof items[0] === 'object') {
        this.clarifyItems = items
        this.clarifyReplies = items.map(it => (it && (it.resolution || it.answer)) || '')
        this.clarifyParsed = true
      } else {
        this.clarifyItems = []
        this.clarifyReplies = []
        this.clarifyParsed = false
      }
    },
    async saveClarifyReplies() {
      if (!this.clarifyParsed) return
      const items = this.clarifyItems.map((it, i) => ({
        ...it,
        resolution: (this.clarifyReplies[i] || '').trim(),
      }))
      const value = this._replaceClarifyJsonInRaw(
        (this.proto && this.proto.clarification_log) || '', JSON.stringify(items, null, 2))
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: 'clarification_log', value },
        })
        this.proto.clarification_log = value
        await this.refresh()
        this.$message.success('澄清回复已保存')
        this.warnApprovalInvalidated()
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async deleteClarifyItem(index) {
      const item = this.clarifyItems[index]
      const loc = (item && (item.location || item.issue)) ? `「${item.location || item.issue}」` : '该澄清点'
      try {
        await this.$confirm(
          `确定要删除澄清点 ${loc} 吗？\n删除后该条目将不再参与后续测试设计与用例生成。`,
          '删除澄清点',
          { confirmButtonText: '确定删除', cancelButtonText: '取消', type: 'warning' }
        )
      } catch (e) {
        return
      }
      const newItems = this.clarifyItems.filter((_, i) => i !== index)
      const newReplies = this.clarifyReplies.filter((_, i) => i !== index)
      const itemsToSave = newItems.map((it, i) => ({
        ...it,
        resolution: (newReplies[i] || '').trim(),
      }))
      const value = this._replaceClarifyJsonInRaw(
        (this.proto && this.proto.clarification_log) || '', JSON.stringify(itemsToSave, null, 2))
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: 'clarification_log', value },
        })
        this.proto.clarification_log = value
        this.clarifyItems = newItems
        this.clarifyReplies = newReplies
        await this.refresh()
        this.$message.success('已删除该澄清点')
        this.warnApprovalInvalidated()
      } catch (e) {
        this.$message.error('删除失败：' + (e.response?.data?.detail || e.message))
      }
    },
    deleteClarifyEditItem(index) {
      this.clarifyEditItems.splice(index, 1)
      this.$message.info('已移除条目，点底部「保存全部结论」后生效')
    },
    /** 澄清/测试点改动后，若原生成批准已失效，提示需重新批准 */
    warnApprovalInvalidated() {
      const g = this.proto && this.proto.generation_gate
      if (g && g.approved && g.fingerprint_matches === false) {
        this.$message.warning('内容已变更，原「批准生成」已失效，需重新批准后再生成最终用例')
      }
    },
    toggleRawEdit() {
      if (this.editingField === 'clarification_log') {
        this.cancelEdit()
      } else {
        this.beginEdit('clarification_log')
      }
    },
    typeTag(t) {
      const m = { '模糊点': 'warning', '缺失点': 'danger', '边界点': 'info' }
      return m[t] || ''
    },
    // ---------- 阶段3 产物编辑弹窗 ----------
    openEditDialog(field) {
      const target = field || 'testcases_json'
      // ⚠️ 切换任务后必须重置弹窗状态，否则 flatPciItemsList / pciSubQuestions
      // 会残留上一个任务的数据，导致 PCI/风险显示错误任务的内容。
      this.flatPciItemsList = []
      this.pciSubQuestions = []
      this.pciReplies = []
      this.pciLoading = []
      this.riskReplies = []
      this.editFieldValue = ''
      this.editRawMode = false
      // 弹窗 PCI 筛选：主卡片选中某级别时，进入弹窗继承该筛选
      if (target === 'pci_json') {
        this.pciEditFilter = this.mainPagePciFilter || 'all'
      } else {
        this.pciEditFilter = 'all'
      }
      this.editDialogVisible = true
      if (this.editField === target) {
        // 同一字段：直接恢复草稿 / 从 proto 初始化
        this.onEditFieldChange()
      } else {
        // 不同字段：交给 watch 处理（先存旧字段草稿，再初始化新字段）
        this.editField = target
      }
    },
    // ---------- 编辑草稿：未保存内容自动保留（关闭/切换/刷新后重进都不丢）----------
    draftKey(field) {
      return `modao_edit_draft_${this.protoId}_${field}`
    },
    saveDraft(field) {
      if (!field || !this.proto) return
      const draft = {
        editFieldValue: this.editFieldValue,
        editRawMode: this.editRawMode,
        pciReplies: this.pciReplies,
        pciSubQuestions: this.pciSubQuestions,
        riskReplies: this.riskReplies,
      }
      try { localStorage.setItem(this.draftKey(field), JSON.stringify(draft)) } catch (e) { /* 忽略隐私模式等异常 */ }
    },
    loadDraft(field) {
      try {
        const s = localStorage.getItem(this.draftKey(field))
        return s ? JSON.parse(s) : null
      } catch (e) { return null }
    },
    clearDraft(field) {
      try { localStorage.removeItem(this.draftKey(field)) } catch (e) { /* noop */ }
    },
    async closeEditDialog() {
      // PCI 弹窗：若已填写/确认了子问题，关闭时自动落库，确保外部列表立即可见（无需再点「保存全部回复」）
      if (this.editField === 'pci_json' && this.hasPciUnsavedContent) {
        await this.savePciReplies()
      } else {
        // 关闭前存草稿，下次重进可恢复
        this.saveDraft(this.editField)
      }
      this.editDialogVisible = false
    },
    revertEditField() {
      // 放弃未保存修改，回到后端原始数据
      this.clearDraft(this.editField)
      this.onEditFieldChange()
    },
    onEditFieldChange() {
      // 优先恢复未保存草稿（关闭弹窗 / 切换字段 / 刷新页面后重新进入都能保留）
      const draft = this.loadDraft(this.editField)
      const protoRaw = (this.proto && this.proto[this.editField]) || ''
      // 草稿有效性校验（关键，已踩坑）：不仅要「非空」，还要能解析出有效列表。
      // 否则「非空但损坏/过期的草稿」会遮盖 proto 真实数据，导致弹窗显示
      // 「当前产物为空或格式无法解析」。规则：
      //   - 草稿有数据且能解析出列表 → 用之（恢复未保存内容）
      //   - proto 有真实可解析数据，而草稿解析为空 → 丢弃草稿，用 proto
      //   - 两者都为空 → 用 proto（可能为空，由模板提示）
      const draftHasData = !!(draft && draft.editFieldValue && draft.editFieldValue.trim())
      const protoHasData = !!protoRaw.trim()
      const draftParsedLen = draftHasData ? this._parseListLen(draft.editFieldValue, this.editField) : 0
      const protoParsedLen = protoHasData ? this._parseListLen(protoRaw, this.editField) : 0
      const useDraft = draftHasData && !(protoParsedLen > 0 && draftParsedLen === 0)

      // ── PCI 字段特殊处理：必须先恢复 pciSubQuestions，再设置 editFieldValue ──
      // 原因：editFieldValue 有 watch 会立即触发 recomputeFlatPciItems()，
      // 如果此时 pciSubQuestions 还是旧值/空数组，recompute 会从 parsedPciItems 重新生成
      // 子问题（answer 为空），导致用户之前填写的答案被覆盖丢失。
      if (this.editField === 'pci_json') {
        // 防御：若后端 proto 已保存了子问题/答案，则忽略浏览器陈旧草稿，直接用 proto。
        // 否则旧代码 bug 时期缓存的空答案草稿会覆盖正确的后端数据，导致「答案消失」。
        const protoHasSavedAnswers = (this.parsedPciItems || []).some(it =>
          (it.sub_questions && it.sub_questions.some(s => s.answer && s.answer.trim())) ||
          (it.answer && it.answer.trim()) || (it.resolution_condition && it.resolution_condition.trim()))
        if (useDraft && draft.pciSubQuestions && draft.pciSubQuestions.length && !protoHasSavedAnswers) {
          // 有有效草稿且 proto 无已保存答案：先恢复子问题（含用户填写的 answer），再设 editFieldValue
          this.pciSubQuestions = draft.pciSubQuestions
          this.pciReplies = draft.pciReplies || []
          this.editFieldValue = draft.editFieldValue
          this.editRawMode = !!draft.editRawMode
          this.riskReplies = draft.riskReplies || []
        } else {
          // 无草稿或草稿无效：从 proto 初始化
          this.editFieldValue = protoRaw
          if (!this.pciSubQuestions.length && this.parsedPciItems.length) {
            this.splitAllPciLocal()
          }
          this.recomputeFlatPciItems()
          this.pciReplies = (this.parsedPciItems || []).map(it => (it && (it.answer || it.resolution_condition || it.resolution)) || '')
          this.pciLoading = (this.parsedPciItems || []).map(() => false)
        }
        return // PCI 已处理完毕，提前返回
      }

      // ── 非 PCI 字段的通用逻辑 ──
      if (useDraft) {
        this.editFieldValue = draft.editFieldValue
        this.editRawMode = !!draft.editRawMode
        this.pciReplies = draft.pciReplies || []
        this.pciSubQuestions = (draft.pciSubQuestions && draft.pciSubQuestions.length) ? draft.pciSubQuestions : []
        this.riskReplies = draft.riskReplies || []
      } else {
        this.editFieldValue = protoRaw
      }
      if (this.editField === 'risks_json') {
        this.riskReplies = (this.parsedRiskItems || []).map(it => (it && it.mitigation) || '')
      }
      // ── 合并测试点 final_testpoints_json：填充稳定工作数组（供删除/修改真正落库）──
      if (this.editField === 'final_testpoints_json') {
        this.rebuildTpEditItems()
      }
      // ── 需求澄清 clarification_log：构建逐条编辑数组 ──
      if (this.editField === 'clarification_log') {
        this.buildClarifyEditItems()
      }
    },
    // ---------- 编辑弹窗：需求澄清逐条编辑 ----------
    buildClarifyEditItems() {
      const items = this._parseClarifyArray(this.editFieldValue)
      this.clarifyEditItems = items.map(it => ({
        ...(it && typeof it === 'object' ? it : { value: it }),
        resolution: (it && (it.resolution || it.answer)) || '',
        _raw: JSON.stringify(it, null, 2),
      }))
    },
    clearAllClarifyResolutions() {
      this.clarifyEditItems = this.clarifyEditItems.map(it => ({ ...it, resolution: '' }))
      this.$message.info('已清空结论，点「💾 保存全部结论」才会生效')
    },
    async saveClarifyEditItems() {
      if (!this.clarifyEditItems.length) return
      const merged = this.clarifyEditItems.map(it => {
        const { _raw, ...rest } = it
        if (_raw && _raw.trim()) {
          try {
            const p = JSON.parse(_raw)
            if (p && typeof p === 'object') return { ...p, resolution: (it.resolution || '').trim() }
          } catch (e) { /* 原始 JSON 非法 → 回退用结构化字段 */ }
        }
        return { ...rest, resolution: (it.resolution || '').trim() }
      })
      const value = this._replaceClarifyJsonInRaw(this.editFieldValue, JSON.stringify(merged, null, 2))
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: 'clarification_log', value },
        })
        this.proto.clarification_log = value
        this.editFieldValue = value
        this.clearDraft('clarification_log')
        this.buildClarifyEditItems()
        await this.refresh()
        this.$message.success('澄清结论已保存')
        this.warnApprovalInvalidated()
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      }
    },

    // 从 editFieldValue 解析出测试点数组并填充 tpEditItems（兼容裸数组 / {testpoints:[...]} 包裹）
    parseTpArray() {
      try {
        const v = JSON.parse(this.editFieldValue || 'null')
        if (Array.isArray(v)) return { arr: v, wrapped: false }
        if (v && Array.isArray(v.testpoints)) return { arr: v.testpoints, wrapped: true }
        return { arr: [], wrapped: false }
      } catch (e) { return { arr: [], wrapped: false } }
    },

    rebuildTpEditItems() {
      const { arr } = this.parseTpArray()
      this.tpEditItems = arr.map(tp => ({ ...tp, _raw: JSON.stringify(tp, null, 2) }))
    },

    addTpItem() {
      this.tpEditItems.push({ title: '新测试点', _raw: JSON.stringify({ title: '新测试点', description: '' }, null, 2) })
    },

    removeTpItem(i) {
      if (i >= 0 && i < this.tpEditItems.length) {
        this.tpEditItems.splice(i, 1)
        this.$message.info(`已删除第 ${i + 1} 条测试点（保存后生效）`)
      }
    },

    // 主页面直接删除某条测试点：解析→移除→重建→PATCH 落库
    async removeMainTpItem(i) {
      const raw = this.proto && this.proto.final_testpoints_json
      if (!raw) return
      let arr = []
      let wrapped = null
      try {
        const v = JSON.parse(raw)
        if (Array.isArray(v)) {
          arr = v
        } else if (v && Array.isArray(v.testpoints)) {
          arr = v.testpoints
          wrapped = v
        } else {
          this.$message.error('测试点数据格式无法解析')
          return
        }
      } catch (e) {
        this.$message.error('测试点数据格式无法解析')
        return
      }
      if (i < 0 || i >= arr.length) return
      const removed = arr[i]
      const name = (removed && (removed.title || removed.name || '')).trim()
      const label = (removed && removed.id && name)
        ? `「${removed.id} · ${name}」`
        : (name ? `「${name}」` : (removed && removed.id ? `「${removed.id}」` : `第 ${i + 1} 个测试点`))
      try {
        await this.$confirm(
          `确定要删除测试点 ${label} 吗？\n删除后该条目将不再参与后续用例生成。`,
          '删除测试点',
          { confirmButtonText: '确定删除', cancelButtonText: '取消', type: 'warning' }
        )
      } catch (e) {
        return
      }
      arr.splice(i, 1)
      const value = wrapped ? JSON.stringify({ ...wrapped, testpoints: arr }, null, 2)
                           : JSON.stringify(arr, null, 2)
      this.removingTpIndex = i
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: 'final_testpoints_json', value },
        })
        this.proto.final_testpoints_json = value
        this.$message.success(`已删除测试点 ${label}`)
      } catch (e) {
        this.$message.error('删除失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.removingTpIndex = -1
      }
    },
    // ---------- PCI 子问题拆解与逐条解答 ----------
    // 纯前端本地快速拆分（按中文标点，零等待）
    // 优先直接读取已保存的 sub_questions（与主视图 mainPageFlatPci 逻辑一致），
    // 仅在 sub_questions 缺失/损坏时回退到按 description 拆分，避免重开弹窗后出现"新问题"。
    splitAllPciLocal() {
      this.parsedPciItems.forEach((pci, i) => {
        // 1) 优先：已有结构化子问题则直接采用（保留各自独立的 question/answer/confirmed）
        const savedSubs = (pci.sub_questions || []).filter(s => s && s.question)
        if (savedSubs.length) {
          this.pciSubQuestions[i] = savedSubs.map(s => ({
            question: s.question,
            answer: s.answer || pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '',
            confirmed: !!s.confirmed,
            _loading: false,
            _aiDraft: !(s.answer || '').trim() && !!(s.answer || pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '').trim(),
          }))
          return
        }
        // 2) 回退：无 sub_questions 时按 description 中文标点拆分
        const desc = (pci.description || '').trim()
        if (!desc) {
          this.pciSubQuestions[i] = [{ question: '(无描述内容)', answer: (pci.answer || pci.suggested_answer || pci.resolution_condition || ''), confirmed: false, _loading: false, _aiDraft: !!(pci.answer || pci.suggested_answer || pci.resolution_condition || '').trim() }]
          return
        }
        const parts = desc.split(/\s*(?:且|以及|；|,)\s*/).map(s => s.trim()).filter(Boolean)
        const items = parts.map(p => ({
          question: p,
          answer: pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '',
          confirmed: false,
          _loading: false,
          _aiDraft: !!(pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '').trim(),
        }))
        this.pciSubQuestions[i] = items.length ? items : [{ question: desc, answer: '', confirmed: false, _loading: false, _aiDraft: false }]
      })
    },
    // 重新计算弹窗 PCI 子问题的扁平列表。
    // 关键：始终以 parsedPciItems 为准，为每条 PCI 固化出「真实响应式子问题对象」到 pciSubQuestions，
    // 再引用这些真实对象生成扁平列表 —— 这样 v-model="item.sub.answer" 永远绑定持久化对象，
    // 彻底避免旧草稿结构不匹配导致 out 为空、进而回退到「兜底纯计算临时对象」而无法输入的问题。
    recomputeFlatPciItems() {
      const pcis = this.parsedPciItems || []
      // 1) 确保每条 PCI 都有一个有效的子问题数组（缺失/损坏则按描述重新拆分并直接赋值固化）
      pcis.forEach((pci, i) => {
        const cur = this.pciSubQuestions[i]
        if (!Array.isArray(cur) || !cur.length) {
          // 优先直接读取已保存的 sub_questions（与主视图一致，避免重建时混入"新问题"）
          const savedSubs = (pci.sub_questions || []).filter(s => s && s.question)
          if (savedSubs.length) {
            this.pciSubQuestions[i] = savedSubs.map(s => ({
              question: s.question,
              answer: s.answer || pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '',
              confirmed: !!s.confirmed,
              _loading: false,
              _aiDraft: !(s.answer || '').trim() && !!(s.answer || pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '').trim(),
            }))
            return
          }
          // 回退：无 sub_questions 时按 description 拆分
          const desc = (pci.description || '').trim()
          const parts = desc ? desc.split(/\s*(?:且|以及|；|,)\s*/).map(s => s.trim()).filter(Boolean) : ['(无描述内容)']
          const items = (parts.length ? parts : [desc || '(无描述内容)']).map(q => ({
            question: q,
            answer: pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '',
            confirmed: false,
            _loading: false,
            _aiDraft: !!(pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '').trim(),
          }))
          this.pciSubQuestions[i] = items
        } else {
          // 数组已存在：同步已有答案到子问题对象（避免重开弹窗后答案丢失）
          const savedSubs = (pci.sub_questions || []).filter(s => s && s.question)
          const savedMap = {}
          savedSubs.forEach(s => { savedMap[(s.question || '').trim()] = { answer: (s.answer || '').trim(), confirmed: !!s.confirmed } })
          cur.forEach(sub => {
            const trimmed = (sub.question || '').trim()
            const saved = savedMap[trimmed]
            if (saved) {
              if (saved.answer && !sub.answer) sub.answer = saved.answer
              if (saved.confirmed) sub.confirmed = true
            }
          })
        }
      })
      // 2) 生成扁平列表：一律引用 pciSubQuestions 里的真实对象
      const out = []
      pcis.forEach((pci, i) => {
        const items = this.pciSubQuestions[i] || []
        items.forEach((sub, j) => out.push({ pci, pciIndex: i, sub, subIndex: j }))
      })
      this.flatPciItemsList = out
    },
    // 将单条 PCI 的 description 拆分为子问题数组，每条自动生成解答（AI）
    async splitAndAnswerPci(i) {
      const pci = this.parsedPciItems[i]
      if (!pci) return
      this.pciLoading[i] = true
      try {
        const prompt =
          '你是一名资深测试分析师。下面是一条"待确认问题(PCI)"的完整描述内容。\n' +
          '请将其拆分为若干个**独立的、具体的子问题**（每个子问题只关注一个确认点），然后对每个子问题给出专业的解答/处置建议。\n\n' +
          '要求：\n' +
          '1. 必须返回合法 JSON 数组，格式：[{"question":"子问题描述","answer":"解答或建议"}]\n' +
          '2. 每个子问题要具体、可操作（如："动画每步的具体时长是否需要限制？" 而非笼统描述）\n' +
          '3. 解答要给出明确结论（接受风险 / 需补充设计 / 降级处理 / 阻塞发布等）及理由\n' +
          '4. 只输出 JSON 数组，不要其他文字\n\n' +
          'PCI 描述：\n' + (pci.description || '') + '\n' +
          (pci.blocked_scenarios && pci.blocked_scenarios.length ? '\n阻塞场景：' + pci.blocked_scenarios.join(', ') : '')
        const res = await request({
          url: `${this.base()}/${this.protoId}/ask/`, method: 'post',
          data: { stage: 3, question: prompt },
        })
        let items = []
        try {
          const text = (res.data.answer || '').trim()
          // 尝试从回答中提取 JSON 数组
          const jsonMatch = text.match(/\[[\s\S]*?\]/)
          if (jsonMatch) {
            items = JSON.parse(jsonMatch[0])
          }
        } catch (e) { /* 解析失败则用原始文本作为单个子问题 */ }
        if (!Array.isArray(items) || !items.length) {
          items = [{ question: pci.description || '', answer: res.data.answer || '' }]
        }
        // 给每项加 confirmed、_loading 和 _aiDraft
        items = items.map(it => ({
          question: it.question || '',
          answer: it.answer || '',
          confirmed: false,
          _loading: false,
          _aiDraft: !!(it.answer || '').trim(),
        }))
        // 合并策略：保留用户已编辑的 answer（避免异步返回覆盖用户正在输入的内容）
        const existing = this.pciSubQuestions[i] || []
        items = items.map((it, j) => {
          const old = (existing.length > j) ? existing[j] : null
          if (old && old.answer && !it.answer) {
            // AI 没给答案但用户已有编辑 → 保留用户的
            return { ...it, answer: old.answer, confirmed: old.confirmed }
          }
          if (old && old.answer && it.answer && old.answer !== it.answer) {
            // 用户和 AI 都有内容 → 保留用户的（用户优先）
            return { ...it, answer: old.answer, confirmed: old.confirmed }
          }
          return it
        })
        this.pciSubQuestions[i] = items
        this.recomputeFlatPciItems()
        this.$message.success(`PCI ${pci.id || ('#' + (i+1))} 拆解为 ${items.length} 个子问题`)
      } catch (e) {
        this.$message.error('拆解失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.pciLoading[i] = false
      }
    },
    // 单独解答某个子问题
    async askSingleSubQ(pciIdx, subIdx) {
      const sq = (this.pciSubQuestions[pciIdx] || [])[subIdx]
      if (!sq) return
      sq._loading = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/ask/`, method: 'post',
          data: { stage: 3, question: sq.question },
        })
        sq.answer = res.data.answer || ''
        sq._aiDraft = true
      } catch (e) {
        this.$message.error('解答失败：' + (e.response?.data?.detail || e.message))
      } finally {
        sq._loading = false
      }
    },
    // 用户在 PCI 回答输入框中编辑时，自动取消 AI 草稿标记（转为人工回答样式）
    clearAiDraft(sub) {
      if (sub && sub._aiDraft) sub._aiDraft = false
    },
    // 清空 editFieldValue 中 PCI 的 sub_questions（重置拆解用，避免 recompute 从旧数据恢复）
    // idx < 0 表示清空全部；否则只清空第 idx 条
    clearPciSubQuestions(idx = -1) {
      try {
        const v = JSON.parse(this.editFieldValue)
        const flat = []
        const collect = (node) => {
          if (!node || typeof node !== 'object') return
          if (Array.isArray(node)) { node.forEach(collect); return }
          if (Array.isArray(node.pci_list)) node.pci_list.forEach(p => flat.push(p))
          if (node.data && typeof node.data === 'object') collect(node.data)
        }
        collect(v)
        if (idx >= 0) {
          const t = flat[idx]
          if (t && typeof t === 'object') delete t.sub_questions
        } else {
          flat.forEach(p => { if (p && typeof p === 'object') delete p.sub_questions })
        }
        this.editFieldValue = JSON.stringify(v, null, 2)
      } catch (e) { /* 解析失败则忽略，recompute 会回退到 description 拆分 */ }
    },
    // 重置某条 PCI 的拆解状态
    resetPciSplit(i) {
      this.clearPciSubQuestions(i)
      this.pciSubQuestions[i] = []
      this.recomputeFlatPciItems()
      this.$message.info(`已重置第 ${i + 1} 条 PCI 的拆解`)
    },
    // 重置全部 PCI 的拆解状态
    resetAllPciSplit() {
      this.clearPciSubQuestions(-1)
      this.pciSubQuestions = []
      this.recomputeFlatPciItems()
      this.$message.info('已重置全部 PCI 拆解（按原文重新拆分）')
    },
    // 一键全部拆解所有 PCI 条目（并发，单条失败不影响整体）
    async splitAllPci() {
      this.allPciLoading = true
      try {
        await Promise.all(this.parsedPciItems.map((_, i) =>
          (this.pciSubQuestions[i] && this.pciSubQuestions[i].length)
            ? Promise.resolve()
            : this.splitAndAnswerPci(i)
        ))
        this.$message.success('所有 PCI 已拆解为子问题')
      } catch (e) {
        /* 单条错误已在 splitAndAnswerPci 内处理 */
      } finally {
        this.allPciLoading = false
        this.recomputeFlatPciItems()
      }
    },
    async savePciReplies() {
      const items = this.parsedPciItems.map((it, i) => {
        // 如果有子问题，汇总子问题的答案
        let answer = (this.pciReplies[i] || '').trim()
        const subs = this.pciSubQuestions[i] || []
        if (subs.length) {
          const subAnswers = subs
            .filter(s => s.answer && s.answer.trim())
            .map((s, si) => `[Q${si + 1}] ${s.question}\n    A: ${s.answer.trim()}`)
          if (subAnswers.length) {
            answer = subAnswers.join('\n\n')
          }
        }
        return {
          ...it,
          answer,
          resolution_condition: answer,
          // 同时保存子问题结构化数据
          sub_questions: subs.map(({ _loading, ...rest }) => rest),
        }
      })
      // 重组回原始结构：多模块时按 _moduleIdx/_listIdx 精确回填各自模块的 pci_list（不再全塞第一个模块）
      let wrapper = {}
      try { wrapper = JSON.parse(this.editFieldValue) } catch (e) { wrapper = {} }
      const strip = ({ _raw, _idx, _moduleIdx, _listIdx, _module, ...rest }) => rest
      wrapper = this.writeBackModuleList(wrapper, items, 'pci_list', strip)
      const value = JSON.stringify(wrapper, null, 2)
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: 'pci_json', value },
        })
        this.proto.pci_json = value
        this.editFieldValue = value
        this.clearDraft('pci_json')
        this.$message.success('PCI 回复已保存')
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async saveRiskReplies() {
      const items = this.parsedRiskItems.map((it, i) => ({
        ...it,
        mitigation: (this.riskReplies[i] || '').trim(),
      }))
      let wrapper = {}
      try { wrapper = JSON.parse(this.editFieldValue) } catch (e) { wrapper = {} }
      const strip = ({ _raw, _idx, _moduleIdx, _listIdx, _module, ...rest }) => rest
      wrapper = this.writeBackModuleList(wrapper, items, 'risk_points', strip)
      const value = JSON.stringify(wrapper, null, 2)
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: 'risks_json', value },
        })
        this.proto.risks_json = value
        this.editFieldValue = value
        this.clearDraft('risks_json')
        this.$message.success('风险处置回复已保存')
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async saveEditDialog() {
      // PCI / 风险 字段已有专门的结构化保存逻辑（会把子问题 answer 汇总并写回 sub_questions），
      // 点「保存修改」时同样走结构化保存，避免只保存原始 JSON 导致子问题答案丢失。
      if (this.editField === 'pci_json') {
        await this.savePciReplies()
        this.editDialogVisible = false
        return
      }
      if (this.editField === 'risks_json') {
        await this.saveRiskReplies()
        this.editDialogVisible = false
        return
      }
      // 需求澄清（结构化模式）：走逐条结论保存，避免只保存原文导致结论丢失
      if (this.editField === 'clarification_log' && !this.editRawMode) {
        await this.saveClarifyEditItems()
        this.editDialogVisible = false
        return
      }
      // ── 合并测试点 final_testpoints_json：从 tpEditItems（已删除/已修改）重建并保存 ──
      if (this.editField === 'final_testpoints_json') {
        const { wrapped } = this.parseTpArray()
        // 剥离 _raw 内部字段，还原纯测试点对象
        const clean = (this.tpEditItems || []).map(tp => {
          const { _raw, ...rest } = tp
          return rest
        })
        // 兼容裸数组 / {testpoints:[...]} 包裹：写回原容器结构
        let value = ''
        try {
          const v = JSON.parse(this.editFieldValue || 'null')
          if (wrapped && v && typeof v === 'object') {
            v.testpoints = clean
            value = JSON.stringify(v, null, 2)
          } else {
            value = JSON.stringify(clean, null, 2)
          }
        } catch (e) {
          value = JSON.stringify(clean, null, 2)
        }
        try {
          await request({
            url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
            data: { field: 'final_testpoints_json', value },
          })
          this.proto.final_testpoints_json = value
          this.editFieldValue = value
          this.rebuildTpEditItems()
          this.clearDraft('final_testpoints_json')
          this.$message.success('合并测试点已保存')
        } catch (e) {
          this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
          return
        }
        this.editDialogVisible = false
        return
      }
      try {
        await request({
          url: `${this.base()}/${this.protoId}/edit/`, method: 'patch',
          data: { field: this.editField, value: this.editFieldValue },
        })
        this.proto[this.editField] = this.editFieldValue
        this.clearDraft(this.editField)
        this.$message.success('已保存修改')
        this.editDialogVisible = false
      } catch (e) {
        this.$message.error('保存失败：' + (e.response?.data?.detail || e.message))
      }
    },
    // ---------- 编辑弹窗：结构化视图 ----------
    // 通用：从可能的「多模块数组」结构中展开指定 listKey 的所有条目。
    // 兼容：[{module,data:{pci_list}}...] / [{module,pci_list}...] / {data:{pci_list}} / {pci_list} / 裸条目数组。
    // 每条附带 _moduleIdx（顶层模块索引，-1 表示单容器/裸数组）、_listIdx（模块内索引）、_module（模块名），供写回时精确定位。
    flattenModuleList(v, listKey) {
      const out = []
      const push = (list, moduleIdx, moduleName) => {
        if (!Array.isArray(list)) return
        list.forEach((item, idx) => out.push({
          ...(item && typeof item === 'object' ? item : { value: item }),
          _moduleIdx: moduleIdx,
          _listIdx: idx,
          _module: moduleName || (item && item.module) || '',
        }))
      }
      if (Array.isArray(v)) {
        // 判断是否为「模块包裹数组」：某元素含 listKey 或 data.listKey
        const wrapped = v.some(m => m && (Array.isArray(m[listKey]) || (m.data && Array.isArray(m.data[listKey]))))
        if (wrapped) {
          v.forEach((m, mi) => {
            const list = Array.isArray(m[listKey]) ? m[listKey]
              : (m && m.data && Array.isArray(m.data[listKey]) ? m.data[listKey] : null)
            push(list, mi, m && (m.module || (m.data && m.data.module)))
          })
        } else {
          push(v, -1, '')  // 裸条目数组
        }
      } else if (v && typeof v === 'object') {
        if (Array.isArray(v[listKey])) push(v[listKey], -1, v.module)
        else if (v.data && Array.isArray(v.data[listKey])) push(v.data[listKey], -1, v.module || v.data.module)
      }
      return out
    },
    // PCI 分级统计（用于批量确认按钮展示）
    pciStats() {
      const stats = { blocker: 0, high: 0, medium: 0, low: 0 }
      ;(this.flatPciItems || []).forEach(item => {
        const level = (item.pci && item.pci.impact) || 'low'
        if (stats[level] !== undefined) stats[level]++
        else stats.low++ // 未知级别归入 low
      })
      return stats
    },
    // 弹窗 PCI 渲染列表：根据 pciEditFilter 过滤，引用未变（仍可编辑 sub.answer）
    displayedPciItems() {
      const filter = this.pciEditFilter
      if (filter === 'all') return this.flatPciItems || []
      return (this.flatPciItems || []).filter(item => {
        const impact = ((item.pci && item.pci.impact) || 'low').toLowerCase()
        return impact === filter
      })
    },
    // 计算某字段原始串能解析出的结构化条目数（用于草稿有效性校验）。
    // 非结构化字段（如 testcases_json）无法用 flattenModuleList 判断，统一视为 1（有内容即有效）。
    _parseListLen(raw, field) {
      try {
        const v = JSON.parse(raw)
        if (field === 'pci_json') return this.flattenModuleList(v, 'pci_list').length
        if (field === 'risks_json') return this.flattenModuleList(v, 'risk_points').length
        return 1
      } catch (e) { return 0 }
    },
    // 把已编辑的条目按 _moduleIdx/_listIdx 精确写回原 wrapper（多模块时逐条回填对应模块的 list）
    writeBackModuleList(wrapper, items, listKey, stripInternal) {
      const multi = items.some(it => it._moduleIdx >= 0)
      if (multi && Array.isArray(wrapper)) {
        items.forEach(it => {
          const m = wrapper[it._moduleIdx]
          if (!m) return
          const target = Array.isArray(m[listKey]) ? m[listKey]
            : (m.data && Array.isArray(m.data[listKey]) ? m.data[listKey] : null)
          if (target && it._listIdx != null && target[it._listIdx] != null) {
            target[it._listIdx] = stripInternal(it)
          }
        })
        return wrapper
      }
      // 单容器/裸结构：整体替换
      const cleanItems = items.map(stripInternal)
      if (Array.isArray(wrapper) && wrapper.length && wrapper[0].data && Array.isArray(wrapper[0].data[listKey])) {
        wrapper[0].data[listKey] = cleanItems
      } else if (wrapper && wrapper.data && Array.isArray(wrapper.data[listKey])) {
        wrapper.data[listKey] = cleanItems
      } else if (Array.isArray(wrapper) && wrapper.length && Array.isArray(wrapper[0][listKey])) {
        wrapper[0][listKey] = cleanItems
      } else {
        if (!wrapper || typeof wrapper !== 'object' || Array.isArray(wrapper)) wrapper = {}
        wrapper[listKey] = cleanItems
      }
      return wrapper
    },
    isStructuredField(f) {
      return ['testcases_json', 'final_testpoints_json', 'module_split', 'smoke_json'].includes(f)
    },
    priTag(p) {
      const m = { P0: 'danger', P1: 'warning', P2: '', critical: 'danger', high: 'warning', medium: 'info', low: 'info' }
      return m[p] || ''
    },
    riskAnswer(risk) {
      if (!risk || typeof risk !== 'object') return ''
      return risk.mitigation || risk.answer || risk.resolution_condition || risk.resolution || ''
    },
    // 冒烟用例字段归一化：技能 p4 输出用 test_steps/pass_criteria/is_core，
    // 前端/测试用例统一用 steps/expected/is_smoke
    normalizeSmokeCase(tc) {
      if (!tc || typeof tc !== 'object') return tc
      return {
        ...tc,
        steps: tc.steps || tc.test_steps || [],
        expected: tc.expected || tc.pass_criteria || '',
        is_smoke: tc.is_smoke !== undefined ? tc.is_smoke : !!tc.is_core,
      }
    },
    // ---------- 通用 JSON 卡片辅助 ----------
    displayFields(item) {
      // 过滤掉内部字段和已单独展示的字段
      const skip = ['_raw', 'module', 'title', 'id']
      const obj = {}
      Object.keys(item).forEach(k => {
        if (!skip.includes(k) && item[k] != null) obj[k] = item[k]
      })
      return obj
    },
    fieldLabel(key) {
      const m = {
        type: '类型', priority: '优先级', impact: '影响',
        description: '描述', category: '分类',
        extended_test_points: '扩展测试点', related_testpoints: '关联测试点',
        data: '数据内容', schema_version: '版本',
        risk_points: '风险点', question: '问题',
        suggested_answer: '建议答案', resolution: '结论',
        status: '状态', score: '得分', suggestion: '建议',
        preconditions: '前置条件', steps: '步骤', expected: '预期结果',
      }
      return m[key] || key
    },
    qualityKeyLabel(key) {
      const m = {
        summary: '总体评价', score: '质量得分', passed: '是否通过',
        issues: '存在问题', suggestions: '改进建议', coverage: '覆盖率',
        completeness: '完整度', correctness: '正确性', clarity: '清晰度',
        total: '总计', detail: '明细', passed_count: '通过数',
        failed_count: '未通过数', rate: '通过率',
      }
      return m[key] || key
    },
    formatFieldValue(val) {
      if (Array.isArray(val)) return val.join(', ')
      if (typeof val === 'object') return JSON.stringify(val)
      return String(val)
    },
    // 取某阶段的答疑问答记录
    qaLogFor(stage) {
      try {
        return (JSON.parse(this.proto?.qa_log || '[]')).filter(i => i.stage === stage)
      } catch (e) { return [] }
    },
    async loadProjects() {
      this.loadingProjects = true
      try {
        const res = await request({ url: '/projects/all/', method: 'get' })
        this.projects = (Array.isArray(res.data) ? res.data : (res.data.results || []))
      } catch (e) {
        this.projects = []
      } finally {
        this.loadingProjects = false
      }
    },
    async loadHistory() {
      this.loadingHistory = true
      try {
        const params = {}
        if (this.filterProject) params.project = this.filterProject
        if (this.filterVersion) params.version = this.filterVersion
        const res = await request({ url: `${this.base()}/list/`, method: 'get', params })
        const data = res.data
        if (Array.isArray(data)) {
          // 兼容旧接口（直接返回数组）
          this.history = data
          this.isAdmin = false
        } else {
          this.history = (data && data.results) || []
          this.isAdmin = !!(data && data.is_admin)
        }
      } catch (e) {
        this.$message.error('加载历史失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.loadingHistory = false
      }
    },
    async onFilterProjectChange(pid) {
      this.filterVersion = null
      this.versions = []
      if (!pid) {
        this.loadHistory()
        return
      }
      try {
        const res = await request({ url: `/versions/projects/${pid}/versions/`, method: 'get' })
        this.versions = res.data.results || res.data || []
      } catch (e) {
        this.versions = []
      }
      this.loadHistory()
    },
    async onContinue(row) {
      // 二级页路由：跳转到画布工作页（独立 URL），由工作页 mounted 载入
      this.$router.push({ name: 'ModaoCanvas', params: { id: row.id } })
    },
    goBackList() {
      // 返回首页任务列表（modao-skill）
      this.$router.push({ name: 'ModaoSkill' })
    },
    goCanvasBoard() {
      // 进入画布任务看板（每个画布作为独立任务分支）
      this.$router.push({ name: 'ModaoCanvasBoard', params: { id: this.protoId } })
    },
    async onResetWorkflow(row) {
      try {
        await this.$confirm(
          `确认将「${row.title}」退回第一步？该任务的读取结果、结构化内容、澄清答案、测试点和用例将被清空；已采纳到用例库的数据不会删除。`,
          '重新走完整流程',
          { type: 'warning', confirmButtonText: '确认重置', cancelButtonText: '取消' }
        )
      } catch (e) { return }
      try {
        const res = await request({ url: `${this.base()}/${row.id}/reset/`, method: 'post' })
        this.stopPoll()
        this.protoId = row.id
        this.proto = res.data
        this.step = 0
        this.busy = false
        this.form = {
          source_type: row.source_type,
          url: row.source_type === 'modao' ? (row.url || '') : '',
          webshare_url: row.source_type === 'webshare' ? (row.url || '') : '',
          cookies: '', requirement_text: '', title: row.title,
          project: row.project || null, version: row.version || null, dual_fusion: true,
        }
        await this.loadHistory()
        // 重置后项目下拉保持不变但版本下拉框被 onProjectChange 清空 → 重新拉一次
        if (this.form.project) {
          await this.onProjectChange(this.form.project)
        }
        this.$message.success('已回到第一步，请点击"重新读取当前任务"')
      } catch (e) {
        this.$message.error('重置失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async onTerminate() {
      if (!this.protoId) return
      try {
        await this.$confirm(
          '确认终止当前正在运行的任务？任务将立即停止，已产生的部分结果会被标记为失败，可随后「重走」或「终止」后新建。',
          '终止任务',
          { type: 'warning', confirmButtonText: '确认终止', cancelButtonText: '取消' }
        )
      } catch (e) { return }
      this.terminating = true
      try {
        await request({ url: `${this.base()}/${this.protoId}/terminate/`, method: 'post' })
        this.stopPoll()
        this.busy = false
        this.$message.success('已发送终止信号，任务即将停止')
        await this.refresh()
      } catch (e) {
        this.$message.error('终止失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.terminating = false
      }
    },
    async onReExtract(targetPageNames) {
      if (this.isRunning || this.busy) return
      if (!this.protoId) return this.onCreate()
      this.busy = true
      try {
        const urls = this.form.source_type === 'webshare'
          ? (this.form.webshare_url || '').split('\n').map(s => s.trim()).filter(Boolean)
          : (this.form.url || '').split('\n').map(s => s.trim()).filter(Boolean)
        const payload = { ...this.form, urls }
        // 单画布重读：只重读指定画布（画布级回退到读取触发），其余画布保持不变
        if (targetPageNames && targetPageNames.length) {
          payload.target_page_names = targetPageNames
        }
        await request({ url: `${this.base()}/${this.protoId}/extract/`, method: 'post', data: payload })
        this.canvasConfirmed = false
        this.canvasActiveId = null
        this.startPoll()
      } catch (e) {
        this.busy = false
        this.$message.error('重新读取失败：' + (e.response?.data?.detail || e.message))
      }
    },
    onNew() {
      this.stopPoll()
      this.protoId = null
      this.proto = null
      this.step = 0
      this.busy = false
      this.smokeDecision = ''
      this.rejectEmail = ''
      this.clarifyItems = []
      this.clarifyReplies = []
      this.clarifyParsed = false
      this.form = { source_type: 'modao', url: '', webshare_url: '', cookies: '', requirement_text: '', title: '墨刀需求梳理', project: null, version: null, dual_fusion: true }
    },
    async onDelete(row) {
      try {
        await this.$confirm(`确认删除「${row.title}」？`, '提示', { type: 'warning' })
      } catch (e) { return }
      try {
        await request({ url: `${this.base()}/${row.id}/`, method: 'delete' })
        if (this.protoId === row.id) this.onNew()
        await this.loadHistory()
        this.$message.success('已删除')
      } catch (e) {
        this.$message.error('删除失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async onCreate() {
      if (this.isRunning || this.busy) return
      this.busy = true
      try {
        // 根据来源类型，把对应输入框按行拆成 urls 传给后端
        let urls = []
        if (this.form.source_type === 'modao') {
          urls = (this.form.url || '').split('\n').map(s => s.trim()).filter(Boolean)
        } else if (this.form.source_type === 'webshare') {
          urls = (this.form.webshare_url || '').split('\n').map(s => s.trim()).filter(Boolean)
        }
        const payload = { ...this.form, urls }
        const res = await request({ url: `${this.base()}/create/`, method: 'post', data: payload })
        this.protoId = res.data.id
        this.proto = res.data
        if (this.form.source_type === 'modao' || this.form.source_type === 'webshare') {
          await request({ url: `${this.base()}/${this.protoId}/extract/`, method: 'post', data: payload })
          this.startPoll()
        } else {
          // 文本来源：直接可进入阶段1
          this.busy = false
          this.goStep(1)
        }
      } catch (e) {
        this.busy = false
        this.$message.error('创建失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async runStage(action, extra = {}) {
      if (this.isRunning || this.busy) return
      this.busy = true
      try {
        await request({ url: `${this.base()}/${this.protoId}/${action}/`, method: 'post', data: { ...this.form, ...extra } })
        this.startPoll()
      } catch (e) {
        this.busy = false
        this.$message.error('启动失败：' + (e.response?.data?.detail || e.message))
      }
    },
    // 启动结构化前先落库当前画布勾选：勾选后不点「保存校验结果」直接点结构化时，
    // 后端 _selected_page_names() 会读不到勾选而回退全选，导致「只选1个画布、摘要却是整个版本」。
    async persistCanvasSelection() {
      if (this.isRunning || this.busy) return false
      const rawPages = (this.proto && this.proto.pages) || []
      if (!rawPages.some(c => c.selected && !c._delete)) {
        this.$message.warning('至少需勾选一个画布')
        return false
      }
      const dirty = rawPages.filter(c => c._dirty)
      if (dirty.length) {
        this.busy = true
        try {
          await request({
            url: `${this.base()}/${this.protoId}/update-pages/`,
            method: 'post',
            data: {
              pages: dirty.map(c => ({
                id: c.id,
                selected: c.selected,
                page_name: c.page_name,
                parent_name: c.parent_name || '',
                recognition_note: c.recognition_note || '',
                delete: !!c._delete,
              })),
            },
          })
        } catch (e) {
          this.busy = false
          this.$message.error('画布选择保存失败：' + (e.response?.data?.detail || e.message))
          return false
        }
        this.busy = false
      }
      return true
    },
    async onStruct() {
      if (!(await this.persistCanvasSelection())) return
      this.runStage('struct')
    },
    // 强制全量重跑：force=True 时后端忽略增量跳过，对所有勾选画布重新调用模型。
    async onReStructAll() {
      if (this.isRunning || this.busy) return
      if (!(await this.persistCanvasSelection())) return
      try {
        await this.$confirm('将忽略已生成的画布摘要，对所有勾选画布强制重新结构化（重新调用模型，耗时与费用更高）。确认继续？', '重新结构化全部', { type: 'warning' })
        this.runStage('struct', { force: true })
      } catch (e) { /* 用户取消 */ }
    },
    // 阶段1：预览将要导出的 XMind 结构（与后端导出层级一致）
    onPreviewXmind() {
      if (!this.proto || !this.proto.requirement_summary) {
        this.$message.warning('尚未生成结构化摘要，无法预览')
        return
      }
      let tree = []
      try {
        const doc = yamlLoad(this.proto.requirement_summary)
        const modules = (doc && doc.modules) || []
        const rootTitle = (this.proto.title || '需求结构化').trim() || '需求结构化'
        const root = { label: rootTitle, children: [] }
        modules.forEach((m) => {
          if (!m || !m.name) return
          const mNode = { label: m.name, children: [] }
          if (m.priority) mNode.children.push({ label: `⚑ 优先级: ${m.priority}` })
          if (m.priority_source) mNode.children.push({ label: `📌 优先级来源: ${m.priority_source}` })
          const kfs = m.key_features || []
          if (kfs.length) {
            const kfChildren = kfs.map((f) => {
              if (typeof f === 'string') return { label: f }
              // 新格式用 text 字段 + F-xxx 稳定编号；老格式是 title/name。
              // 不读 text 会让新版功能点在导出的思维导图里整体消失。
              const body = f.text || f.title || f.name || ''
              if (!body) return { label: '' }
              const id = typeof f.id === 'string' ? f.id : ''
              const note = f.desc || f.description
              return {
                label: (id ? `${id} ` : '') + body +
                  (note ? `（${note}）` : (f.involves_api === true ? '（涉及接口）' : ''))
              }
            }).filter((n) => n.label)
            if (kfChildren.length) mNode.children.push({ label: '🔑 关键特征', children: kfChildren })
          }
          const sps = m.sub_pages || []
          if (sps.length) {
            const spChildren = sps.map((s) => ({ label: typeof s === 'string' ? s : (s.title || s.name || '') }))
              .filter((n) => n.label)
            if (spChildren.length) mNode.children.push({ label: '📄 子页面', children: spChildren })
          }
          root.children.push(mNode)
        })
        tree = [root]
      } catch (e) {
        // 解析失败不阻断：弹出对话框并在树区展示原始文本，保证有可见反馈
        this.$message.warning('结构化摘要非标准 YAML，已按原文展示')
        tree = [{ label: (this.proto.title || '需求结构化'), children: [
          { label: '（原始摘要）', children: [{ label: this.proto.requirement_summary }] }
        ] }]
      }
      this.xmindPreviewTree = tree
      this.xmindPreviewVisible = true
    },
    // 阶段1：导出结构化摘要为可编辑 XMind
    async onExportXmind() {
      if (!this.proto || !this.proto.requirement_summary) {
        this.$message.warning('尚未生成结构化摘要，无法导出 XMind')
        return
      }
      this.xmindExporting = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/export-xmind/`,
          method: 'get',
          responseType: 'blob'
        })
        const blob = res.data
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        const safeTitle = (this.proto.title || 'requirement').replace(/[\\/:*?"<>|]/g, '_')
        a.href = url
        a.download = `${safeTitle}.xmind`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        window.URL.revokeObjectURL(url)
        this.$message.success('XMind 已导出，可在本地用 XMind 打开编辑')
      } catch (e) {
        this.$message.error('导出失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.xmindExporting = false
      }
    },
    triggerXmindUpload() {
      if (!this.proto || !this.proto.requirement_summary) {
        this.$message.warning('请先生成结构化摘要再上传 XMind')
        return
      }
      this.$refs.xmindFile && this.$refs.xmindFile.click()
    },
    async onXmindFileChange(e) {
      const file = e.target.files && e.target.files[0]
      if (!file) return
      this.xmindImporting = true
      try {
        const formData = new FormData()
        formData.append('file', file)
        const res = await request({
          url: `${this.base()}/${this.protoId}/import-xmind/`,
          method: 'post',
          data: formData,
          headers: { 'Content-Type': 'multipart/form-data' }
        })
        // 用返回的最新摘要刷新本地展示
        if (res.data && res.data.requirement_summary) {
          this.proto.requirement_summary = res.data.requirement_summary
        } else {
          const detail = await request({ url: `${this.base()}/${this.protoId}/`, method: 'get' })
          if (detail && detail.data) this.proto = detail.data
        }
        this.$message.success(res.data?.detail || 'XMind 已覆盖结构化摘要')
      } catch (err) {
        const detail = err.response?.data?.detail || err.message
        this.$message.error('上传失败：' + detail)
      } finally {
        this.xmindImporting = false
        e.target.value = ''  // 允许重复选同一文件
      }
    },
    onClarify() { this.runStage('clarify') },
    onDesign() { this.runStage('design') },
    async onGenerateCases() {
      try {
        await this.$confirm(
          '请确认：所有位置、交互和需求疑问均已由人工给出结论，且当前合并测试点可用于生成最终测试用例。确认后系统会记录批准人和当前内容指纹；后续编辑会使本次批准失效。',
          '人工批准生成最终测试用例', { type: 'warning', confirmButtonText: '确认并批准生成' })
      } catch (e) { return }
      this.busy = true
      try {
        await request({
          url: `${this.base()}/${this.protoId}/approve-case-generation/`, method: 'post', data: {},
        })
        await request({
          url: `${this.base()}/${this.protoId}/generate-cases/`, method: 'post', data: this.form,
        })
        this.startPoll()
      } catch (e) {
        this.busy = false
        const data = e.response?.data || {}
        const reasons = data.generation_gate?.reasons || []
        this.$message.error(data.detail || reasons.join('；') || e.message)
        await this.refresh()
      }
    },
    async loadProjects() {
      this.loadingProjects = true
      try {
        const res = await request({ url: '/projects/all/', method: 'get' })
        console.log('[loadProjects] res.data:', JSON.stringify(res.data).slice(0, 300))
        this.projects = res.data.results || res.data || []
        console.log('[loadProjects] projects count:', this.projects.length)
      } catch (e) {
        console.error('[loadProjects] error:', e)
        this.projects = []
        this.$message.error('加载项目失败: ' + (e.response?.data?.detail || e.message))
      } finally {
        this.loadingProjects = false
      }
    },
    async onProjectChange(pid) {
      this.form.version = null
      this.versions = []
      if (!pid) return
      try {
        const res = await request({ url: `/versions/projects/${pid}/versions/`, method: 'get' })
        this.versions = res.data.results || res.data || []
      } catch (e) {
        this.versions = []
      }
    },
    // ---------- 快速新建项目/版本 ----------
    async createQuickProject() {
      try {
        const res = await request({
          url: '/projects/', method: 'post',
          data: { name: this.quickProjectName.trim(), description: (this.quickProjectDesc || '').trim() || undefined },
        })
        const p = res.data
        await this.loadProjects()
        this.form.project = p.id
        this.showQuickProject = false
        this.quickProjectName = ''
        this.quickProjectDesc = ''
        this.$message.success('已创建项目：' + p.name)
      } catch (e) {
        this.$message.error('创建失败：' + (e.response?.data?.name?.[0] || e.response?.data?.detail || e.message))
      }
    },
    async createQuickVersion() {
      if (!this.form.project) {
        this.$message.warning('请先选择归属项目')
        return
      }
      try {
        const res = await request({
          url: '/versions/', method: 'post',
          data: { name: this.quickVersionName.trim(), project_ids: [this.form.project] },
        })
        const v = res.data
        // 刷新版本列表
        const vRes = await request({ url: `/versions/projects/${this.form.project}/versions/`, method: 'get' })
        this.versions = vRes.data.results || vRes.data || []
        this.form.version = v.id
        this.showQuickVersion = false
        this.quickVersionName = ''
        this.$message.success('已创建版本：' + v.name)
      } catch (e) {
        this.$message.error('创建失败：' + (e.response?.data?.name?.[0] || e.response?.data?.detail || e.message))
      }
    },
    // ── A2-c：画布级用例采纳 ──
    pageCaseGroups(pg) {
      try {
        const raw = pg.page_testcases_json
        if (!raw) return []
        const v = JSON.parse(raw)
        const arr = Array.isArray(v) ? v : []
        const groups = {}
        arr.forEach((tc, rawIdx) => {
          const real = tc.testcase || tc
          const mod = (tc.module || real.module || real.feature_module) || '未分组'
          if (!groups[mod]) groups[mod] = []
          groups[mod].push({ ...real, module: mod, _rawIndex: rawIdx })
        })
        return Object.keys(groups).map(k => ({ module: k, items: groups[k] }))
      } catch (e) { return [] }
    },
    pageCaseCount(pg) {
      try {
        const v = JSON.parse(pg.page_testcases_json || '[]')
        return Array.isArray(v) ? v.length : 0
      } catch (e) { return 0 }
    },
    async approvePage(pg) {
      this.approvePagesLoading = true
      try {
        await request({
          url: `${this.base()}/${this.protoId}/approve-pages/`, method: 'post',
          data: { page_ids: [pg.id] },
        })
        this.$message.success(`已采纳画布「${pg.page_name}」`)
        await this.refresh()
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.approvePagesLoading = false
      }
    },
    async approveAllPages() {
      this.approvePagesLoading = true
      try {
        await request({
          url: `${this.base()}/${this.protoId}/approve-pages/`, method: 'post',
          data: { approve_all_selected: true },
        })
        this.$message.success('已采纳全部已生成画布，汇总已更新')
        await this.refresh()
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.approvePagesLoading = false
      }
    },
    async adoptToLibrary(includeSmoke) {
      if (!this.protoId) return
      if (!this.proto || !this.proto.project) {
        this.$message.warning('请先在阶段0关联项目，再采纳到用例库')
        return
      }
      this.adoptLoading = true
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/adopt/`, method: 'post',
          data: { include_smoke: !!includeSmoke },
        })
        const d = res.data || {}
        let msg = `已采纳 ${d.testcases} 条用例、自动创建 ${d.modules} 个功能模块到用例库`
        if (d.smoke) msg += `，冒烟 ${d.smoke} 条`
        if (d.skipped) msg += `（跳过已存在 ${d.skipped} 条）`
        this.$message.success(msg)
        await this.refresh()
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
      } finally {
        this.adoptLoading = false
      }
    },
    async adoptSingleCase(caseIndex, caseKey) {
      if (!this.protoId) return
      if (!this.proto || !this.proto.project) {
        this.$message.warning('请先在阶段0关联项目，再采纳到用例库')
        return
      }
      // 防重复点击
      if (this.adoptedCaseMap[caseKey]) return
      try {
        const res = await request({
          url: `${this.base()}/${this.protoId}/adopt-single/`,
          method: 'post',
          data: { index: caseIndex },
        })
        const d = res.data || {}
        // 标记已采纳（Vue 3 无需 $set，直接赋值即可触发响应式更新）
        this.adoptedCaseMap[caseKey] = true
        this.$message.success(d.detail || '已采纳')
      } catch (e) {
        this.$message.error('采纳失败：' + (e.response?.data?.detail || e.message))
      }
    },
    async onConfirm(stage) {
      // 确认前若有未保存的编辑，先落库
      if (this.editingField) {
        await this.saveEdit()
      }
      try {
        const res = await request({ url: `${this.base()}/${this.protoId}/confirm/`, method: 'post', data: { stage } })
        await this.refresh()
        this.showSync(res.data.sync, stage)
        if (stage === 3) this.showKbSync(res.data.kb_sync)
        // 阶段2 确认后折叠该卡片（仍可展开修改，不再整块隐藏导致失去修改入口）
        if (stage === 2) this.stage2Collapsed = true
        this.goStep(stage + 1) // 进入下一阶段
      } catch (e) {
        this.$message.error('确认失败：' + (e.response?.data?.detail || e.message))
      }
    },
    showKbSync(kb) {
      if (!kb || !kb.created_count) return
      this.$message.success(`已自适应沉淀 ${kb.created_count} 条新知识到项目知识库（跳过已存在 ${kb.skipped_count} 条）`)
    },
    showSync(sync, stage) {
      if (!sync) {
        if (stage === 1 && !this.proto.project) {
          this.syncMsg = '未选择归属项目，未同步功能模块（可在阶段0补充项目后重新确认）'
        }
        return
      }
      if (stage === 1) {
        this.syncMsg = `已自动同步功能模块「${sync.name}」`
        this.$message.success(this.syncMsg)
      } else if (stage === 3) {
        this.syncMsg = `已同步 ${sync.modules} 个子模块 / ${sync.testpoints} 条测试点 / ${sync.testcases} 条用例到功能模块`
        this.$message.success(this.syncMsg)
      }
    },
    async onSmoke() {
      try {
        await request({
          url: `${this.base()}/${this.protoId}/smoke/`, method: 'post',
          data: { decision: this.smokeDecision, reject_email: this.rejectEmail },
        })
        await this.refresh()
        this.$message.success('冒烟决策已记录')
      } catch (e) {
        this.$message.error('提交失败：' + (e.response?.data?.detail || e.message))
      }
    },
    onDownload() {
      window.open(`${this.base()}/${this.protoId}/excel/`, '_blank')
    },
  },
  mounted() {
    this.loadProjects().then(async () => {
      const rawId = this.id || (this.$route.params && this.$route.params.id) || (this.$route.query && (this.$route.query.id || this.$route.query.protoId))
      const rid = (rawId !== undefined && rawId !== null && String(rawId) !== 'new') ? rawId : null
      if (rid) {
        try {
          const res = await request({ url: `${this.base()}/${rid}/`, method: 'get' })
          if (res.data && res.data.id) {
            this.protoId = res.data.id
            this.proto = res.data
            this.step = res.data.current_stage || 0
            // 从任务列表「继续」进入时，只要任务还处于可编辑的早期阶段，就重新展示画布校验面板，
            // 避免之前点过「保存校验/跳过」导致 canvasConfirmed=true 后回退无法再选画布。
            // partial（读取超时中断）同样要露出校验面板与「继续读取」按钮。
            if (['pending', 'extracted', 'partial', 'structuring', 'clarifying', 'designing'].includes(this.proto.status)) {
              this.canvasConfirmed = false
            }
            const s = res.data.status
            if (['extracting', 'structuring', 'clarifying', 'designing'].includes(s)) {
              this.busy = true
              this.startPoll()
            } else {
              this.busy = false
              this.stopPoll()
            }
            await this.refresh()
            return
          }
        } catch (e) {
          this.$message.error('加载指定历史任务失败：' + (e.response?.data?.detail || e.message))
        }
      }
      // 无 id（或 id='new'）：空态（新建任务），项目列表已加载
    })
  },
  computed: {
    // 合并 DB 画布（selected/recognition_note）与 extracted_json 中的识别质量指标
    canvasPages() {
      const raw = (this.proto && this.proto.pages) || []
      let metaMap = {}
      try {
        const ex = this.proto && this.proto.extracted_json
        const data = typeof ex === 'string' ? JSON.parse(ex) : ex
        const pages = (data && data.pages) || []
        pages.forEach(p => { metaMap[p.page_no] = p })
      } catch (e) { metaMap = {} }
      return raw.map(p => {
        const m = metaMap[p.page_no] || {}
        return {
          ...p,
          visionAttempted: !!m.vision_attempted,
          visionSuccess: !!m.vision_success,
          warnings: m.extraction_warnings || [],
          duplicate_of_page: m.duplicate_of_page || null,
        }
      })
    },
    canvasActive() {
      return this.canvasPages.find(c => c.id === this.canvasActiveId) || null
    },
    // 当前画布可预览的截图列表：优先分段图（超长画布），否则单张主图
    activeShots() {
      const ca = this.canvasActive
      if (!ca) return []
      const segs = ca.screenshot_segments
      if (Array.isArray(segs) && segs.length) return segs
      return ca.screenshot ? [ca.screenshot] : []
    },
    // 当前展示的分段图（受缩略图切换控制）
    activeShot() {
      const shots = this.activeShots
      if (!shots.length) return ''
      const idx = Math.min(this.activeShotIdx || 0, shots.length - 1)
      return shots[idx]
    },
    canvasAllSelected() {
      return this.canvasPages.length > 0 && this.canvasPages.every(c => c.selected)
    },
    checkAll: {
      get() {
        return this.canvasAllSelected
      },
      set(val) {
        this.toggleSelectAll(val)
      }
    },
    canvasSelectedCount() {
      return this.canvasPages.filter(c => c.selected).length
    },
    // 已读取但未走完所有阶段（current_stage<4）的画布数，用于「继续运行」引导
    incompleteCanvasCount() {
      return this.canvasPages.filter(c => (c.current_stage || 0) < 4).length
    },
    // 任务已整体完成（done），但仍有画布未走完所有阶段：可继续勾选补跑
    canContinueCanvas() {
      return this.proto && this.proto.status === 'done' && this.incompleteCanvasCount > 0
    },
    canvasIndeterminate() {
      const cnt = this.canvasSelectedCount
      return cnt > 0 && cnt < this.canvasPages.length
    },
    isAutoFiltered() {
      // 后端读取时自动判定为无效画布（recognition_note 以「自动过滤」开头）
      return (c) => !!(c && c.recognition_note && String(c.recognition_note).startsWith('自动过滤'))
    },
    filterVersions() {
      // 历史列表「版本」下拉只展示所选项目下的版本
      if (!this.filterProject) return []
      return this.versions.filter(v => v.project === this.filterProject || v.project_id === this.filterProject || String(v.project) === String(this.filterProject))
    },
    // 以服务端 proto.status 为权威依据：任务运行中（含未终止）则禁止再次触发，
    // 防止连点/刷新后组件重挂导致 busy 被清零而并发启动多个 run。
    isRunning() {
      return ['extracting', 'structuring', 'clarifying', 'designing'].includes(this.proto?.status)
    },
    clarificationUnresolvedCount() {
      return this.proto?.generation_gate?.unresolved_count || 0
    },
    stage2Confirmed() {
      return !!(this.proto?.generation_gate?.stage2_confirmed)
    },
    generationGateReasons() {
      return this.proto?.generation_gate?.reasons || []
    },
    // 解析质量自检报告，汇总 FAIL/WARNING 检查项，转为自然语言供失败时直观展示
    qualityGateSummary() {
      const raw = this.proto && this.proto.quality_report_json
      if (!raw) return []
      let d
      try { d = typeof raw === 'string' ? JSON.parse(raw) : raw } catch (e) { return [] }
      const qr = (d && d.quality_report) || d
      if (!qr || typeof qr !== 'object') return []
      const out = []
      const overall = String(qr.overall_result || '').toUpperCase()
      if (overall === 'FAIL') out.push('总体评价：未通过质量门禁')
      else if (overall === 'WARNING') out.push('总体评价：存在警告项')
      // 检查项简称 → 中文标签映射
      const checkLabels = {
        C1_completeness: '完整性', C2_smoke_ratio: '冒烟比例',
        C3_p0_main_flow_coverage: 'P0主流程覆盖', C4_duplication: '重复率',
        C5_priority_distribution: '优先级分布', C6_description_length: '描述长度'
      }
      const checks = qr.checks || {}
      for (const [cid, cval] of Object.entries(checks)) {
        if (cval && (cval.result === 'FAIL' || cval.result === 'WARNING')) {
          const label = checkLabels[cid] || cid
          const issues = cval.issues || []
          // 从 issue 对象中提取自然语言描述，去掉字段名噪音
          const toText = (i) => {
            if (!i) return ''
            if (typeof i === 'string') return i
            // 优先取 detail / issue 字段（已是自然语言）
            const msg = (i.detail || i.issue || i.message || '')
            if (typeof msg === 'string' && msg.trim()) return msg.trim()
            // 处理 {case_id, missing_fields} 等结构化对象 → 转自然语言
            const parts = []
            if (i.case_id) parts.push(`用例 ${i.case_id}`)
            if (Array.isArray(i.missing_fields) && i.missing_fields.length) {
              parts.push(`缺少字段：${i.missing_fields.join('、')}`)
            }
            if (parts.length) return parts.join('，')
            try { return JSON.stringify(i) } catch { return String(i) }
          }
          const parts = issues.map(toText).filter(Boolean)
          if (parts.length) {
            const statusTag = cval.result === 'FAIL' ? '❌' : '⚠️'
            out.push(`${statusTag} ${label}：${parts.join('；')}`)
          } else {
            const statusTag = cval.result === 'FAIL' ? '❌' : '⚠️'
            out.push(`${statusTag} ${label}`)
          }
        }
      }
      return out
    },
    // PCI 分级统计（用于批量确认按钮展示）
    pciStats() {
      const stats = { blocker: 0, high: 0, medium: 0, low: 0 }
      ;(this.flatPciItems || []).forEach(item => {
        const level = (item.pci && item.pci.impact) || 'low'
        if (stats[level] !== undefined) stats[level]++
        else stats.low++ // 未知级别归入 low
      })
      return stats
    },
    // 弹窗 PCI 渲染列表：根据 pciEditFilter 过滤，引用未变（仍可编辑 sub.answer）
    displayedPciItems() {
      const filter = this.pciEditFilter
      if (filter === 'all') return this.flatPciItems || []
      return (this.flatPciItems || []).filter(item => {
        const impact = ((item.pci && item.pci.impact) || 'low').toLowerCase()
        return impact === filter
      })
    },
    hasAnyArtifact() {
      const p = this.proto
      return p && (p.module_split || p.risks_json || p.pci_json || p.final_testpoints_json || p.testcases_json || p.smoke_json || p.quality_report_json)
    },
    // 判断 PCI 子问题是否有已填/已确认的未落库内容
    hasPciUnsavedContent() {
      const subs = this.pciSubQuestions || []
      return subs.some(arr => Array.isArray(arr) && arr.some(s => (s.answer && s.answer.trim()) || s.confirmed))
    },
    formattedValue() {
      try {
        const v = JSON.parse(this.editFieldValue)
        return JSON.stringify(v, null, 2)
      } catch (e) {
        return this.editFieldValue
      }
    },
    parsedJsonArray() {
      // 先按 JSON 解析；失败再按 YAML 兜底（module_split 等产物是 YAML 格式）
      let v = null
      try {
        v = JSON.parse(this.editFieldValue)
      } catch (e) {
        try { v = yamlLoad(this.editFieldValue) } catch (e2) { return [] }
      }
      if (Array.isArray(v)) return v.map(item => ({ ...item, _raw: JSON.stringify(item, null, 2) }))
      if (v && typeof v === 'object') {
        // {modules: [...]} 之类的单键数组容器：展开为每个模块一张卡片
        if (Array.isArray(v.modules)) {
          return v.modules.map(item => ({ ...item, _raw: JSON.stringify(item, null, 2) }))
        }
        return [{ ...v, _raw: JSON.stringify(v, null, 2) }]
      }
      return []
    },
    parsedTestcases() {
      let arr = this.parsedJsonArray
      // 冒烟用例产物可能是 {smoke_overview, smoke_testcases, execution_suggestion}，需取出 smoke_testcases 数组
      if (this.editField === 'smoke_json') {
        try {
          const v = JSON.parse(this.editFieldValue)
          const list = (v && Array.isArray(v.smoke_testcases)) ? v.smoke_testcases
            : (Array.isArray(v) ? v : [])
          arr = list.map(tc => ({ ...this.normalizeSmokeCase(tc), _raw: JSON.stringify(tc, null, 2) }))
        } catch (e) { arr = [] }
      }
      if (!arr.length) return []
      // 每条可能是 { testcase: {...}, module, _raw } 嵌套包裹，或扁平用例对象
      const groups = {}
      arr.forEach((tc) => {
        const real = tc.testcase || tc
        // 去掉内部 _raw，重新生成干净的原始 JSON（避免嵌套转义串越叠越大）
        const clean = {}
        Object.keys(real).forEach(k => { if (k !== '_raw') clean[k] = real[k] })
        const mod = (tc.module || real.module || real.feature_module) || '未分组'
        if (!groups[mod]) groups[mod] = []
        groups[mod].push({ ...clean, module: mod, _raw: JSON.stringify(clean, null, 2) })
      })
      return Object.keys(groups).map(k => ({ module: k, items: groups[k] }))
    },
    parsedTestpoints() {
      const arr = this.parsedJsonArray
      return arr.map(tp => ({ ...tp, _raw: JSON.stringify(tp, null, 2) }))
    },
    // PCI：从嵌套的 pci_list 中展开每条（兼容多种结构）
    // 可能是: {pci_list:[...]} / {data:{pci_list:[...]}} / [{data:{pci_list:[...]}}] / [{pci_list:[...]}]
    parsedPciItems() {
      try {
        const v = JSON.parse(this.editFieldValue)
        // 遍历「所有模块」展开 pci_list（不再只取第一个模块）
        return this.flattenModuleList(v, 'pci_list').map((item, idx) => {
          const { _moduleIdx, _listIdx, _module, ...pure } = item
          return { ...item, _raw: JSON.stringify(pure, null, 2), _idx: idx }
        })
      } catch (e) { return [] }
    },
    // 弹窗 PCI 子问题扁平列表（优先用方法层维护的真实对象，兜底纯计算避免回退通用 JSON 视图）
    flatPciItems() {
      if (this.flatPciItemsList && this.flatPciItemsList.length) {
        return this.flatPciItemsList
      }
      // flatPciItemsList 尚未就绪（极端时序）：异步补算为真实对象，下一帧即替换本帧的临时对象，
      // 从而恢复可输入状态；本帧仅用纯计算结果占位，避免回退到通用 JSON 视图。
      if (this.parsedPciItems && this.parsedPciItems.length) {
        this.$nextTick(() => this.recomputeFlatPciItems())
      }
      // 兜底：只读计算生成子问题卡片（不修改任何 data，避免 computed 副作用）
      const out = []
      const pcis = this.parsedPciItems || []
      pcis.forEach((pci, i) => {
        const desc = (pci.description || '').trim()
        const parts = desc ? desc.split(/\s*(?:且|以及|；|,)\s*/).map(s => s.trim()).filter(Boolean) : ['(无描述内容)']
        const items = (parts.length ? parts : [desc || '(无描述内容)']).map(q => ({
          question: q,
          answer: pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || '',
          confirmed: false,
          _loading: false,
        }))
        items.forEach((sub, j) => out.push({ pci, pciIndex: i, sub: sub, subIndex: j }))
      })
      return out
    },
    // PCI 分级统计（用于批量确认按钮展示）
    pciStats() {
      const stats = { blocker: 0, high: 0, medium: 0, low: 0 }
      ;(this.flatPciItems || []).forEach(item => {
        const level = (item.pci && item.pci.impact) || 'low'
        if (stats[level] !== undefined) stats[level]++
        else stats.low++ // 未知级别归入 low
      })
      return stats
    },
    // 弹窗 PCI 渲染列表：根据 pciEditFilter 过滤，引用未变（仍可编辑 sub.answer）
    displayedPciItems() {
      const filter = this.pciEditFilter
      if (filter === 'all') return this.flatPciItems || []
      return (this.flatPciItems || []).filter(item => {
        const impact = ((item.pci && item.pci.impact) || 'low').toLowerCase()
        return impact === filter
      })
    },
    // 风险识别：从嵌套的 risk_points 中展开每条（兼容多种结构，同 PCI）
    parsedRiskItems() {
      try {
        const v = JSON.parse(this.editFieldValue)
        // 遍历「所有模块」展开 risk_points（不再只取第一个模块）
        return this.flattenModuleList(v, 'risk_points').map((item, idx) => {
          const { _moduleIdx, _listIdx, _module, ...pure } = item
          return { ...item, _raw: JSON.stringify(pure, null, 2), _idx: idx }
        })
      } catch (e) { return [] }
    },
    // ── 主页面展示用：基于 proto.xxx_json（非弹窗 editFieldValue）──
    mainPageParsedRisks() {
      try {
        const raw = this.proto && this.proto.risks_json
        if (!raw) return []
        // 遍历所有模块展开 risk_points（不再只取第一个模块）
        return this.flattenModuleList(JSON.parse(raw), 'risk_points')
      } catch (e) { return [] }
    },
    mainPageParsedPci() {
      try {
        const raw = this.proto && this.proto.pci_json
        if (!raw) return []
        // 遍历所有模块展开 pci_list（不再只取第一个模块）
        return this.flattenModuleList(JSON.parse(raw), 'pci_list')
      } catch (e) { return [] }
    },
    // 主页面 PCI 扁平展示（同 flatPciItems 逻辑，但基于 proto）
    // 优先使用保存的结构化子问题 sub_questions，使每个子问题分别展示各自独立的答案；
    // 无 sub_questions 的旧数据回退到「按描述拆分 + 共用 PCI 汇总 answer」。
    mainPageFlatPci() {
      const out = []
      const pcis = this.mainPageParsedPci || []
      const filter = this.mainPagePciFilter
      pcis.forEach((pci, i) => {
        // 优先级筛选
        const impact = (pci.impact || 'low').toLowerCase()
        if (filter !== 'all' && filter !== impact) return
        const subs = pci.sub_questions
        const fallbackAnswer = pci.answer || pci.suggested_answer || pci.resolution_condition || pci.resolution || ''
        if (Array.isArray(subs) && subs.length) {
          subs.forEach((s, j) => {
            out.push({
              pci, pciIndex: i,
              question: s.question || '',
              subIndex: j,
              // 兼容旧数据：历史版本可能只保存 PCI 顶层结论，
              // 同时留下 answer 为空的 sub_questions，外层展示需回退顶层答案。
              answer: s.answer || fallbackAnswer,
            })
          })
        } else {
          const desc = (pci.description || '').trim()
          const parts = desc ? desc.split(/\s*(?:且|以及|；|,)\s*/).map(s=>s.trim()).filter(Boolean) : ['(无描述内容)']
          const answer = fallbackAnswer
          ;(parts.length ? parts : [desc || '(无描述内容)']).forEach((q, j) => {
            out.push({ pci, pciIndex: i, question: q, subIndex: j, answer })
          })
        }
      })
      return out
    },
    // 主卡片 PCI 分级统计（基于全部 PCI，不受筛选影响）
    mainPagePciStats() {
      const stats = { blocker: 0, high: 0, medium: 0, low: 0, all: 0 }
      const pcis = this.mainPageParsedPci || []
      pcis.forEach(pci => {
        const impact = (pci.impact || 'low').toLowerCase()
        if (stats[impact] !== undefined) stats[impact]++
        else stats.low++
      })
      stats.all = pcis.length
      return stats
    },
    // PCI 分级统计（用于批量确认按钮展示）
    pciStats() {
      const stats = { blocker: 0, high: 0, medium: 0, low: 0 }
      ;(this.flatPciItems || []).forEach(item => {
        const level = (item.pci && item.pci.impact) || 'low'
        if (stats[level] !== undefined) stats[level]++
        else stats.low++ // 未知级别归入 low
      })
      return stats
    },
    // 弹窗 PCI 渲染列表：根据 pciEditFilter 过滤，引用未变（仍可编辑 sub.answer）
    displayedPciItems() {
      const filter = this.pciEditFilter
      if (filter === 'all') return this.flatPciItems || []
      return (this.flatPciItems || []).filter(item => {
        const impact = ((item.pci && item.pci.impact) || 'low').toLowerCase()
        return impact === filter
      })
    },
    // 弹窗 PCI 渲染列表：根据 pciEditFilter 过滤，引用未变（仍可编辑 sub.answer）
    displayedPciItems() {
      const filter = this.pciEditFilter
      if (filter === 'all') return this.flatPciItems || []
      return (this.flatPciItems || []).filter(item => {
        const impact = ((item.pci && item.pci.impact) || 'low').toLowerCase()
        return impact === filter
      })
    },
    // ── 主页面：合并去重后测试点 ──
    mainPageTestpoints() {
      try {
        const raw = this.proto && this.proto.final_testpoints_json
        if (!raw) return []
        const v = JSON.parse(raw)
        return Array.isArray(v) ? v : (Array.isArray(v.testpoints) ? v.testpoints : [])
      } catch (e) { return [] }
    },
    // ── 主页面：测试用例（按模块分组，兼容嵌套包裹）──
    mainPageTestcases() {
      try {
        const raw = this.proto && this.proto.testcases_json
        if (!raw) return []
        const v = JSON.parse(raw)
        const arr = Array.isArray(v) ? v : []
        const groups = {}
        let total = 0
        arr.forEach((tc, rawIdx) => {
          const real = tc.testcase || tc
          const mod = (tc.module || real.module || real.feature_module) || '未分组'
          if (!groups[mod]) groups[mod] = []
          groups[mod].push({ ...real, module: mod, _rawIndex: rawIdx })
          total++
        })
        return { groups: Object.keys(groups).map(k => ({ module: k, items: groups[k] })), total }
      } catch (e) { return { groups: [], total: 0 } }
    },
    // ── A2-c：画布级用例（按选中子集分别生成，各自一份）──
    canvasWithCases() {
      try {
        const pages = (this.proto && this.proto.pages) || []
        return pages.filter(p => p.page_testcases_json && p.page_testcases_json.trim() && p.page_testcases_json.trim() !== '[]')
      } catch (e) { return [] }
    },
    mergedCaseCount() {
      try {
        const raw = this.proto && this.proto.merged_testcases_json
        if (!raw) return 0
        const v = JSON.parse(raw)
        return Array.isArray(v) ? v.length : 0
      } catch (e) { return 0 }
    },
    // ── 主页面：冒烟用例（同测试用例结构）──
    mainPageSmoke() {
      try {
        const raw = this.proto && this.proto.smoke_json
        if (!raw) return []
        const v = JSON.parse(raw)
        // 兼容技能原始输出 {smoke_overview, smoke_testcases, execution_suggestion} 或纯数组
        const src = Array.isArray(v) ? v
          : (v && Array.isArray(v.smoke_testcases) ? v.smoke_testcases : [])
        const arr = src.map(tc => this.normalizeSmokeCase(tc))
        const groups = {}
        let total = 0
        arr.forEach((tc, rawIdx) => {
          const real = tc.testcase || tc
          const mod = (tc.module || real.module || real.feature_module) || '未分组'
          if (!groups[mod]) groups[mod] = []
          groups[mod].push({ ...real, module: mod, _rawIndex: rawIdx })
          total++
        })
        return { groups: Object.keys(groups).map(k => ({ module: k, items: groups[k] })), total }
      } catch (e) { return { groups: [], total: 0 } }
    },
    // ── 主页面：质量自检报告 ──
    mainPageQuality() {
      try {
        const raw = this.proto && this.proto.quality_report_json
        if (!raw) return null
        return JSON.parse(raw)
      } catch (e) { return null }
    },
    // 历史筛选：按当前选中项目过滤出的版本列表（onFilterProjectChange 已填充 this.versions）
    filterVersions() {
      return this.versions || []
    },
  },
}
</script>

<style scoped>
.modao-skill { padding: 20px; max-width: none; }
.canvas-page-head {
  display: flex; align-items: center; gap: 12px;
  padding: 0 20px 12px; max-width: none;
}
.canvas-page-title { font-size: 15px; font-weight: 600; color: #1f2937; }
.page-header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.page-header h2 { margin: 0 4px 0 0; }
.header-actions { margin-left: auto; display: flex; gap: 8px; }
.history-card { margin: 16px 0; }
.history-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.history-filters { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.empty-hint { color: #999; font-size: 13px; padding: 8px 0; }
.subtitle { color: #888; margin: 0 0 16px; font-size: 13px; }
.steps { margin-bottom: 22px; }
.stage-card { margin-bottom: 16px; }
.md-box, .json-box {
  background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px;
  max-height: 460px; overflow: auto; font-size: 12.5px; line-height: 1.6; white-space: pre-wrap;
  margin-top: 12px;
}
.artifact { margin-top: 14px; }
/* 阶段3产物筛选标签页 */
.stage3-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }
.form-hint { font-size: 12px; color: #909399; line-height: 1.5; margin-top: 4px; }
.stage3-filter-bar { margin: 14px 0 6px; }
.stage3-tabs { flex-wrap: wrap; }
.stage3-tabs :deep(.el-radio-button__inner) { font-size: 12px; padding: 6px 10px; }
.stage3-tabs :deep(.el-radio-button:first-child .el-radio-button__inner) {
  border-left-color: #dcdfe6;
}
/* 逐条采纳按钮 */
.main-page-card-head .el-button.el-button--extra-mini {
  font-size: 11px; padding: 2px 8px; margin-left: auto; flex-shrink: 0;
}
.adopted-badge {
  font-size: 11px; color: #67c23a; margin-left: auto; white-space: nowrap;
}
.artifact h4 { margin: 0 0 4px; color: #1f2937; }
.artifact-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.artifact-head .hint { color: #9ca3af; font-size: 12px; }
.artifact-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.stage1-summary-container { margin-top: 10px; }
.clarify-item {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; margin-top: 12px;
  background: #fff;
}
.clarify-item-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.clarify-head-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 1;
}
.clarify-delete-btn {
  margin-left: auto;
}
.clarify-loc { color: #6b7280; font-size: 12px; }
.clarify-issue { color: #1f2937; font-size: 13px; line-height: 1.7; margin-top: 8px; }
.clarify-suggest { color: #374151; font-size: 12.5px; line-height: 1.7; margin-top: 6px; background: #f3f4f6; border-radius: 6px; padding: 6px 10px; }
.clarify-impact { color: #6b7280; font-size: 12px; margin-top: 6px; }
.edit-actions { margin-top: 8px; display: flex; gap: 8px; }
.mt { margin-top: 12px; }
.expand-detail { padding: 8px 16px; background: #f8f9fb; border-radius: 4px; }
.log-section { margin-bottom: 10px; }
.log-section h4 { margin: 0 0 6px; color: #1f2937; font-size: 13px; }
.log-box {
  background: #fff; color: #374151; padding: 10px; border-radius: 6px;
  max-height: 300px; overflow: auto; font-size: 12px; line-height: 1.7;
  white-space: pre-wrap; font-family: 'Courier New', monospace; border: 1px solid #e5e7eb;
}
.error-section h4 { color: #dc2626; }
.error-box { color: #dc2626; background: #fef2f2; border-color: #fecaca; }

/* 编辑产物弹窗 - 结构化视图 */
.edit-dlg-head { display: flex; align-items: center; margin-bottom: 12px; }
.structured-view { max-height: 62vh; overflow-y: auto; padding-right: 4px; }
.raw-mode { max-height: 62vh; overflow-y: auto; }
.mono-font .el-textarea__inner { font-family: 'Courier New', Consolas, monospace; font-size: 12px; line-height: 1.6; }
.tc-group { margin-bottom: 20px; }
.tc-group-title {
  font-size: 14px; color: #1f2937; background: #eff6ff;
  padding: 8px 12px; border-radius: 6px; border-left: 3px solid #3b82f6; margin-bottom: 10px;
}
.tc-card {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; margin-top: 8px;
  background: #fff; transition: box-shadow .15s;
}
.tc-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.08); }
.tc-card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.tc-card-head b { color: #1f2937; font-size: 13.5px; }
.tc-id { color: #6b7280; font-size: 11.5px; font-family: monospace; background: #f3f4f6; padding: 1px 6px; border-radius: 4px; }
.tc-card-body > div { font-size: 13px; line-height: 1.7; color: #374151; margin-bottom: 4px; }
.tc-steps { padding-left: 18px; margin: 6px 0; }
.tc-steps li { font-size: 13px; line-height: 1.7; color: #4b5563; margin-bottom: 2px; }
.tc-expected { color: #059669; }
.tc-ref { color: #4b5563; background: #f8fafc; border-radius: 6px; padding: 4px 8px; font-size: 12px; }
.tc-notes { color: #6b7280; font-size: 12.5px; }
.tp-card {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px;
  background: #fff;
}
.tp-card b { color: #1f2937; font-size: 13.5px; }
.tp-card p { color: #6b7280; font-size: 12.5px; margin: 4px 0 8px; line-height: 1.6; }
.json-box {
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;
  padding: 16px; font-family: 'Courier New', Consolas, monospace;
  font-size: 12px; line-height: 1.65; white-space: pre-wrap; word-break: break-all;
  max-height: 55vh; overflow-y: auto;
}

/* 通用 JSON 卡片列表 */
.generic-json-list { display: flex; flex-direction: column; gap: 12px; }
.generic-json-card {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px;
  background: #fff; transition: box-shadow .15s;
}
.generic-json-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,.07); }
.generic-card-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #f0f0f0;
}
.generic-card-index {
  background: #3b82f6; color: #fff; font-size: 11px; font-weight: 600;
  width: 24px; height: 24px; border-radius: 50%; display: inline-flex;
  align-items: center; justify-content: center;
}
.generic-card-title { color: #1f2937; font-size: 14px; font-weight: 600; }
.generic-card-id { color: #6b7280; font-size: 12px; font-family: monospace; }
.generic-card-body { display: flex; flex-direction: column; gap: 5px; }
.generic-field { display: flex; gap: 6px; font-size: 12.5px; line-height: 1.7; }
.generic-field-key {
  color: #6b7280; min-width: 90px; flex-shrink: 0;
  font-weight: 500; position: relative; padding-left: 10px;
}
.generic-field-key::before { content: '•'; position: absolute; left: 0; color: #93c5fd; }
.generic-field-val { color: #374151; word-break: break-all; }

/* ── 主页面结构化卡片（风险/PCI 只读展示）── */
.main-page-risk-list, .main-page-pci-list {
  display: flex; flex-direction: column; gap: 10px; margin-top: 8px;
}
.main-page-risk-card, .main-page-pci-card {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px;
  background: #fff; transition: box-shadow .15s;
}
.main-page-risk-card:hover, .main-page-pci-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.main-page-card-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 6px; padding-bottom: 6px; border-bottom: 1px solid #f3f4f6;
}
.main-page-card-edit { margin-left: auto; padding: 0 4px; }
.main-page-card-desc {
  color: #374151; font-size: 13px; line-height: 1.7;
}
.main-page-card-extra {
  color: #6b7280; font-size: 12px; margin-top: 6px; line-height: 1.6;
}
/* 区块标题行：标题 + 右侧编辑按钮 */
.artifact-head-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; margin-bottom: 4px;
}
.artifact-head-row h4 { margin: 0; color: #1f2937; }
.artifact-head-row .el-button { padding: 0 2px; }

/* 合并测试点卡片 */
.main-page-tp-list { display: flex; flex-direction: column; gap: 10px; margin-top: 8px; }
.main-page-tp-card {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px;
  background: #fff; transition: box-shadow .15s;
}
.main-page-tp-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.06); }

/* 测试用例 / 冒烟用例分组卡片 */
.main-page-tc-wrap { display: flex; flex-direction: column; gap: 14px; margin-top: 8px; }
.main-page-tc-group-title { margin: 0 0 8px; color: #374151; font-size: 14px; }
.main-page-tc-card {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 14px;
  background: #fff; margin-bottom: 10px; transition: box-shadow .15s;
}
.main-page-tc-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.main-page-tc-steps { margin: 6px 0 0; padding-left: 20px; color: #374151; font-size: 12.5px; line-height: 1.7; }
.tc-expected { color: #047857; }

/* 质量自检报告 */
.main-page-quality {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 16px;
  background: #fff; margin-top: 8px; display: flex; flex-direction: column; gap: 8px;
}
.main-page-quality-row { display: flex; gap: 10px; font-size: 13px; line-height: 1.6; }
.main-page-quality-key {
  color: #6b7280; min-width: 100px; flex-shrink: 0; font-weight: 500;
}
.main-page-quality-val { color: #374151; word-break: break-all; }
.quality-json {
  background: #f8fafc; border: 1px solid #eef2f7; border-radius: 6px;
  padding: 8px 10px; margin: 0; font-size: 12px; max-height: 240px; overflow: auto;
  white-space: pre-wrap; color: #334155;
}

/* PCI 卡片 */
.pci-card {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px;
  background: #fff; transition: box-shadow .15s;
}
.pci-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,.07); }
.pci-card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.pci-source { color: #6b7280; font-size: 12px; font-family: monospace; }
.pci-card-desc {
  color: #1f2937; font-size: 13px; line-height: 1.75; margin-bottom: 8px;
  padding: 8px 12px; background: #fefce8; border-radius: 6px; border-left: 3px solid #eab308;
}
.pci-q-badge {
  display: inline-block; margin-right: 6px; padding: 0 6px; background: #3b82f6; color: #fff;
  border-radius: 4px; font-size: 11px; font-weight: 600; vertical-align: middle;
}
.pci-reply-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.pci-reply-head .pci-reply-label { margin-bottom: 0; }
.pci-blocked { margin-bottom: 8px; }
.pci-blocked b { color: #dc2626; font-size: 12.5px; }
.pci-bs-tag {
  display: inline-block; margin: 2px 4px 2px 0; padding: 1px 8px;
  background: #fef2f2; color: #dc2626; border-radius: 4px; font-size: 11.5px;
}
.pci-reply-section { margin-top: 10px; padding-top: 10px; border-top: 1px dashed #e5e7eb; }
.pci-reply-label { display: block; font-size: 13px; font-weight: 600; color: #374151; margin-bottom: 6px; }

/* 风险卡片 */
.risk-card {
  border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px 16px;
  background: #fff; transition: box-shadow .15s;
}
.risk-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,.07); }
.risk-card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.risk-card-desc {
  color: #1f2937; font-size: 13px; line-height: 1.75; margin-bottom: 8px;
  padding: 8px 12px; background: #fef2f2; border-radius: 6px; border-left: 3px solid #ef4444;
}
.risk-related { margin-bottom: 8px; font-size: 12.5px; color: #4b5563; }

/* PCI 子问题拆解 */
.pci-split-btn-wrap {
  display: flex; align-items: center; gap: 10px;
  padding: 12px; background: #f0fdf4; border-radius: 8px;
  border: 1px dashed #86efac; margin-top: 10px;
}
.hint-text { color: #9ca3af; font-size: 12px; }
.pci-stat-inline { color: #6b7280; font-size: 11.5px; margin-left: 4px; }
.pci-stat-inline b { color: #374151; }
.pci-batch-actions { padding: 10px 0; border-top: 1px solid #f0f0f0; margin-top: 8px; }
.pci-sub-list { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
.pci-sub-card {
  border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px 14px;
  background: #fff; transition: all .2s;
}
.pci-sub-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.pci-sub-confirmed {
  background: #f0fdf4; border-color: #86efac;
  opacity: 0.85;
}
.pci-dup {
  opacity: 0.55;
}
.pci-sub-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.pci-sub-index {
  display: inline-block; width: 26px; height: 26px; line-height: 26px;
  text-align: center; background: #3b82f6; color: #fff;
  border-radius: 50%; font-size: 12px; font-weight: 700;
}
.pci-sub-q {
  font-size: 13px; line-height: 1.7; color: #1e3a8a;
  padding: 6px 10px; background: #eff6ff; border-radius: 6px;
  border-left: 3px solid #3b82f6; margin-bottom: 8px;
}
.pci-sub-a-wrap {
  display: flex; gap: 6px; align-items: flex-start;
  padding: 6px 10px; background: #f0fdf4; border-radius: 6px;
  border-left: 3px solid #10b981; margin-bottom: 8px;
}
.pci-sub-a-label {
  font-weight: 600; color: #059669; font-size: 13px;
  white-space: nowrap; margin-top: 4px;
}
/* 人工回答输入框（绿色调，与蓝色问题文案区分） */
.pci-answer-input { flex: 1; }
.pci-answer-input .el-textarea__inner {
  background: #ecfdf5; border-color: #86efac; color: #065f46;
  box-shadow: 0 0 0 1px #86efac inset;
}
.pci-answer-input .el-textarea__inner:focus {
  border-color: #10b981;
  box-shadow: 0 0 0 1px #10b981 inset;
}
.pci-answer-input .el-textarea__inner::placeholder { color: #6ee7b7; }

/* AI 草稿答案（灰色/蓝色调，与人工绿色回答区分） */
.pci-ai-draft {
  background: #f8fafc !important;
  border-left-color: #94a3b8 !important;
  border-left-style: solid;
  border-left-width: 3px;
}
.pci-ai-draft .pci-sub-a-label { color: #64748b; }
.pci-ai-answer .el-textarea__inner {
  background: #f1f5f9 !important; border-color: #cbd5e1 !important; color: #475569 !important;
  box-shadow: 0 0 0 1px #cbd5e1 inset !important;
}
.pci-ai-answer .el-textarea__inner:focus {
  border-color: #94a3b8 !important;
  box-shadow: 0 0 0 1px #94a3b8 inset !important;
}
.pci-ai-answer .el-textarea__inner::placeholder { color: #94a3b8 !important; }
.pci-sub-a-text {
  flex: 1;
  font-size: 13px; line-height: 1.7; color: #065f46;
  padding: 6px 10px; background: #ecfdf5; border-radius: 6px;
  border-left: 3px solid #10b981;
  white-space: pre-wrap; word-break: break-word;
}
.pci-sub-summary {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; background: #f8fafc; border-radius: 6px;
  font-size: 12.5px; color: #6b7280; margin-top: 8px;
}
.pci-sub-qbadge {
  display: inline-block; padding: 0 7px; background: #3b82f6; color: #fff;
  border-radius: 4px; font-size: 11px; font-weight: 700; line-height: 18px;
}
.pci-sub-foot {
  display: flex; align-items: center; gap: 12px; margin-top: 8px;
  padding-top: 8px; border-top: 1px dashed #e5e7eb;
}
/* 画布模块校验 */
.canvas-review-body {
  display: flex; gap: 16px; margin-top: 14px; align-items: flex-start;
  min-height: 520px;
}
.canvas-list {
  flex: 0 0 280px; max-height: calc(100vh - 240px); min-height: 480px; overflow-y: auto; border: 1px solid #e5e7eb;
  border-radius: 8px; padding: 10px; background: #fafafa;
}
.canvas-list-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid #eee;
}
.canvas-count { font-size: 12px; color: #909399; }
.canvas-card {
  border: 1px solid #e4e7ed; border-radius: 8px; padding: 8px 10px; margin-bottom: 8px;
  background: #fff; cursor: pointer; transition: all .15s;
}
.canvas-card:hover { border-color: #c0c4cc; }
.canvas-card.active { border-color: #409eff; box-shadow: 0 0 0 2px rgba(64,158,255,.15); }
.canvas-card.unchecked { opacity: .6; background: #f4f4f5; }
.canvas-card.auto-filtered { border-style: dashed; background: #f7f8fa; opacity: .85; }
.auto-filter-tag { background: #f0f2f5; color: #909399; border-color: #e4e7ed; }
.canvas-auto-filter-reason {
  margin: 4px 0 0 28px; font-size: 11px; color: #a8abb2; line-height: 1.4;
}
.canvas-card-head { display: flex; align-items: center; gap: 6px; }
.canvas-no {
  font-size: 12px; font-weight: 700; color: #409eff; min-width: 22px;
  font-family: monospace;
}
.canvas-name-input { flex: 1; }
.canvas-card-meta { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0 4px 28px; }
.canvas-card-accuracy { display: flex; gap: 6px; margin-left: 28px; align-items: center; }
.canvas-card-progress { margin: 6px 0 4px 28px; }
.canvas-card-progress :deep(.el-step__title) { font-size: 11px; }
.canvas-card-progress :deep(.el-step__icon) { width: 18px; height: 18px; }
.canvas-card-progress :deep(.el-step__icon-inner) { font-size: 11px; }
.canvas-preview {
  flex: 1; border: 1px solid #e5e7eb; border-radius: 8px; padding: 12px;
  background: #fff; max-height: calc(100vh - 240px); min-height: 480px; overflow-y: auto;
}
.canvas-preview.empty {
  display: flex; align-items: center; justify-content: center;
  color: #909399; font-size: 13px; min-height: 200px;
}
.canvas-preview-head {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px;
}
.canvas-preview-no { font-size: 12px; color: #909399; }
.canvas-preview-img {
  max-width: 100%; border: 1px solid #eee; border-radius: 6px; margin-bottom: 12px;
  background: #f5f5f5; cursor: zoom-in;
}
.canvas-preview-img :deep(img) {
  max-width: 100%; display: block;
}
.canvas-preview-noshot {
  color: #909399; font-size: 13px; padding: 20px; text-align: center;
  border: 1px dashed #dcdfe6; border-radius: 6px; margin-bottom: 12px;
}
.canvas-preview-segs {
  display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px;
}
.canvas-preview-seg {
  position: relative; width: 88px; height: 66px; border: 2px solid #e5e7eb;
  border-radius: 6px; overflow: hidden; cursor: pointer; flex: none;
  transition: border-color .15s;
}
.canvas-preview-seg:hover { border-color: #c0c4cc; }
.canvas-preview-seg.active { border-color: #409eff; }
.canvas-preview-seg-img { width: 100%; height: 100%; display: block; }
.canvas-preview-seg-no {
  position: absolute; right: 2px; bottom: 2px; background: rgba(0,0,0,.55);
  color: #fff; font-size: 10px; line-height: 1; padding: 2px 4px; border-radius: 3px;
}
.canvas-preview-subtitle {
  font-weight: 600; font-size: 13px; color: #303133; margin: 10px 0 4px;
}
.canvas-preview-text .md-box { font-size: 12px; max-height: 300px; overflow-y: auto; }
.canvas-preview-text .md-box.warn { color: #e6a23c; }
.canvas-review-actions {
  display: flex; gap: 12px; margin-top: 14px;
}
</style>
