<template>
  <div class="apk-regression">
    <div class="page-header">
      <div>
        <h2>APK 智能回归</h2>
        <p class="sub">
          上传 APK → REA 自动分析 → 人工确认测试路径 → Midscene 真机执行 → 生成 HTML 报告
        </p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="refreshAll">刷新</el-button>
    </div>

    <!-- ===== 步骤条：让 MVP 的 5 段链路一眼可见 ===== -->
    <el-steps :active="activeStep" finish-status="success" align-center class="steps">
      <el-step title="上传 APK" :description="current?.artifact?.version_name || ''" />
      <el-step title="REA 分析" :description="analysisHint" />
      <el-step title="人工确认" :description="confirmHint" />
      <el-step title="真机执行" :description="execHint" />
      <el-step title="HTML 报告" :description="current?.report_path ? '已生成' : ''" />
    </el-steps>

    <el-card shadow="never" class="card">
      <!-- ============ 顶部：选 APK / 设备 ============ -->
      <div class="toolbar">
        <el-select
          v-model="selectedArtifactId"
          placeholder="选择已上传的 APK"
          filterable
          clearable
          style="width: 320px"
          @change="handleArtifactChange"
        >
          <el-option
            v-for="a in artifacts"
            :key="a.id"
            :label="`${a.package_name || a.display_name}  ${a.version_name || ''}`"
            :value="a.id"
          >
            <span>{{ a.package_name || a.display_name }}</span>
            <span class="opt-sub">{{ a.version_name }} · {{ formatSize(a.size) }}</span>
          </el-option>
        </el-select>

        <el-upload
          :show-file-list="false"
          :auto-upload="false"
          accept=".apk,.aab"
          :on-change="handleFilePicked"
        >
          <el-button :icon="Upload" :loading="uploading">上传 APK</el-button>
        </el-upload>

        <el-select
          v-model="selectedDeviceId"
          placeholder="选择 Android 设备"
          filterable
          clearable
          style="width: 260px"
        >
          <el-option
            v-for="d in devices"
            :key="d.id"
            :label="`${d.name} (${d.udid})`"
            :value="d.id"
          >
            <span>{{ d.name }}</span>
            <span class="opt-sub">{{ d.status_display || d.status }}</span>
          </el-option>
        </el-select>

        <el-button
          type="primary"
          :icon="Plus"
          :disabled="!selectedArtifactId"
          :loading="creating"
          @click="handleCreateRun"
        >
          新建回归任务
        </el-button>

        <el-select
          v-model="selectedRunId"
          placeholder="历史任务"
          filterable
          clearable
          style="width: 240px; margin-left: auto"
          @change="handleRunChange"
        >
          <el-option
            v-for="r in runs"
            :key="r.id"
            :label="`#${r.id} ${r.artifact?.package_name || ''} · ${r.status_display}`"
            :value="r.id"
          />
        </el-select>
      </div>

      <!-- 上传进度 -->
      <el-progress
        v-if="uploading"
        :percentage="uploadPercent"
        :stroke-width="6"
        style="margin-top: 12px"
      />

      <!-- APK 解析结果 -->
      <el-alert
        v-if="current?.artifact?.parse_error"
        type="error"
        :closable="false"
        show-icon
        style="margin-top: 12px"
        :title="`APK 解析失败：${current.artifact.parse_error}`"
      />

      <div v-if="currentArtifact && !currentArtifact.parse_error" class="apk-info">
        <div class="info-item"><span class="k">包名</span><span class="v">{{ currentArtifact.package_name }}</span></div>
        <div class="info-item"><span class="k">版本</span><span class="v">{{ currentArtifact.version_name }} / {{ currentArtifact.version_code }}</span></div>
        <div class="info-item"><span class="k">启动 Activity</span><span class="v">{{ currentArtifact.launch_activity || '-' }}</span></div>
        <div class="info-item"><span class="k">SDK</span><span class="v">min {{ currentArtifact.min_sdk }} / target {{ currentArtifact.target_sdk }}</span></div>
        <div class="info-item"><span class="k">权限</span><span class="v">{{ currentArtifact.permissions?.length || 0 }} 项</span></div>
        <div class="info-item"><span class="k">危险权限</span>
          <span class="v">
            <el-tag
              v-for="d in currentArtifact.dangerous_permissions || []"
              :key="d.name"
              size="small"
              type="warning"
              class="tag"
            >{{ d.label }}</el-tag>
            <span v-if="!(currentArtifact.dangerous_permissions || []).length">无</span>
          </span>
        </div>
      </div>
    </el-card>

    <!-- ============ 业务资料：分析前先备料 ============ -->
    <el-card v-if="current" shadow="never" class="card">
      <template #header>
        <div class="card-head">
          <span>阶段 1 前置 · 业务资料</span>
          <div class="head-actions">
            <el-tag v-if="ctx.has_context" type="success" size="small">已备料</el-tag>
            <el-tag v-else type="info" size="small">未提供（路径会偏通用）</el-tag>
            <el-button size="small" :icon="View" :disabled="!ctx.has_context" @click="openPreview">
              查看模型看到的资料
            </el-button>
          </div>
        </div>
      </template>

      <el-alert type="info" :closable="false" show-icon class="ctx-tip">
        <template #title>
          只靠 APK 清单只能生成「启动/探索」这类通用路径。上传<b>操作说明</b>或关联
          <b>项目知识库</b>（含 DB 条目与 knowledge-bases 下的 md 文档）后，才能生成
          针对真实业务流程的路径，且每条路径都会标注依据。
        </template>
      </el-alert>

      <el-row :gutter="16">
        <el-col :span="12">
          <div class="ctx-block">
            <div class="ctx-label">操作说明文档</div>
            <el-upload
              :show-file-list="false"
              :auto-upload="false"
              accept=".docx,.pdf,.md,.txt,.markdown"
              :on-change="handleContextFile"
            >
              <el-button size="small" :icon="Upload" :loading="ctxUploading">上传文档</el-button>
            </el-upload>
            <span class="hint inline">支持 docx / pdf / md / txt</span>
            <div v-if="ctx.docs?.length" class="doc-list">
              <div v-for="(d, i) in ctx.docs" :key="i" class="doc-row">
                <span class="doc-name" :title="d.preview">{{ d.name }}</span>
                <span class="doc-meta">{{ d.chars }} 字{{ d.truncated ? '（已截断）' : '' }}</span>
                <el-button link size="small" type="danger" @click="removeContextDoc(i)">删除</el-button>
              </div>
            </div>
            <div v-else class="empty-row">暂无文档</div>
          </div>
        </el-col>

        <el-col :span="12">
          <div class="ctx-block">
            <div class="ctx-label">项目知识库</div>
            <el-select
              v-model="ctx.project_id"
              placeholder="选择项目以引入知识库"
              filterable
              clearable
              style="width: 100%"
              @change="handleProjectChange"
            >
              <el-option
                v-for="p in projects"
                :key="p.id"
                :label="`${p.name}（${p.knowledge_count} 条知识 / ${p.kb_file_count || 0} 篇文档）`"
                :value="p.id"
              />
            </el-select>
            <div v-if="ctx.project_id" class="kb-info">
              已关联「{{ ctx.project_name || '项目' }}」：
              <b>{{ ctx.knowledge_count }}</b> 条启用知识（{{ ctx.knowledge_chars }} 字）
              <template v-if="ctx.kb_file_count">
                · <b>{{ ctx.kb_file_count }}</b> 篇知识文档
              </template>
              <template v-else>
                · <span class="kb-none">无 md 文档目录</span>
              </template>
              <el-button link size="small" @click="viewKnowledge">查看条目</el-button>
            </div>
            <div v-else class="empty-row">未关联项目（不读知识库）</div>
          </div>
        </el-col>
      </el-row>

      <div class="ctx-block" style="margin-top: 12px">
        <div class="ctx-label">人工补充说明</div>
        <el-input
          v-model="ctx.context_notes"
          type="textarea"
          :rows="3"
          placeholder="补充关键业务信息，例如：首页有广告弹窗需先关闭；登录后才能下单；主流程是 搜索→加购→结算"
        />
        <el-button size="small" style="margin-top: 8px" :loading="ctxSaving" @click="saveContextNotes">
          保存补充说明
        </el-button>
      </div>

      <div v-if="contextSources.length" class="ctx-sources">
        <span class="ctx-label">上次分析依据：</span>
        <el-tag
          v-for="(s, i) in contextSources"
          :key="i"
          size="small"
          :type="s.kind === 'doc' ? 'primary' : (s.kind === 'kb_file' ? 'success' : 'info')"
          class="tag"
        >{{ sourceLabel(s) }}：{{ s.name }}</el-tag>
      </div>
    </el-card>

    <!-- ============ 阶段 2：REA 分析 ============ -->
    <el-card v-if="current" shadow="never" class="card">
      <template #header>
        <div class="card-head">
          <span>阶段 1 · REA 分析</span>
          <div class="head-actions">
            <el-tag :type="statusTagType(current.status)" size="small">{{ current.status_display }}</el-tag>
            <el-button
              size="small"
              :icon="MagicStick"
              :loading="current.busy && current.status === 'analyzing'"
              :disabled="current.busy"
              @click="handleAnalyze"
            >{{ current.paths?.length ? '重新分析' : '开始分析' }}</el-button>
          </div>
        </div>
      </template>

      <el-alert
        v-if="current.rea_analysis?.llm_error"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        :title="current.rea_analysis.llm_error"
        description="已自动降级为规则分析（只基于 APK 清单信息生成通用路径），请人工补充业务路径后再确认。"
      />

      <p v-if="current.rea_analysis?.analysis_notes" class="notes">
        {{ current.rea_analysis.analysis_notes }}
      </p>

      <div v-if="dangerousActions.length" class="danger-box">
        <span class="danger-title">危险操作（执行时自动拦截）</span>
        <el-tag v-for="d in dangerousActions" :key="d.name" type="danger" size="small" class="tag">
          {{ d.name }}
        </el-tag>
      </div>

      <el-table
        v-if="current.paths?.length"
        :data="current.paths"
        size="small"
        style="width: 100%; margin-top: 12px"
      >
        <el-table-column type="index" label="#" width="50" />
        <el-table-column label="启用" width="76" align="center">
          <template #default="{ row, $index }">
            <el-switch
              :model-value="row.enabled !== false"
              size="small"
              :loading="row._toggling"
              @change="(v) => togglePathEnabled($index, v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="路径名称" min-width="180">
          <template #default="{ row }">
            <span :class="{ 'path-off': row.enabled === false }">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="priorityType(row.priority)" size="small">{{ row.priority }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="type" label="类型" width="110" align="center" />
        <el-table-column label="步骤" min-width="320">
          <template #default="{ row }">
            <ol class="steps-list">
              <li v-for="(s, i) in row.steps" :key="i">{{ s }}</li>
            </ol>
          </template>
        </el-table-column>
        <el-table-column label="跳过" min-width="140">
          <template #default="{ row }">
            <el-tag v-for="s in row.skip" :key="s" size="small" type="info" class="tag">{{ s }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="校验" width="130" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.invalid_reasons?.length" type="warning" size="small">需修正</el-tag>
            <span v-else class="ok-text">OK</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" align="center">
          <template #default="{ row, $index }">
            <el-button link size="small" type="primary" @click="openEditPath($index)">编辑</el-button>
            <el-button link size="small" @click="openYaml($index)">YAML</el-button>
            <el-button link size="small" type="danger" @click="removePath($index)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="current.paths?.length && disabledPathCount" class="disabled-tip">
        已关闭 {{ disabledPathCount }} 条路径，真机执行时会自动跳过（通过率只统计真正执行过的路径）
      </div>
      <el-empty v-else-if="!current.paths?.length" description="尚未生成测试路径" :image-size="70" />
    </el-card>

    <!-- ============ 阶段 3：人工确认闸门 ============ -->
    <el-card v-if="current" shadow="never" class="card">
      <template #header>
        <div class="card-head">
          <span>阶段 2 · 人工确认（必须）</span>
          <el-tag v-if="current.approved" type="success" size="small">已确认</el-tag>
          <el-tag v-else type="info" size="small">未确认</el-tag>
        </div>
      </template>

      <el-alert
        v-if="current.approved"
        type="success"
        :closable="false"
        show-icon
        :title="`已由 ${current.confirmation?.approved_by_name || '-'} 于 ${formatTime(current.confirmation?.approved_at)} 确认`"
        :description="`路径指纹 ${(current.confirmation?.fingerprint || '').slice(0, 16)}…（内容变更后会自动失效）`"
      />
      <el-alert
        v-else-if="current.confirmation?.invalidated_reason"
        type="warning"
        :closable="false"
        show-icon
        :title="current.confirmation.invalidated_reason"
        description="提示：只修改路径「名称 / 优先级」不会让确认失效；修改「执行步骤或跳过项」才需要重新确认。"
      />

      <ul v-if="current.approval_blockers?.length" class="blockers">
        <li v-for="(b, i) in current.approval_blockers" :key="i">{{ b }}</li>
      </ul>

      <el-input
        v-model="approveNotes"
        type="textarea"
        :rows="2"
        placeholder="确认备注（可选）：例如「已与产品确认跳过的支付/删除入口」"
        style="margin-top: 12px"
      />
      <div style="margin-top: 12px">
        <el-checkbox v-if="hasInvalidPaths" v-model="forceApprove">忽略路径校验告警，强制确认</el-checkbox>
        <el-button
          type="primary"
          :icon="CircleCheck"
          :disabled="!current.paths?.length || current.busy"
          :loading="approving"
          style="margin-left: 12px"
          @click="handleApprove"
        >确认路径清单</el-button>
      </div>
    </el-card>

    <!-- ============ 阶段 4：执行 ============ -->
    <el-card v-if="current" shadow="never" class="card">
      <template #header>
        <div class="card-head">
          <span>阶段 3 · Midscene 真机执行</span>
          <div class="head-actions">
            <el-button
              size="small"
              type="primary"
              :icon="VideoPlay"
              :loading="current.status === 'running'"
              :disabled="!current.approved || current.busy"
              @click="handleExecute"
            >{{ executeLabel }}</el-button>
            <el-button
              v-if="current.status === 'running'"
              size="small"
              type="danger"
              plain
              :loading="aborting"
              @click="handleAbort"
            >中止执行</el-button>
            <el-button size="small" :icon="Download" :disabled="!current.report_path" @click="openReport(false)">
              查看报告
            </el-button>
            <el-button size="small" :disabled="!current.report_path" @click="openReport(true)">下载 HTML</el-button>
          </div>
        </div>
      </template>

      <div v-if="current.summary?.total" class="summary">
        <div class="sum-item"><span class="k">总路径</span><span class="v">{{ current.summary.total }}</span></div>
        <div class="sum-item pass"><span class="k">通过</span><span class="v">{{ current.summary.passed }}</span></div>
        <div class="sum-item fail"><span class="k">失败</span><span class="v">{{ current.summary.failed }}</span></div>
        <div class="sum-item skip"><span class="k">跳过</span><span class="v">{{ current.summary.skipped }}</span></div>
        <div class="sum-item"><span class="k">通过率</span><span class="v">{{ current.summary.pass_rate }}%</span></div>
        <div class="sum-item"><span class="k">耗时</span><span class="v">{{ (current.duration || 0).toFixed(1) }}s</span></div>
      </div>

      <el-table
        v-if="current.results?.length"
        :data="current.results"
        size="small"
        style="width: 100%; margin-top: 12px"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="result-detail">
              <div v-for="s in row.steps" :key="s.index" class="step-row">
                <span :class="['dot', s.status === 'failed' ? 'no' : 'ok']"></span>
                <span class="step-name">步骤{{ s.index + 1 }} · {{ s.name }}（{{ s.action }}）</span>
                <span v-if="s.prompt" class="step-prompt">{{ s.prompt }}</span>
                <span v-if="s.error" class="step-err">{{ s.error }}</span>
              </div>
              <div v-if="!row.steps?.length" class="empty-tip">无步骤记录</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="#" width="50">
          <template #default="{ row }">{{ row.index + 1 }}</template>
        </el-table-column>
        <el-table-column prop="name" label="路径" min-width="180" />
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="resultTagType(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="duration" label="耗时(s)" width="90" align="center" />
        <el-table-column label="错误" min-width="220">
          <template #default="{ row }">
            <span class="err-text">{{ row.error || '-' }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="log-box">
        <div class="log-head">
          <span>运行日志</span>
          <span class="log-meta">{{ current.log ? current.log.split('\n').length : 0 }} 行</span>
        </div>
        <pre class="log">{{ current.log || '暂无日志' }}</pre>
      </div>
    </el-card>

    <el-dialog v-model="yamlVisible" title="Midscene YAML" width="720px">
      <pre class="yaml">{{ yamlText }}</pre>
      <template #footer>
        <el-button @click="yamlVisible = false">关闭</el-button>
        <el-button type="primary" @click="downloadYaml">下载</el-button>
      </template>
    </el-dialog>

    <!-- 资料预览：让用户确认"模型到底看到了什么" -->
    <el-dialog v-model="previewVisible" title="模型实际看到的业务资料" width="760px">
      <div class="preview-meta">
        共 {{ previewData.chars }} 字
        <span v-if="previewData.truncated_preview">（下方只预览前 4000 字）</span>
      </div>
      <div v-if="previewData.sources?.length" class="preview-sources">
        <el-tag
          v-for="(s, i) in previewData.sources"
          :key="i"
          size="small"
          :type="s.kind === 'doc' ? 'primary' : (s.kind === 'kb_file' ? 'success' : 'info')"
          class="tag"
        >{{ sourceLabel(s) }}{{ s.name ? '：' + s.name : '' }}</el-tag>
      </div>
      <pre class="yaml">{{ previewData.text || '（无资料）' }}</pre>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 编辑路径：名称随处可改，步骤改动会触发重新确认 -->
    <el-dialog
      v-model="editVisible"
      :title="`编辑路径 #${editIndex + 1}`"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form label-width="96px" label-position="right">
        <el-form-item label="路径名称">
          <el-input v-model="editForm.name" placeholder="例如：首页入口探索" />
          <div class="hint">仅修改名称不会让已确认状态失效</div>
        </el-form-item>

        <el-form-item label="启用状态">
          <el-switch v-model="editForm.enabled" active-text="启用" inactive-text="关闭（真机执行时跳过）" />
          <div class="hint">关闭只是跳过，名称与步骤都会保留；不影响「已确认」状态</div>
        </el-form-item>

        <el-form-item label="优先级">
          <el-select v-model="editForm.priority" style="width: 160px">
            <el-option label="high" value="high" />
            <el-option label="medium" value="medium" />
            <el-option label="low" value="low" />
          </el-select>
          <span class="hint inline">类型</span>
          <el-select v-model="editForm.type" style="width: 160px; margin-left: 8px">
            <el-option label="explore" value="explore" />
            <el-option label="regression" value="regression" />
          </el-select>
        </el-form-item>

        <el-form-item label="执行步骤">
          <div class="step-editor">
            <div v-for="(s, i) in editForm.steps" :key="i" class="step-line">
              <span class="step-no">{{ i + 1 }}</span>
              <el-input v-model="editForm.steps[i]" placeholder="自然语言描述，如：点击我的进入个人中心" />
              <el-button
                link type="primary" :icon="Top" :disabled="i === 0"
                @click="moveStep(i, -1)"
              />
              <el-button
                link type="primary" :icon="Bottom" :disabled="i === editForm.steps.length - 1"
                @click="moveStep(i, 1)"
              />
              <el-button
                link type="danger" :icon="Delete" :disabled="editForm.steps.length <= 1"
                @click="editForm.steps.splice(i, 1)"
              />
            </div>
            <el-button size="small" :icon="Plus" @click="editForm.steps.push('')">
              添加步骤
            </el-button>
          </div>
          <div class="hint">
            步骤必须是自然语言语义描述（Midscene 靠语义定位），禁止写坐标。改动步骤会要求重新确认。
          </div>
        </el-form-item>

        <el-form-item label="跳过项">
          <el-select
            v-model="editForm.skip"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车添加，如：支付 / 删除 / 退出登录"
            style="width: 100%"
          />
          <div class="hint">命中的步骤在执行时会被直接拦截跳过（危险操作保护）</div>
        </el-form-item>

        <el-alert
          v-if="editWillInvalidate"
          type="warning"
          :closable="false"
          show-icon
          title="步骤/跳过项有改动：保存后需重新点「确认路径清单」才能执行"
          class="mini-alert"
        />
        <el-alert
          v-else-if="current?.approved"
          type="success"
          :closable="false"
          show-icon
          title="仅修改名称/优先级：已确认状态保持有效，保存后可直接执行"
          class="mini-alert"
        />
      </el-form>

      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingPath" @click="saveEditPath">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Refresh, Upload, Plus, MagicStick, VideoPlay, CircleCheck, Download,
  Top, Bottom, Delete, View
} from '@element-plus/icons-vue'
import {
  uploadApk, getApkArtifacts, getApkRuns, createApkRun, getApkRun,
  updateApkRun, analyzeApkRun, approveApkRun, executeApkRun, abortApkRun,
  getApkYamlUrl, getApkReportUrl
} from '@/api/apk-regression'
import {
  getProjectOptions, getProjectKnowledge, getRunContext, updateRunContext,
  uploadContextDoc, deleteContextDoc, previewRunContext
} from '@/api/apk-context'
import { getAppDevices } from '@/api/app-pipeline'

const artifacts = ref([])
const devices = ref([])
const runs = ref([])
const current = ref(null)
const loading = ref(false)
const uploading = ref(false)
const uploadPercent = ref(0)
const creating = ref(false)
const approving = ref(false)
const selectedArtifactId = ref(null)
const selectedDeviceId = ref(null)
const selectedRunId = ref(null)
const approveNotes = ref('')
const forceApprove = ref(false)
const yamlVisible = ref(false)
const editVisible = ref(false)
const editIndex = ref(-1)
const savingPath = ref(false)
const aborting = ref(false)

// ---- 业务资料 ----
const projects = ref([])
const ctx = reactive({
  project_id: null, project_name: '', context_notes: '',
  docs: [], doc_count: 0, doc_chars: 0,
  knowledge_count: 0, knowledge_chars: 0,
  kb_file_count: 0, kb_file_path: '', has_context: false
})
const ctxUploading = ref(false)
const ctxSaving = ref(false)
const previewVisible = ref(false)
const previewData = reactive({ chars: 0, text: '', truncated_preview: false, sources: [] })
//: 上次分析实际用到的资料（来自 rea_analysis.context_sources）
const contextSources = computed(
  () => (current.value?.rea_analysis?.context_sources) || [])
const editForm = reactive({
  name: '', priority: 'medium', type: 'explore',
  steps: [''], skip: [], enabled: true
})
//: 打开编辑时的原始快照，用来判断"是否只改了名称"
let editSnapshot = { name: '', content: '' }
const yamlText = ref('')
const yamlPathIndex = ref(0)

let pollTimer = null

const currentArtifact = computed(() => {
  if (current.value?.artifact) return current.value.artifact
  return artifacts.value.find(a => a.id === selectedArtifactId.value) || null
})
const dangerousActions = computed(() => current.value?.rea_analysis?.dangerous_actions || [])
const hasInvalidPaths = computed(() =>
  (current.value?.paths || []).some(p => p.invalid_reasons?.length))

const activeStep = computed(() => {
  const s = current.value?.status
  if (!s || s === 'uploaded') return 0
  if (s === 'analyzing' || s === 'analyzing_failed') return 1
  if (s === 'analyzed') return 2
  if (s === 'confirmed') return 3
  if (s === 'running' || s === 'failed') return 3
  if (s === 'done') return 5
  return 0
})
const analysisHint = computed(() => {
  const s = current.value?.status
  if (s === 'analyzing') return '分析中…'
  if (current.value?.paths?.length) return `${current.value.paths.length} 条路径`
  return ''
})
const confirmHint = computed(() => current.value?.approved ? '已确认' : '')
const execHint = computed(() => {
  const sm = current.value?.summary
  return sm?.total ? `${sm.passed}/${sm.total} 通过` : ''
})

//: 被关闭（不参与执行）的路径条数
const disabledPathCount = computed(
  () => (current.value?.paths || []).filter(p => p.enabled === false).length)

//: 本次真正会执行的路径数（排除已关闭的）。
//: 执行确认弹窗必须用它，不能用 paths.length —— 否则用户只开 1 条、
//: 提示却说"将执行 8 条"，与实际行为不符（2026-10-28 用户反馈）。
const enabledPathCount = computed(
  () => (current.value?.paths || []).filter(p => p.enabled !== false).length)

//: 执行按钮文案：跑过一次之后再点就是"重新执行"，语义更清楚
const executeLabel = computed(() => {
  const s = current.value?.status
  if (s === 'running') return '执行中…'
  if (s === 'done' || s === 'failed') return '重新执行'
  return '开始执行'
})

const statusTagType = (s) => ({
  uploaded: 'info', analyzing: 'warning', analyzed: '', analyzing_failed: 'danger',
  confirmed: 'success', running: 'warning', done: 'success', failed: 'danger', cancelled: 'info'
}[s] || 'info')
const resultTagType = (s) => ({ passed: 'success', failed: 'danger', skipped: 'warning' }[s] || 'info')
const priorityType = (p) => ({ high: 'danger', medium: 'warning', low: 'info' }[p] || 'info')
const formatTime = (t) => (t ? new Date(t).toLocaleString() : '-')
const formatSize = (n) => {
  if (!n) return '-'
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024).toFixed(0)} KB`
}

async function loadArtifacts() {
  try {
    const res = await getApkArtifacts({})
    artifacts.value = unwrap(res)
  } catch (e) {
    ElMessage.error('加载 APK 列表失败')
  }
}
async function loadDevices() {
  try {
    const res = await getAppDevices({ page_size: 200 })
    devices.value = unwrap(res).filter(d => (d.platform || '').toLowerCase() === 'android')
  } catch (e) { /* 忽略 */ }
}
async function loadRuns() {
  try {
    const res = await getApkRuns({})
    runs.value = unwrap(res)
  } catch (e) {
    ElMessage.error('加载运行记录失败')
  }
}

function unwrap(res) {
  const d = res?.data
  const payload = d?.results !== undefined ? d.results : (d?.data?.results !== undefined ? d.data.results : d)
  return Array.isArray(payload) ? payload : (payload?.results || [])
}

async function refreshAll() {
  loading.value = true
  try {
    await Promise.all([loadArtifacts(), loadDevices(), loadRuns()])
  } finally {
    loading.value = false
  }
}

function handleArtifactChange() {
  // 换 APK 时清掉当前运行详情，避免把上一个包的结果显示成这个包的
  if (current.value && current.value.artifact?.id !== selectedArtifactId.value) {
    current.value = null
    selectedRunId.value = null
  }
}

async function handleFilePicked(uploadFile) {
  const file = uploadFile.raw
  if (!file) return
  const fd = new FormData()
  fd.append('file', file)
  fd.append('name', file.name.replace(/\.(apk|aab)$/i, ''))
  uploading.value = true
  uploadPercent.value = 0
  try {
    const res = await uploadApk(fd, (e) => {
      if (e.total) uploadPercent.value = Math.min(99, Math.round((e.loaded * 100) / e.total))
    })
    uploadPercent.value = 100
    const data = res?.data || {}
    await loadArtifacts()
    selectedArtifactId.value = data.id
    if (data.warning) {
      ElMessage.warning(data.warning)
    } else {
      ElMessage.success(`上传成功：${data.package_name} ${data.version_name || ''}`)
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '上传失败')
  } finally {
    uploading.value = false
  }
}

async function handleCreateRun() {
  if (!selectedArtifactId.value) {
    ElMessage.warning('请先选择或上传 APK')
    return
  }
  creating.value = true
  try {
    // 不再 auto_analyze：分析前要先给用户机会提供业务资料
    // （操作说明 / 知识库 / 补充说明），否则只能拿到通用路径。
    const payload = { artifact_id: selectedArtifactId.value }
    if (selectedDeviceId.value) payload.device_id = selectedDeviceId.value
    const res = await createApkRun(payload)
    current.value = res.data
    selectedRunId.value = current.value.id
    await loadContext(current.value.id)
    await loadRuns()
    ElMessage.success('已创建回归任务。可先补充业务资料，再点「开始分析」')
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '创建任务失败')
  } finally {
    creating.value = false
  }
}

async function handleRunChange(id) {
  if (!id) {
    current.value = null
    return
  }
  try {
    const res = await getApkRun(id)
    current.value = res.data
    selectedArtifactId.value = current.value.artifact?.id || selectedArtifactId.value
    await loadContext(current.value.id)
    if (current.value.busy) startPoll()
  } catch (e) {
    ElMessage.error('加载运行详情失败')
  }
}

async function handleAnalyze() {
  try {
    await analyzeApkRun(current.value.id)
    ElMessage.success('已开始分析')
    startPoll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '启动分析失败')
  }
}

async function handleApprove() {
  approving.value = true
  try {
    const res = await approveApkRun(current.value.id, {
      notes: approveNotes.value,
      force: forceApprove.value
    })
    current.value = res.data
    ElMessage.success('已确认路径清单')
    await loadRuns()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '确认失败')
  } finally {
    approving.value = false
  }
}

async function handleExecute() {
  if (!selectedDeviceId.value) {
    ElMessage.warning('请先选择执行设备')
    return
  }
  const isRerun = ['done', 'failed'].includes(current.value.status)
  const total = current.value.paths.length
  const enabled = enabledPathCount.value
  const skipped = total - enabled
  if (enabled === 0) {
    ElMessage.warning('所有路径都已关闭，没有可执行的路径。请至少启用一条，或删除无用的路径。')
    return
  }
  // 文案要说清"真正会跑几条"，并把跳过的条数摆出来，
  // 否则用户以为自己的开关没生效。
  const scope = skipped
    ? `${enabled} 条路径（另有 ${skipped} 条已关闭将跳过）`
    : `${enabled} 条路径`
  const tip = isRerun
    ? `将重新执行 ${scope}，上一次的结果与报告会被覆盖。是否继续？`
    : `将在设备上真实执行 ${scope}，期间设备会被占用。是否继续？`
  try {
    await ElMessageBox.confirm(tip, isRerun ? '重新执行' : '开始真机执行', { type: 'warning' })
  } catch (e) {
    return
  }
  try {
    await executeApkRun(current.value.id, { device_id: selectedDeviceId.value })
    ElMessage.success(isRerun ? '已重新开始执行' : '已开始执行')
    startPoll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '执行失败')
  }
}

async function handleAbort() {
  try {
    await ElMessageBox.confirm(
      '将强制中止本次执行。已完成的路径结果会保留，但本次不再继续。是否继续？',
      '中止执行', { type: 'warning' })
  } catch (e) {
    return
  }
  aborting.value = true
  try {
    const res = await abortApkRun(current.value.id)
    stopPoll()
    current.value = res.data?.run || current.value
    ElMessage.success(res.data?.message || '已中止')
    await loadRuns()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '中止失败')
  } finally {
    aborting.value = false
  }
}

function openReport(download) {
  window.open(getApkReportUrl(current.value.id, download), '_blank')
}

// ---------------- 业务资料（操作说明 / 知识库 / 人工补充） ----------------
function applyContext(d) {
  if (!d) return
  Object.assign(ctx, {
    project_id: d.project_id || null,
    project_name: d.project_name || '',
    context_notes: d.context_notes || '',
    docs: d.docs || [],
    doc_count: d.doc_count || 0,
    doc_chars: d.doc_chars || 0,
    knowledge_count: d.knowledge_count || 0,
    knowledge_chars: d.knowledge_chars || 0,
    kb_file_count: d.kb_file_count || 0,
    kb_file_path: d.kb_file_path || '',
    has_context: !!d.has_context
  })
}

async function loadContext(runId) {
  try {
    const res = await getRunContext(runId)
    applyContext(res.data)
  } catch (e) { /* 资料读取失败不阻断主流程 */ }
}

async function loadProjects() {
  try {
    const res = await getProjectOptions()
    projects.value = res.data?.results || []
  } catch (e) { /* 忽略 */ }
}

async function handleContextFile(uploadFile) {
  const file = uploadFile?.raw
  if (!file || !current.value?.id) return
  ctxUploading.value = true
  try {
    const res = await uploadContextDoc(current.value.id, file)
    applyContext({ ...ctx, ...res.data, has_context: true, project_id: ctx.project_id })
    // 后端返回的 docs 只含摘要，重新拉一次保证字段完整
    await loadContext(current.value.id)
    ElMessage.success(res.data?.message || '已上传')
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '上传失败')
  } finally {
    ctxUploading.value = false
  }
}

async function removeContextDoc(idx) {
  try {
    const res = await deleteContextDoc(current.value.id, idx)
    ElMessage.success(res.data?.message || '已删除')
    await loadContext(current.value.id)
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '删除失败')
  }
}

async function handleProjectChange(pid) {
  try {
    const res = await updateRunContext(current.value.id, { project_id: pid || null })
    applyContext(res.data)
    if (pid) {
      ElMessage.success(`已关联项目知识库（${ctx.knowledge_count} 条）`)
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '关联失败')
  }
}

async function saveContextNotes() {
  ctxSaving.value = true
  try {
    const res = await updateRunContext(current.value.id, { context_notes: ctx.context_notes })
    applyContext(res.data)
    ElMessage.success('补充说明已保存')
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '保存失败')
  } finally {
    ctxSaving.value = false
  }
}

function sourceLabel(s) {
  if (s.kind === 'doc') return '说明文档'
  if (s.kind === 'kb_file') return '知识文档'
  if (s.kind === 'kb_file_summary') return '文件知识库'
  if (s.kind === 'kb_summary') return '知识库'
  if (s.kind === 'notes') return '补充说明'
  return '知识库'
}

async function viewKnowledge() {
  if (!ctx.project_id) return
  try {
    const res = await getProjectKnowledge(ctx.project_id)
    const items = res.data?.results || []
    const files = res.data?.kb_files || []
    const parts = []
    parts.push(`【DB 条目】${items.length} 条`)
    if (items.length) {
      parts.push(items.map(x => `  【${x.category_display}】${x.title}`).join('\n'))
    } else {
      parts.push('  （无启用的知识条目）')
    }
    parts.push('')
    parts.push(`【文件知识库】${files.length} 篇${res.data?.kb_file_path ? `  ${res.data.kb_file_path}` : ''}`)
    parts.push(files.length
      ? files.map(f => `  ${f.path}`).join('\n')
      : '  （该项目未配置文件知识库目录）')
    await ElMessageBox.alert(
      parts.join('\n'),
      `知识库（${items.length} 条条目 / ${files.length} 篇文档）`,
      { customStyle: { whiteSpace: 'pre-line', maxHeight: '60vh', overflow: 'auto' } }
    )
  } catch (e) {
    ElMessage.error('读取知识库失败')
  }
}

async function openPreview() {
  try {
    const res = await previewRunContext(current.value.id)
    Object.assign(previewData, res.data || {})
    previewVisible.value = true
  } catch (e) {
    ElMessage.error('读取资料预览失败')
  }
}

// ---------------- 路径编辑（方案 C：改名免确认 / 改步骤需重新确认） ----------------
// 执行内容指纹只覆盖 steps / skip / type（与后端 FINGERPRINT_FIELDS 一致），
// 这里用同样的口径做前端预判，好让用户在保存前就知道会不会要求重新确认。
const normList = (arr) =>
  (arr || []).map(x => String(x == null ? '' : x).trim()).filter(Boolean)
const contentKey = (p) => JSON.stringify({
  steps: normList(p.steps),
  skip: normList(p.skip),
  type: p.type
})

const editWillInvalidate = computed(() => {
  if (!current.value?.approved) return false
  const draft = {
    steps: editForm.steps,
    skip: editForm.skip,
    type: editForm.type
  }
  return contentKey(draft) !== editSnapshot.content
})

function openEditPath(index) {
  const p = (current.value?.paths || [])[index]
  if (!p) return
  editIndex.value = index
  editForm.name = p.name || ''
  editForm.priority = p.priority || 'medium'
  editForm.type = p.type || 'explore'
  editForm.steps = (p.steps && p.steps.length) ? [...p.steps] : ['']
  editForm.skip = [...(p.skip || [])]
  editForm.enabled = p.enabled !== false
  editSnapshot = { name: p.name || '', content: contentKey(p) }
  editVisible.value = true
}

function moveStep(i, delta) {
  const j = i + delta
  if (j < 0 || j >= editForm.steps.length) return
  const arr = editForm.steps
  const t = arr[i]
  arr[i] = arr[j]
  arr[j] = t
}

async function saveEditPath() {
  const name = (editForm.name || '').trim()
  const steps = normList(editForm.steps)
  if (!name) {
    ElMessage.warning('路径名称不能为空')
    return
  }
  if (!steps.length) {
    ElMessage.warning('至少保留一条执行步骤')
    return
  }
  const paths = JSON.parse(JSON.stringify(current.value.paths || []))
  paths[editIndex.value] = {
    ...paths[editIndex.value],
    name,
    priority: editForm.priority,
    type: editForm.type,
    steps,
    skip: normList(editForm.skip),
    enabled: editForm.enabled
  }
  savingPath.value = true
  try {
    // 执行中也允许保存：后端只把改动落到下一次执行，不打断当前这一轮
    const res = await updateApkRun(current.value.id, { paths })
    current.value = res.data
    editVisible.value = false
    if (current.value.content_changed) {
      ElMessage.warning('已保存。执行步骤有改动，请重新点「确认路径清单」后再执行')
    } else {
      ElMessage.success('已保存')
    }
    await loadRuns()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '保存失败')
  } finally {
    savingPath.value = false
  }
}

// 路径启停开关：只改 enabled 字段，不动 steps/skip/type，
// 所以**不会**触发确认失效（后端指纹不含 enabled）。
async function togglePathEnabled(index, val) {
  const row = current.value?.paths?.[index]
  if (!row) return
  row._toggling = true
  const paths = JSON.parse(JSON.stringify(current.value.paths || []))
  paths[index].enabled = val
  try {
    const res = await updateApkRun(current.value.id, { paths })
    current.value = res.data
    ElMessage.success(val ? `已启用「${row.name}」` : `已关闭「${row.name}」，真机执行时将跳过`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '切换失败')
  } finally {
    row._toggling = false
  }
}

async function removePath(index) {
  const p = (current.value?.paths || [])[index]
  try {
    await ElMessageBox.confirm(
      `确定删除路径「${p?.name || '#' + (index + 1)}」？`, '删除确认', { type: 'warning' })
  } catch (e) {
    return
  }
  const paths = JSON.parse(JSON.stringify(current.value.paths || []))
  paths.splice(index, 1)
  try {
    const res = await updateApkRun(current.value.id, { paths })
    current.value = res.data
    ElMessage.success('已删除')
    await loadRuns()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '删除失败')
  }
}

async function openYaml(index) {
  yamlPathIndex.value = index
  try {
    const token = localStorage.getItem('access_token') || localStorage.getItem('token')
    const resp = await fetch(getApkYamlUrl(current.value.id, index), {
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    })
    yamlText.value = await resp.text()
    yamlVisible.value = true
  } catch (e) {
    ElMessage.error('获取 YAML 失败')
  }
}

function downloadYaml() {
  const blob = new Blob([yamlText.value], { type: 'text/yaml;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `apk_path_${yamlPathIndex.value}.yaml`
  a.click()
  URL.revokeObjectURL(a.href)
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(async () => {
    if (!current.value?.id) return
    try {
      const res = await getApkRun(current.value.id)
      current.value = res.data
      if (!current.value.busy) {
        stopPoll()
        await loadRuns()
      }
    } catch (e) {
      stopPoll()
    }
  }, 3000)
}
function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

onMounted(async () => {
  await Promise.all([refreshAll(), loadProjects()])
})
onBeforeUnmount(stopPoll)
</script>

<style scoped lang="scss">
.apk-regression {
  padding: 20px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;

  h2 {
    margin: 0;
    font-size: 20px;
  }

  .sub {
    margin: 4px 0 0;
    color: #8a9099;
    font-size: 13px;
  }
}

.steps {
  margin-bottom: 18px;
}

.card {
  margin-bottom: 16px;
}

.toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}

.opt-sub {
  float: right;
  color: #a0a6ad;
  font-size: 12px;
  margin-left: 12px;
}

.apk-info {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 8px 20px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed #eef0f3;

  .info-item {
    font-size: 13px;

    .k {
      color: #8a9099;
      margin-right: 8px;
    }

    .v {
      color: #1f2329;
      word-break: break-all;
    }
  }
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;

  .head-actions {
    display: flex;
    gap: 8px;
    align-items: center;
  }
}

.tag {
  margin: 0 4px 4px 0;
}

.notes {
  color: #5c6370;
  font-size: 13px;
  white-space: pre-wrap;
  margin: 0 0 12px;
}

.danger-box {
  background: #fdf3e2;
  border-radius: 8px;
  padding: 10px 12px;

  .danger-title {
    font-size: 13px;
    color: #b7791f;
    margin-right: 10px;
  }
}

.steps-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  color: #3d4450;
}

.ok-text {
  color: #12a150;
  font-size: 13px;
}

.blockers {
  color: #e5484d;
  font-size: 13px;
  margin: 8px 0 0;
  padding-left: 18px;
}

.summary {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;

  .sum-item {
    .k {
      color: #8a9099;
      font-size: 12px;
      margin-right: 6px;
    }

    .v {
      font-size: 18px;
      font-weight: 600;
    }

    &.pass .v {
      color: #12a150;
    }

    &.fail .v {
      color: #e5484d;
    }

    &.skip .v {
      color: #b7791f;
    }
  }
}

.result-detail {
  padding: 8px 16px;

  .step-row {
    display: flex;
    gap: 8px;
    align-items: baseline;
    font-size: 13px;
    padding: 3px 0;

    .dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      flex: 0 0 6px;

      &.ok {
        background: #12a150;
      }

      &.no {
        background: #e5484d;
      }
    }

    .step-prompt {
      color: #5c6370;
    }

    .step-err {
      color: #e5484d;
    }
  }

  .empty-tip {
    color: #8a9099;
    font-size: 13px;
  }
}

.err-text {
  color: #e5484d;
  font-size: 12px;
  word-break: break-all;
}

.log-box {
  margin-top: 16px;

  .log-head {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    color: #3d4450;
    margin-bottom: 6px;

    .log-meta {
      color: #8a9099;
    }
  }

  .log {
    background: #1f2329;
    color: #d6e0ea;
    border-radius: 8px;
    padding: 12px;
    max-height: 340px;
    overflow: auto;
    font-size: 12px;
    line-height: 1.6;
    margin: 0;
    white-space: pre-wrap;
    word-break: break-all;
  }
}

.ctx-tip {
  margin-bottom: 14px;
}

.ctx-block {
  .ctx-label {
    font-size: 13px;
    color: #3d4450;
    margin-bottom: 8px;
    font-weight: 500;
  }
}

.doc-list {
  margin-top: 8px;

  .doc-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0;
    font-size: 13px;

    .doc-name {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .doc-meta {
      color: #8a9099;
      font-size: 12px;
      flex: 0 0 auto;
    }
  }
}

.kb-info {
  margin-top: 8px;
  font-size: 13px;
  color: #5c6370;
}

.ctx-sources {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed #eef0f3;
}

.preview-meta {
  font-size: 12px;
  color: #8a9099;
  margin-bottom: 8px;
}

.preview-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
  max-height: 88px;
  overflow: auto;
}

.kb-none {
  color: #b6bcc4;
}

.path-off {
  color: #a0a6ad;
  text-decoration: line-through;
}

.disabled-tip {
  margin-top: 10px;
  padding: 6px 10px;
  background: #f7f8fa;
  border-radius: 6px;
  color: #8a9099;
  font-size: 12px;
}

.step-editor {
  width: 100%;

  .step-line {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 8px;

    .step-no {
      flex: 0 0 20px;
      color: #8a9099;
      font-size: 12px;
      text-align: right;
    }
  }
}

.hint {
  color: #8a9099;
  font-size: 12px;
  line-height: 1.6;
  margin-top: 2px;

  &.inline {
    margin-left: 12px;
  }
}

.mini-alert {
  margin-top: 4px;
}

.yaml {
  background: #f7f8fa;
  border-radius: 8px;
  padding: 12px;
  max-height: 480px;
  overflow: auto;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
