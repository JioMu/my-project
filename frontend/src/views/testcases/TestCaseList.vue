<template>
  <div class="page-container">
    <div class="page-header">
      <h1 class="page-title">测试用例</h1>
      <div class="header-actions">
        <el-button 
          v-if="!selectAllMode && selectedTestCases.length > 0" 
          type="danger" 
          @click="batchDeleteTestCases"
          :disabled="isDeleting">
          <el-icon><Delete /></el-icon>
          批量删除 ({{ selectedTestCases.length }})
        </el-button>
        <el-button 
          v-if="!selectAllMode && selectedTestCases.length > 0"
          type="primary"
          @click="openConvertDialog(selectedTestCases, 'ui')"
          style="background:#409eff;border-color:#409eff;">
          <el-icon><Promotion /></el-icon>
          🚀 转UI自动化 ({{ selectedTestCases.length }})
        </el-button>
        <el-button 
          v-if="!selectAllMode && selectedTestCases.length > 0"
          type="success"
          @click="openConvertDialog(selectedTestCases, 'appium')"
          style="background:#67c23a;border-color:#67c23a;">
          <el-icon><Promotion /></el-icon>
          📱 转Appium用例 ({{ selectedTestCases.length }})
        </el-button>
        <el-button 
          v-if="selectAllMode"
          type="danger" 
          @click="deleteAllFiltered"
          :disabled="isDeleting">
          <el-icon><Delete /></el-icon>
          删除已选 ({{ selectedTestCases.length }})
        </el-button>
        <el-button 
          v-if="!selectAllMode && total > 0"
          type="warning" 
          plain
          @click="handleSelectAllFiltered"
          :loading="isSelectingAll">
          <el-icon><Select /></el-icon>
          全选筛选结果 ({{ total }})
        </el-button>
        <el-button 
          v-if="selectAllMode"
          type="warning" 
          @click="handleClearSelection">
          取消全选
        </el-button>
        <el-button type="primary" @click="openImportDialog">
          <el-icon><Upload /></el-icon>
          导入Excel
        </el-button>
        <el-button type="success" @click="exportToExcel">
          <el-icon><Download /></el-icon>
          导出Excel
        </el-button>
        <el-button type="primary" @click="$router.push('/ai-generation/testcases/create')">
          <el-icon><Plus /></el-icon>
          新建用例
        </el-button>
      </div>
    </div>
    
    <div class="card-container">
      <div class="filter-bar">
        <!-- Row 1: 主要筛选器 -->
        <div class="filter-row">
          <div class="filter-item filter-item--wide">
            <el-input
              v-model="searchText"
              placeholder="搜索用例标题..."
              clearable
              @input="handleSearch"
              size="default"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </div>
          <div class="filter-item">
            <el-select v-model="projectFilter" placeholder="关联项目" clearable @change="handleProjectFilterChange" size="default">
              <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
            </el-select>
          </div>
          <div class="filter-item">
            <el-select v-model="priorityFilter" placeholder="优先级" clearable @change="handleFilter" size="default">
              <el-option label="低" value="low" />
              <el-option label="中" value="medium" />
              <el-option label="高" value="high" />
              <el-option label="紧急" value="critical" />
            </el-select>
          </div>
          <div class="filter-item">
            <el-select v-model="statusFilter" placeholder="状态" clearable @change="handleFilter" size="default">
              <el-option label="草稿" value="draft" />
              <el-option label="激活" value="active" />
              <el-option label="废弃" value="deprecated" />
            </el-select>
          </div>
          <div class="filter-item">
            <el-select v-model="tagsFilter" placeholder="用例类别" @change="handleFilter" size="default">
              <el-option label="常规" value="" />
              <el-option label="冒烟用例" value="冒烟" />
            </el-select>
          </div>
          <div class="filter-item">
            <el-select v-model="versionFilter" placeholder="关联版本" clearable :disabled="!projectFilter" @change="handleVersionFilterChange" size="default">
              <el-option v-for="ver in projectVersions" :key="ver.id" :label="ver.name + (ver.is_baseline ? ' (基线)' : '')" :value="ver.id" />
            </el-select>
          </div>
          <div class="filter-item filter-item--module">
            <el-select v-model="featureModuleFilter" placeholder="功能模块" clearable filterable :disabled="!projectFilter" @change="handleFeatureModuleChange" size="default" class="module-select">
              <el-option-group v-if="featureModuleGroups.used && featureModuleGroups.used.length" label="✅ 本版本用例在用">
                <el-option v-for="fm in featureModuleGroups.used" :key="'used-' + fm.id" :label="fm.name" :value="fm.id" />
              </el-option-group>
              <el-option-group v-if="featureModuleGroups.version.length" :label="`已关联 ${selectedVersionName}`">
                <el-option v-for="fm in featureModuleGroups.version" :key="fm.id" :label="fm.name" :value="fm.id" />
              </el-option-group>
              <el-option-group v-if="featureModuleGroups.unassociated.length" label="未关联版本">
                <el-option v-for="fm in featureModuleGroups.unassociated" :key="fm.id" :label="fm.name" :value="fm.id" />
              </el-option-group>
              <el-option-group v-if="featureModuleGroups.other && featureModuleGroups.other.length" label="关联了其他版本">
                <el-option v-for="fm in featureModuleGroups.other" :key="'other-' + fm.id" :label="fm.name" :value="fm.id" />
              </el-option-group>
              <el-option v-for="fm in featureModuleGroups.all" :key="fm.id" :label="fm.name" :value="fm.id" />
            </el-select>
            <el-button size="small" circle @click="openQuickCreateFeatureModule" title="快速新建功能模块" class="quick-add-btn">
              <el-icon><Plus /></el-icon>
            </el-button>
          </div>
          <div class="filter-item">
            <el-select v-model="sourceCanvasFilter" placeholder="来源画布" clearable filterable :disabled="!projectFilter" @change="handleFilter" size="default">
              <el-option v-for="c in sourceTagOptions.canvases" :key="c" :label="c.replace('画布:', '')" :value="c" />
            </el-select>
          </div>
          <div class="filter-item">
            <el-select v-model="sourceFolderFilter" placeholder="来源文件夹" clearable filterable :disabled="!projectFilter" @change="handleFilter" size="default">
              <el-option v-for="f in sourceTagOptions.folders" :key="f" :label="f.replace('文件夹:', '')" :value="f" />
            </el-select>
          </div>
        </div>
        <!-- Row 2: 测试点（功能模块下级） + 操作区 -->
        <div class="filter-row filter-row--sub">
          <div class="cascade-arrow" v-show="featureModuleFilter">
            <span>↳</span>
          </div>
          <div class="filter-item filter-item--module">
            <el-select 
              v-model="testPointFilter" 
              placeholder="选择测试点" 
              clearable 
              :disabled="!featureModuleFilter"
              @change="handleFilter" 
              size="default"
              class="module-select"
            >
              <el-option v-for="tp in allTestPoints" :key="tp.id" :label="tp.name" :value="tp.id" />
            </el-select>
            <el-button size="small" circle @click="openQuickCreateTestPoint" :disabled="!featureModuleFilter" title="快速新建测试点" class="quick-add-btn">
              <el-icon><Plus /></el-icon>
            </el-button>
            <span v-if="!featureModuleFilter" class="filter-hint">← 请先选择功能模块</span>
          </div>
          <div class="filter-spacer"></div>
          <div class="filter-actions">
            <el-button text size="small" @click="resetAllFilters" :disabled="!hasActiveFilters">
              <el-icon><RefreshLeft /></el-icon>
              重置筛选
            </el-button>
          </div>
        </div>
      </div>
      
      <!-- 展示方式切换：列表 / 卡片（选择状态两者互通） -->
      <div class="view-bar">
        <span class="view-bar-label">展示方式</span>
        <el-radio-group v-model="viewMode" size="small">
          <el-radio-button value="list">
            <el-icon><List /></el-icon> 列表
          </el-radio-button>
          <el-radio-button value="card">
            <el-icon><Grid /></el-icon> 卡片
          </el-radio-button>
        </el-radio-group>
        <span class="view-bar-hint">
          <template v-if="viewMode === 'card'">
            卡片视图同样支持勾选、批量操作与翻页
          </template>
        </span>
      </div>

      <div v-if="viewMode === 'list'" class="table-wrapper">
      <el-table
        ref="tableRef"
        :data="testcases"
        v-loading="loading"
        style="width: 100%"
        show-overflow-tooltip
        @selection-change="handleSelectionChange">
        <el-table-column type="selection" width="45" :show-overflow-tooltip="false" />
        <el-table-column type="index" label="序号" width="60" :index="getSerialNumber" :show-overflow-tooltip="false" />
        <el-table-column prop="title" label="用例标题" min-width="280">
          <template #default="{ row }">
            <el-link @click="goToTestCase(row.id)" type="primary">
              {{ row.title }}
            </el-link>
            <el-tag v-if="row.steps && row.steps.includes('【步骤缺失')" size="mini" type="danger" style="margin-left:6px">步骤缺失</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="project.name" label="关联项目" min-width="120">
          <template #default="{ row }">
            {{ row.project?.name || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="versions" label="关联版本" min-width="140">
          <template #default="{ row }">
            <div v-if="row.versions && row.versions.length > 0" class="version-tags">
              <el-tag
                v-for="version in row.versions.slice(0, 2)"
                :key="version.id"
                size="small"
                :type="version.is_baseline ? 'warning' : 'info'"
                class="version-tag"
              >
                {{ version.name }}
              </el-tag>
              <el-tooltip v-if="row.versions.length > 2" :content="getVersionsTooltip(row.versions)">
                <el-tag size="small" type="info" class="version-tag">
                  +{{ row.versions.length - 2 }}
                </el-tag>
              </el-tooltip>
            </div>
            <span v-else class="no-version">未关联版本</span>
          </template>
        </el-table-column>
        <el-table-column prop="feature_modules" label="功能模块" min-width="140">
          <template #default="{ row }">
            <div v-if="row.feature_modules && row.feature_modules.length > 0" class="version-tags">
              <el-tag
                v-for="fm in row.feature_modules.slice(0, 2)"
                :key="fm.id"
                size="small"
                type="success"
                class="version-tag"
              >
                {{ fm.name }}
              </el-tag>
              <el-tooltip v-if="row.feature_modules.length > 2" :content="getFeatureModulesTooltip(row.feature_modules)">
                <el-tag size="small" type="success" class="version-tag">
                  +{{ row.feature_modules.length - 2 }}
                </el-tag>
              </el-tooltip>
            </div>
            <span v-else class="no-version">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="test_point" label="测试点" min-width="120">
          <template #default="{ row }">
            <div v-if="row.test_point && row.test_point.name">
              <el-tag size="small" type="warning" class="version-tag">
                {{ row.test_point.name }}
              </el-tag>
            </div>
            <span v-else class="no-version">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="priority" label="优先级" min-width="70">
          <template #default="{ row }">
            <el-tag :class="`priority-tag ${row.priority}`">{{ getPriorityText(row.priority) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" min-width="70">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)">{{ getStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="来源" min-width="80">
          <template #default="{ row }">
            <el-tag :type="getSourceType(row.source)" size="small">{{ getSourceText(row.source) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="执行结果" min-width="80">
          <template #default="{ row }">
            <el-tag v-if="row.latest_execution_result === 'pass'" type="success" size="small">✅ 通过</el-tag>
            <el-tag v-else-if="row.latest_execution_result === 'fail'" type="danger" size="small">❌ 失败</el-tag>
            <span v-else class="no-version">未执行</span>
          </template>
        </el-table-column>
        <el-table-column prop="test_type" label="测试类型" min-width="80">
          <template #default="{ row }">
            {{ getTypeText(row.test_type) }}
          </template>
        </el-table-column>
        <el-table-column prop="author.username" label="作者" min-width="100" />
        <el-table-column prop="created_at" label="创建时间" min-width="160">
          <template #default="{ row }">
            {{ formatDate(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="290" fixed="right" :show-overflow-tooltip="false">
          <template #default="{ row }">
            <el-button size="small" type="success" @click="openExecuteDialog(row)">执行</el-button>
            <el-button size="small" @click="editTestCase(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="deleteTestCase(row)">删除</el-button>
            <el-button
              size="small"
              type="primary"
              @click="openConvertDialog([row], 'ui')"
              title="将这条用例转为UI自动化结构化用例"
              style="background:#409eff;border-color:#409eff;">🚀 转UI</el-button>
            <el-button
              size="small"
              type="success"
              @click="openConvertDialog([row], 'appium')"
              title="转为UI自动化结构化用例（Appium App自动化），需回填元素定位器"
              style="background:#67c23a;border-color:#67c23a;">📱 转Appium</el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>

      <!-- 卡片视图：一条用例一张卡，字段与列表一致 -->
      <div v-if="viewMode === 'card'" class="card-grid" v-loading="loading">
        <div v-if="!testcases.length && !loading" class="card-empty">暂无用例</div>
        <div
          v-for="(row, idx) in testcases"
          :key="row.id"
          class="case-card"
          :class="{ 'is-selected': isCardSelected(row) }"
        >
          <div class="card-head">
            <el-checkbox
              :model-value="isCardSelected(row)"
              @change="(v) => toggleCardSelect(row, v)"
            />
            <span class="card-index">#{{ getSerialNumber(idx) }}</span>
            <el-link class="card-title" type="primary" @click="goToTestCase(row.id)">
              {{ row.title }}
            </el-link>
            <el-tag v-if="row.steps && row.steps.includes('【步骤缺失')" size="small" type="danger">
              步骤缺失
            </el-tag>
          </div>

          <div class="card-tags">
            <el-tag :class="`priority-tag ${row.priority}`" size="small">{{ getPriorityText(row.priority) }}</el-tag>
            <el-tag :type="getStatusType(row.status)" size="small">{{ getStatusText(row.status) }}</el-tag>
            <el-tag :type="getSourceType(row.source)" size="small">{{ getSourceText(row.source) }}</el-tag>
            <el-tag v-if="row.latest_execution_result === 'pass'" type="success" size="small">✅ 通过</el-tag>
            <el-tag v-else-if="row.latest_execution_result === 'fail'" type="danger" size="small">❌ 失败</el-tag>
            <span v-else class="card-muted">未执行</span>
            <span v-if="row.test_type" class="card-type">{{ getTypeText(row.test_type) }}</span>
          </div>

          <!-- 用例正文：前置条件 / 操作步骤 / 预期结果（与详情页同样按富文本渲染） -->
          <div class="card-body">
            <div v-if="row.preconditions" class="card-block">
              <div class="block-label">前置条件</div>
              <div class="block-text" v-html="row.preconditions"></div>
            </div>
            <div class="card-block">
              <div class="block-label">操作步骤</div>
              <div class="block-text" v-html="row.steps || '无'"></div>
            </div>
            <div class="card-block">
              <div class="block-label">预期结果</div>
              <div class="block-text" v-html="row.expected_result || '无'"></div>
            </div>
          </div>

          <div class="card-meta">
            <span class="card-field">
              <b>项目</b>
              <em>{{ row.project?.name || '-' }}</em>
            </span>
            <span class="card-field">
              <b>版本</b>
              <em>
                <template v-if="row.versions && row.versions.length">
                  <el-tag
                    v-for="v in row.versions.slice(0, 2)"
                    :key="v.id"
                    size="small"
                    :type="v.is_baseline ? 'warning' : 'info'"
                    class="mini-tag"
                  >{{ v.name }}</el-tag>
                  <el-tooltip v-if="row.versions.length > 2" :content="getVersionsTooltip(row.versions)">
                    <el-tag size="small" type="info" class="mini-tag">+{{ row.versions.length - 2 }}</el-tag>
                  </el-tooltip>
                </template>
                <span v-else class="card-muted">未关联版本</span>
              </em>
            </span>
            <span class="card-field">
              <b>模块</b>
              <em>
                <template v-if="row.feature_modules && row.feature_modules.length">
                  <el-tag
                    v-for="fm in row.feature_modules.slice(0, 2)"
                    :key="fm.id"
                    size="small"
                    type="success"
                    class="mini-tag"
                  >{{ fm.name }}</el-tag>
                  <el-tooltip v-if="row.feature_modules.length > 2" :content="getFeatureModulesTooltip(row.feature_modules)">
                    <el-tag size="small" type="success" class="mini-tag">+{{ row.feature_modules.length - 2 }}</el-tag>
                  </el-tooltip>
                </template>
                <span v-else class="card-muted">-</span>
              </em>
            </span>
            <span class="card-field">
              <b>测试点</b>
              <em>
                <el-tag v-if="row.test_point && row.test_point.name" size="small" type="warning" class="mini-tag">
                  {{ row.test_point.name }}
                </el-tag>
                <span v-else class="card-muted">-</span>
              </em>
            </span>
          </div>

          <div class="card-foot">
            <span class="card-sub">
              {{ row.author?.username || '-' }} · {{ formatDate(row.created_at) }}
            </span>
            <div class="card-actions">
              <el-button size="small" type="success" @click="openExecuteDialog(row)">执行</el-button>
              <el-button size="small" @click="editTestCase(row)">编辑</el-button>
              <el-button size="small" type="danger" @click="deleteTestCase(row)">删除</el-button>
              <el-button
                size="small"
                type="primary"
                title="将这条用例转为UI自动化结构化用例"
                @click="openConvertDialog([row], 'ui')"
              >🚀 转UI</el-button>
              <el-button
                size="small"
                type="success"
                title="转为UI自动化结构化用例（Appium App自动化），需回填元素定位器"
                @click="openConvertDialog([row], 'appium')"
              >📱 转Appium</el-button>
            </div>
          </div>
        </div>
      </div>

      <div class="pagination-container">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="handlePageChange"
        />
      </div>
    </div>

    <!-- 快速创建功能模块弹窗 -->
    <el-dialog v-model="showQuickCreateFeatureModule" title="⚡ 快速新建功能模块" width="450px" :close-on-click-modal="false">
      <el-form label-width="80px">
        <el-form-item label="模块名称" required>
          <el-input v-model="quickFeatureModuleForm.name" placeholder="例如：用户登录、搜索功能" maxlength="100" />
        </el-form-item>
        <el-form-item label="关联项目" required>
          <el-select v-model="quickFeatureModuleForm.project_id" placeholder="请选择项目" style="width:100%">
            <el-option v-for="project in projects" :key="project.id" :label="project.name" :value="project.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="模块描述">
          <el-input v-model="quickFeatureModuleForm.description" type="textarea" :rows="2" placeholder="可选填模块描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showQuickCreateFeatureModule = false">取消</el-button>
        <el-button type="primary" @click="saveQuickFeatureModule" :loading="quickFeatureModuleSaving" :disabled="!quickFeatureModuleForm.name.trim() || !quickFeatureModuleForm.project_id">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- 快速创建测试点弹窗 -->
    <el-dialog v-model="showQuickCreateTestPoint" title="⚡ 快速新建测试点" width="450px" :close-on-click-modal="false">
      <el-form label-width="80px">
        <el-form-item label="所属模块">
          <el-input :model-value="currentFeatureModuleName" disabled />
        </el-form-item>
        <el-form-item label="测试点名称" required>
          <el-input v-model="quickTestPointForm.name" placeholder="例如：登录验证、密码校验" maxlength="100" />
        </el-form-item>
        <el-form-item label="测试点描述">
          <el-input v-model="quickTestPointForm.description" type="textarea" :rows="2" placeholder="可选填测试点描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showQuickCreateTestPoint = false">取消</el-button>
        <el-button type="primary" @click="saveQuickTestPoint" :loading="quickTestPointSaving" :disabled="!quickTestPointForm.name.trim()">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- 执行用例弹窗 -->
    <el-dialog v-model="showExecuteDialog" title="执行测试用例" width="420px" :close-on-click-modal="false">
      <div class="execute-info">
        <p><strong>用例：</strong>{{ executeTarget?.title }}</p>
        <el-form label-width="80px" style="margin-top: 16px">
          <el-form-item label="执行结果" required>
            <el-radio-group v-model="executeForm.result">
              <el-radio value="pass">✅ 通过</el-radio>
              <el-radio value="fail">❌ 失败</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="executeForm.notes" type="textarea" :rows="3" placeholder="可选：备注信息" />
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="showExecuteDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmExecute" :loading="executing">
          {{ executing ? '提交中...' : '确认执行' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
    <!-- 转换为 UI 自动化用例：选择目标项目 -->
    <el-dialog v-model="convertAppiumVisible" :title="convertMode === 'ui' ? '转为 UI 自动化用例' : '转为 Appium 结构化用例'" width="480px" @closed="resetConvertAppium">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="将生成结构化用例（步骤 + 占位元素）"
        :description="convertMode === 'ui' ? '转换后将跳转到 UI 自动化测试的用例库，可在那里编辑步骤、回填元素定位器后执行。每条用例会拆成步骤骨架，交互步骤带占位元素。' : '转换后将跳转到 App 自动化「真机录制」页面，可在那里选择设备/App执行。每条用例会拆成步骤骨架，交互步骤带占位元素。'"
        style="margin-bottom:16px;"
      />
      <el-form label-width="110px">
        <el-form-item label="目标项目" required>
          <el-select v-model="selectedUiProject" placeholder="选择 UI 自动化项目" style="width:100%" :loading="loadingUiProjects">
            <el-option v-for="p in uiProjects" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="待转换">
          {{ convertAppiumCases.length }} 条用例
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="convertAppiumVisible = false">取消</el-button>
        <el-button type="success" :loading="convertingAppium" @click="confirmConvertAppium">确定转换</el-button>
      </template>
    </el-dialog>

    <!-- 导入测试用例弹窗 -->
    <el-dialog v-model="showImportDialog" title="导入测试用例" width="580px" :close-on-click-modal="false" @closed="resetImport">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="导入强制要求"
        description="必须选择「所属项目」和「关联版本」，这两个值将应用到 Excel 中的每一条用例；文件内的关联项目/版本列会被忽略。Excel 表头需包含「用例标题」「预期结果」（其余列为可选）。"
        style="margin-bottom:16px;"
      />
      <el-form label-width="110px">
        <el-form-item label="所属项目" required>
          <el-select v-model="importProject" placeholder="请选择项目" style="width:100%" :disabled="importing" @change="importProjectChange">
            <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="关联版本" required>
          <el-select v-model="importVersions" multiple placeholder="请选择版本" style="width:100%" :disabled="!importProject || importing">
            <el-option v-for="v in importProjectVersions" :key="v.id" :label="v.name + (v.is_baseline ? ' (基线)' : '')" :value="v.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="选择Excel" required>
          <el-upload
            :auto-upload="false"
            :limit="1"
            :on-change="handleImportFileChange"
            :on-remove="handleImportFileRemove"
            :file-list="importFileList"
            accept=".xlsx,.xls,.csv"
            style="width:100%">
            <el-button type="primary">选择文件</el-button>
            <template #tip>
              <div class="el-upload__tip">支持从本系统导出的 Excel（.xlsx/.xls），表头需含「用例标题」「预期结果」</div>
            </template>
          </el-upload>
        </el-form-item>
        <el-form-item v-if="importPreview" label="解析预览">
          <span :class="importPreview.invalid > 0 ? 'no-version' : ''">
            共解析 {{ importPreview.total }} 条，有效 {{ importPreview.valid }} 条，缺失必填（标题/预期结果）{{ importPreview.invalid }} 条
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showImportDialog = false">取消</el-button>
        <el-button type="success" :loading="importing" :disabled="!canImport" @click="confirmImport">确认导入</el-button>
      </template>
    </el-dialog>

</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Download, Delete, RefreshLeft, Select, Promotion, Upload, List, Grid } from '@element-plus/icons-vue'
import api from '@/utils/api'
import dayjs from 'dayjs'
import * as XLSX from 'xlsx'
import { convertTestcasesToAppium, getUiProjects } from '@/api/ui_automation'

const router = useRouter()
const route = useRoute()
const loading = ref(false)
const tableRef = ref(null)
const testcases = ref([])
const projects = ref([])
const pageSize = ref(20)
const total = ref(0)
// 从 URL query 恢复筛选条件（支持返回不丢失）
// 注意：route.query 值全是字符串，数字字段需显式转 Number，否则 el-select 无法匹配 options
const parseNum = (v) => { const n = parseInt(v); return isNaN(n) ? '' : n }
const currentPage = ref(parseInt(route.query.page) || 1)
const searchText = ref(route.query.search || '')
const projectFilter = ref(parseNum(route.query.project))
const priorityFilter = ref(route.query.priority || '')
const statusFilter = ref(route.query.status || '')
const tagsFilter = ref(route.query.tags || '')
const versionFilter = ref(parseNum(route.query.version))
const featureModuleFilter = ref(parseNum(route.query.feature_module))
const testPointFilter = ref(parseNum(route.query.test_point))
// 来源筛选（墨刀采纳时把来源写入用例 tags：画布:P{n} {name} / 文件夹:{folder}）
const sourceCanvasFilter = ref(route.query.source_canvas || '')
const sourceFolderFilter = ref(route.query.source_folder || '')
const sourceTagOptions = ref({ canvases: [], folders: [] })
// 本版本用例实际在用的功能模块（优先展示；修复按版本分组时「已用模块因归属其他版本而消失」的问题）
const usedModules = ref([])
const allVersions = ref([])
const allFeatureModules = ref([])
const allTestPoints = ref([])
const selectedTestCases = ref([])
const isDeleting = ref(false)
const isSelectingAll = ref(false)  // 正在全选筛选结果（分页拉取中）
const selectAllMode = ref(false)  // 全选模式：true=已全选当前筛选条件下的全部数据

// 展示方式：list=表格 / card=卡片。持久化到 localStorage，刷新后保持上次选择。
const VIEW_MODE_KEY = 'testhub:testcase:view-mode'
const viewMode = ref(localStorage.getItem(VIEW_MODE_KEY) === 'card' ? 'card' : 'list')
watch(viewMode, async (val) => {
  localStorage.setItem(VIEW_MODE_KEY, val)
  if (val !== 'list') return
  // 从卡片切回表格：el-table 的勾选状态由内部维护，卡片里改动的选择需要回灌，
  // 否则会出现「卡片选了 3 条，切回表格全没勾」。
  await nextTick()
  const picked = new Set(selectedTestCases.value.map(c => c.id))
  testcases.value.forEach(row => {
    tableRef.value?.toggleRowSelection(row, selectAllMode.value || picked.has(row.id))
  })
})

// 快速创建功能模块
const showQuickCreateFeatureModule = ref(false)
const quickFeatureModuleSaving = ref(false)
const quickFeatureModuleForm = reactive({
  name: '',
  project_id: '',
  description: ''
})

const openQuickCreateFeatureModule = () => {
  showQuickCreateFeatureModule.value = true
  quickFeatureModuleForm.name = ''
  quickFeatureModuleForm.project_id = projectFilter.value || ''
  quickFeatureModuleForm.description = ''
}

const saveQuickFeatureModule = async () => {
  if (!quickFeatureModuleForm.name.trim()) {
    ElMessage.warning('请输入模块名称')
    return
  }
  if (!quickFeatureModuleForm.project_id) {
    ElMessage.warning('请选择关联项目')
    return
  }
  quickFeatureModuleSaving.value = true
  try {
    const payload = {
      name: quickFeatureModuleForm.name.trim(),
      description: quickFeatureModuleForm.description.trim(),
      project_id: quickFeatureModuleForm.project_id
    }
    // 如果当前选了版本，自动关联到该版本
    if (versionFilter.value) {
      payload.version_ids = [Number(versionFilter.value)]
    }
    await api.post('/feature-modules/', payload)
    ElMessage.success(`功能模块「${quickFeatureModuleForm.name}」创建成功`)
    showQuickCreateFeatureModule.value = false
    await fetchFeatureModules(projectFilter.value)
  } catch (error) {
    // 优先提取字段级错误（DRF ValidationError格式）
    const errData = error.response?.data
    let msg = '创建功能模块失败'
    if (errData?.detail) {
      msg = errData.detail
    } else if (errData && typeof errData === 'object') {
      const fieldErrors = Object.values(errData).flat()
      if (fieldErrors.length > 0) msg = fieldErrors[0]
    }
    ElMessage.error(msg)
  } finally {
    quickFeatureModuleSaving.value = false
  }
}

// 快速创建测试点
const showQuickCreateTestPoint = ref(false)
const quickTestPointSaving = ref(false)
const quickTestPointForm = reactive({
  name: '',
  description: ''
})

const currentFeatureModuleName = computed(() => {
  const fm = allFeatureModules.value.find(f => f.id === featureModuleFilter.value)
  return fm ? fm.name : '未选择'
})

const openQuickCreateTestPoint = () => {
  if (!featureModuleFilter.value) {
    ElMessage.warning('请先选择功能模块')
    return
  }
  showQuickCreateTestPoint.value = true
  quickTestPointForm.name = ''
  quickTestPointForm.description = ''
}

const saveQuickTestPoint = async () => {
  if (!quickTestPointForm.name.trim()) {
    ElMessage.warning('请输入测试点名称')
    return
  }
  quickTestPointSaving.value = true
  try {
    await api.post('/feature-modules/test-points/', {
      name: quickTestPointForm.name.trim(),
      description: quickTestPointForm.description.trim(),
      feature_module_id: featureModuleFilter.value
    })
    ElMessage.success(`测试点「${quickTestPointForm.name}」创建成功`)
    showQuickCreateTestPoint.value = false
    await fetchTestPoints(featureModuleFilter.value)
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || '创建测试点失败')
  } finally {
    quickTestPointSaving.value = false
  }
}

// =============== 执行用例 ===============
const showExecuteDialog = ref(false)
const executeTarget = ref(null)
const executing = ref(false)
const executeForm = reactive({
  result: 'pass',
  notes: ''
})

const openExecuteDialog = (row) => {
  executeTarget.value = row
  executeForm.result = 'pass'
  executeForm.notes = ''
  showExecuteDialog.value = true
}

const confirmExecute = async () => {
  if (!executeForm.result) {
    ElMessage.warning('请选择执行结果')
    return
  }
  executing.value = true
  try {
    await api.post(`/testcases/${executeTarget.value.id}/execute/`, {
      testcase: executeTarget.value.id,
      result: executeForm.result,
      notes: executeForm.notes
    })
    ElMessage.success(`执行完成：${executeForm.result === 'pass' ? '通过 ✅' : '失败 ❌'}`)
    showExecuteDialog.value = false
    await fetchTestCases()
  } catch (error) {
    ElMessage.error('执行失败: ' + (error.response?.data?.detail || error.message))
  } finally {
    executing.value = false
  }
}

const fetchTestCases = async () => {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      search: searchText.value,
      project: projectFilter.value,
      priority: priorityFilter.value,
      status: statusFilter.value,
      tags: combinedTags.value,
      version: versionFilter.value,
      feature_module: featureModuleFilter.value,
      test_point: testPointFilter.value
    }
    const response = await api.get('/testcases/', { params })
    testcases.value = response.data.results || []
    total.value = response.data.count || 0
    // 数据更新后清除选择状态
    selectAllMode.value = false
    tableRef.value?.clearSelection()
  } catch (error) {
    ElMessage.error('获取测试用例列表失败')
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  currentPage.value = 1
  syncFiltersToUrl()
  fetchTestCases()
}

const handleFilter = () => {
  currentPage.value = 1
  syncFiltersToUrl()
  fetchTestCases()
}

const handleProjectFilterChange = () => {
  // 清空下游筛选条件
  versionFilter.value = ''
  featureModuleFilter.value = ''
  testPointFilter.value = ''
  sourceCanvasFilter.value = ''
  sourceFolderFilter.value = ''
  usedModules.value = []
  allTestPoints.value = []
  selectAllMode.value = false
  currentPage.value = 1
  syncFiltersToUrl()
  fetchTestCases()
  // 切换项目后刷新功能模块下拉（按新项目过滤）
  fetchFeatureModules(projectFilter.value)
  fetchSourceTags()
}

const handleFeatureModuleChange = () => {
  // 清空测试点筛选
  testPointFilter.value = ''
  if (featureModuleFilter.value) {
    fetchTestPoints(featureModuleFilter.value)
  } else {
    allTestPoints.value = []
  }
  currentPage.value = 1
  syncFiltersToUrl()
  fetchTestCases()
}

// 版本筛选变化时清空功能模块和测试点（模块需匹配版本的归属项目）
const handleVersionFilterChange = () => {
  featureModuleFilter.value = ''
  testPointFilter.value = ''
  sourceCanvasFilter.value = ''
  sourceFolderFilter.value = ''
  allTestPoints.value = []
  selectAllMode.value = false
  currentPage.value = 1
  syncFiltersToUrl()
  fetchTestCases()
  fetchUsedModules()
  fetchSourceTags()
}

// 拉取「本版本用例实际在用」的功能模块（置顶多分组）
const fetchUsedModules = async () => {
  if (!versionFilter.value) {
    usedModules.value = []
    return
  }
  try {
    const params = { used_with_version: versionFilter.value }
    if (projectFilter.value) params.project = projectFilter.value
    const response = await api.get('/feature-modules/', { params })
    usedModules.value = response.data.results || response.data || []
  } catch (error) {
    console.error('获取本版本在用模块失败:', error)
    usedModules.value = []
  }
}

// 拉取来源画布/来源文件夹筛选项（后端聚合当前项目+版本下用例 tags 中的来源标签）
const fetchSourceTags = async () => {
  if (!projectFilter.value) {
    sourceTagOptions.value = { canvases: [], folders: [] }
    return
  }
  try {
    const params = { project: projectFilter.value }
    if (versionFilter.value) params.version = versionFilter.value
    const response = await api.get('/testcases/source-tags/', { params })
    sourceTagOptions.value = response.data || { canvases: [], folders: [] }
  } catch (error) {
    console.error('获取来源筛选项失败:', error)
  }
}

// 根据选中项目过滤的版本列表
const projectVersions = computed(() => {
  if (!projectFilter.value) {
    return allVersions.value
  }
  const pid = Number(projectFilter.value)
  return allVersions.value.filter(v => {
    if (v.projects && Array.isArray(v.projects)) {
      return v.projects.some(p => p.id === pid)
    }
    return false
  })
})

// 选中版本的名称（用于下拉分组标题）
const selectedVersionName = computed(() => {
  if (!versionFilter.value) return ''
  const v = allVersions.value.find(v => v.id === Number(versionFilter.value))
  return v ? v.name : ''
})

// 功能模块下拉分组：属于选中版本的归「已关联」，未填写版本的归「未关联版本」，
// 关联了其他版本的归「其他」（此前会被整个丢弃，导致用户筛不到想看的模块）。
// 「本版本用例在用」来自 usedModules（该版本下用例实际关联的模块），置顶展示。
const featureModuleGroups = computed(() => {
  // 选了版本 → 按版本关联分组
  if (versionFilter.value) {
    const vid = Number(versionFilter.value)
    const version = allVersions.value.find(v => v.id === vid)
    const projectIds = (version && version.projects) ? version.projects.map(p => p.id) : []
    const versionMods = []
    const unassociated = []
    const other = []
    for (const fm of allFeatureModules.value) {
      const pid = fm.project ? fm.project.id : fm.project_id
      if (projectIds.length && !projectIds.includes(pid)) continue
      const versions = fm.versions || []
      if (versions.some(v => v.id === vid)) versionMods.push(fm)
      else if (versions.length === 0) unassociated.push(fm)
      else other.push(fm)
    }
    return { used: usedModules.value, version: versionMods, unassociated, other, all: [] }
  }
  // 只选了项目 → 该项目下所有模块（扁平）
  if (projectFilter.value) {
    const pid = Number(projectFilter.value)
    return {
      used: [],
      version: [],
      unassociated: [],
      other: [],
      all: allFeatureModules.value.filter(fm => {
        const fpid = fm.project ? fm.project.id : fm.project_id
        return fpid === pid
      })
    }
  }
  return { used: [], version: [], unassociated: [], other: [], all: [] }
})

// 用例类别 + 来源画布 + 来源文件夹 合并为逗号分隔的 tags 参数（后端按「且」过滤）
const combinedTags = computed(() => {
  const arr = []
  if (tagsFilter.value) arr.push(tagsFilter.value)
  if (sourceCanvasFilter.value) arr.push(sourceCanvasFilter.value)
  if (sourceFolderFilter.value) arr.push(sourceFolderFilter.value)
  return arr.join(',')
})

// 是否有激活的筛选器
const hasActiveFilters = computed(() => {
  return searchText.value || projectFilter.value || priorityFilter.value ||
    statusFilter.value || tagsFilter.value || versionFilter.value || featureModuleFilter.value ||
    testPointFilter.value || sourceCanvasFilter.value || sourceFolderFilter.value
})

// 重置所有筛选器
const resetAllFilters = () => {
  searchText.value = ''
  projectFilter.value = ''
  priorityFilter.value = ''
  statusFilter.value = ''
  tagsFilter.value = ''
  versionFilter.value = ''
  featureModuleFilter.value = ''
  testPointFilter.value = ''
  sourceCanvasFilter.value = ''
  sourceFolderFilter.value = ''
  usedModules.value = []
  allTestPoints.value = []
  selectAllMode.value = false
  tableRef.value?.clearSelection()
  currentPage.value = 1
  router.replace({ query: {} })  // 清除 URL 中的筛选参数
  fetchTestCases()
}

// 将筛选条件同步到 URL query（支持返回/刷新不丢失）
const syncFiltersToUrl = () => {
  const query = {}
  if (searchText.value) query.search = searchText.value
  if (projectFilter.value) query.project = projectFilter.value
  if (priorityFilter.value) query.priority = priorityFilter.value
  if (statusFilter.value) query.status = statusFilter.value
  if (tagsFilter.value) query.tags = tagsFilter.value
  if (versionFilter.value) query.version = versionFilter.value
  if (featureModuleFilter.value) query.feature_module = featureModuleFilter.value
  if (testPointFilter.value) query.test_point = testPointFilter.value
  if (sourceCanvasFilter.value) query.source_canvas = sourceCanvasFilter.value
  if (sourceFolderFilter.value) query.source_folder = sourceFolderFilter.value
  if (currentPage.value > 1) query.page = String(currentPage.value)
  router.replace({ query })
}

const fetchTestPoints = async (moduleId) => {
  try {
    const response = await api.get(`/feature-modules/modules/${moduleId}/test-points/`)
    allTestPoints.value = response.data || response.data.results || []
  } catch (error) {
    console.error('获取测试点列表失败:', error)
    allTestPoints.value = []
  }
}

const handlePageChange = () => {
  syncFiltersToUrl()
  fetchTestCases()
}

const goToTestCase = (id) => {
  // 将当前列表的筛选条件带入详情页，使「上一个/下一个」在同一筛选集合内切换
  const q = {}
  const src = route.query
  ;['search', 'project', 'priority', 'status', 'version', 'feature_module', 'test_point'].forEach(k => {
    if (src[k]) q[k] = src[k]
  })
  router.push({ path: `/ai-generation/testcases/${id}`, query: q })
}

const editTestCase = (testcase) => {
  // 将当前列表的筛选条件带入编辑页，保存后返回详情页才能保持筛选后的用例集合
  const q = {}
  const src = route.query
  ;['search', 'project', 'priority', 'status', 'version', 'feature_module', 'test_point'].forEach(k => {
    if (src[k]) q[k] = src[k]
  })
  router.push({ path: `/ai-generation/testcases/${testcase.id}/edit`, query: q })
}

const deleteTestCase = async (testcase) => {
  try {
    await ElMessageBox.confirm('确定要删除这个测试用例吗？', '警告', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })
    
    await api.delete(`/testcases/${testcase.id}/`)
    ElMessage.success('测试用例删除成功')
    fetchTestCases()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('测试用例删除失败')
    }
  }
}

// 卡片视图的选中判定：全选模式下当前页视为全部选中（与表格行为一致）
const isCardSelected = (row) => {
  if (selectAllMode.value) return true
  return selectedTestCases.value.some(c => c.id === row.id)
}

// 卡片视图勾选 / 取消勾选（与 selectedTestCases 保持同一份状态）
const toggleCardSelect = (row, checked) => {
  if (selectAllMode.value) {
    if (checked) return
    // 全选模式下取消某一条 → 退出全选，回退为「当前页除该条外全部选中」，与表格语义一致
    selectAllMode.value = false
    selectedTestCases.value = testcases.value.filter(r => r.id !== row.id)
    return
  }
  const list = selectedTestCases.value.slice()
  const idx = list.findIndex(c => c.id === row.id)
  if (checked && idx === -1) list.push(row)
  if (!checked && idx >= 0) list.splice(idx, 1)
  selectedTestCases.value = list
}

// 处理选择变化
const handleSelectionChange = (selection) => {
  if (selectAllMode.value) {
    // 全选模式下：如果用户手动取消了某些勾选，退出全选模式
    const currentPageIds = new Set(testcases.value.map(r => r.id))
    const selectedIds = new Set(selection.map(r => r.id))
    for (const id of currentPageIds) {
      if (!selectedIds.has(id)) {
        // 有行被取消勾选 → 回退为普通选择模式
        selectAllMode.value = false
        selectedTestCases.value = selection
        return
      }
    }
    // 当前页全部选中，保持全选模式（selectedTestCases 维持跨页快照不变）
    return
  }
  selectedTestCases.value = selection
}

// 全选当前筛选条件下的全部数据（跨页拉取所有 ID）
const handleSelectAllFiltered = async () => {
  isSelectingAll.value = true
  try {
    const params = {
      page: 1,
      page_size: 200,  // 后端限制的单页最大条数
      search: searchText.value,
      project: projectFilter.value,
      priority: priorityFilter.value,
      status: statusFilter.value,
      version: versionFilter.value,
      feature_module: featureModuleFilter.value,
      test_point: testPointFilter.value
    }

    const allIds = []
    let hasMore = true

    while (hasMore) {
      const response = await api.get('/testcases/', { params: { ...params } })
      const results = response.data.results || []
      allIds.push(...results.map(r => r.id))
      if (!response.data.next) {
        hasMore = false
      } else {
        params.page = params.page + 1
      }
    }

    // 选中当前页在表格中的行（视觉反馈）
    const currentPageIds = new Set(testcases.value.map(r => r.id))
    testcases.value.forEach(row => {
      tableRef.value?.toggleRowSelection(row, true)
    })

    // 存储所有筛选结果的 ID 快照
    selectedTestCases.value = allIds.map(id => ({ id }))
    selectAllMode.value = true
    ElMessage.success(`已全选筛选条件下的全部 ${allIds.length} 条用例`)
  } catch (error) {
    ElMessage.error('全选失败: ' + (error.message || '未知错误'))
  } finally {
    isSelectingAll.value = false
  }
}

// 取消全选
const handleClearSelection = () => {
  selectAllMode.value = false
  tableRef.value?.clearSelection()
}

// 删除当前全选的全部用例（selectedTestCases 已持有所有 ID）
const deleteAllFiltered = async () => {
  const ids = selectedTestCases.value.map(item => item.id)
  if (ids.length === 0) {
    ElMessage.warning('没有选中的用例')
    return
  }

  try {
    await ElMessageBox.confirm(
      `确定要删除选中的全部 ${ids.length} 条测试用例吗？此操作不可恢复！`,
      '⚠️ 危险操作',
      {
        confirmButtonText: '确定删除',
        cancelButtonText: '取消',
        type: 'error',
        confirmButtonClass: 'el-button--danger'
      }
    )

    isDeleting.value = true
    ElMessage.info(`正在删除全部 ${ids.length} 条用例，请稍候...`)

    let successCount = 0
    let failCount = 0

    for (const id of ids) {
      try {
        await api.delete(`/testcases/${id}/`)
        successCount++
      } catch (error) {
        console.error(`删除测试用例 ${id} 失败:`, error)
        failCount++
      }
    }

    if (successCount > 0) {
      ElMessage.success(`成功删除 ${successCount} 条${failCount > 0 ? `，${failCount} 条失败` : ''}`)
    } else {
      ElMessage.error('删除失败')
    }

    selectAllMode.value = false
    selectedTestCases.value = []
    tableRef.value?.clearSelection()
    await refreshAfterDelete()
  } catch (error) {
    if (error !== 'cancel') {
      console.error('批量删除失败:', error)
      ElMessage.error('批量删除失败: ' + (error.message || '未知错误'))
    }
  } finally {
    isDeleting.value = false
  }
}

// 删除后刷新，如果当前页已空则回退到上一页
const refreshAfterDelete = async () => {
  await fetchTestCases()
  if (testcases.value.length === 0 && currentPage.value > 1) {
    currentPage.value--
    await fetchTestCases()
  }
}

// 获取序号
const getSerialNumber = (index) => {
  return (currentPage.value - 1) * pageSize.value + index + 1
}

// 将选中的用例库用例转为 UI 自动化用例（UI模式或Appium模式共用）
const convertMode = ref('appium') // 'ui' | 'appium'
const convertAppiumVisible = ref(false)
const convertAppiumCases = ref([])
const uiProjects = ref([])
const selectedUiProject = ref(null)
const loadingUiProjects = ref(false)
const convertingAppium = ref(false)

const openConvertDialog = async (cases, mode) => {
  if (!cases || cases.length === 0) {
    ElMessage.warning('请先选择要转换的测试用例')
    return
  }
  convertMode.value = mode
  convertAppiumCases.value = cases
  selectedUiProject.value = null
  convertAppiumVisible.value = true
  loadingUiProjects.value = true
  try {
    const res = await getUiProjects({ page_size: 999 })
    uiProjects.value = res.data.results || res.data || []
    if (uiProjects.value.length === 1) {
      selectedUiProject.value = uiProjects.value[0].id
    }
  } catch (e) {
    ElMessage.error('加载 UI 自动化项目失败')
  } finally {
    loadingUiProjects.value = false
  }
}

const resetConvertAppium = () => {
  convertAppiumCases.value = []
  selectedUiProject.value = null
}

const confirmConvertAppium = async () => {
  if (!selectedUiProject.value) {
    ElMessage.warning('请选择目标 UI 自动化项目')
    return
  }
  convertingAppium.value = true
  try {
    const res = await convertTestcasesToAppium({
      case_ids: convertAppiumCases.value.map(c => c.id),
      ui_project_id: selectedUiProject.value,
      mode: convertMode.value === 'ui' ? 'web' : 'appium'
    })
    ElMessage.success((res.data && res.data.message) || '转换成功，已转为UI自动化结构化用例')
    convertAppiumVisible.value = false
    if (selectedTestCases.value.length > 0) {
      tableRef.value?.clearSelection()
      selectedTestCases.value = []
    }
    router.push({
      path: convertMode.value === 'ui' ? '/ui-automation/test-cases' : '/app-automation/recorder',
      query: { project: 'ui_' + selectedUiProject.value }
    })
  } catch (error) {
    console.error('转换失败:', error)
    ElMessage.error('转换失败: ' + (error.response?.data?.message || error.response?.data?.error || error.message))
  } finally {
    convertingAppium.value = false
  }
}

// 批量删除
const batchDeleteTestCases = async () => {
  if (selectedTestCases.value.length === 0) {
    ElMessage.warning('请先选择要删除的测试用例')
    return
  }

  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedTestCases.value.length} 个测试用例吗？此操作不可恢复。`,
      '警告',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    isDeleting.value = true
    let successCount = 0
    let failCount = 0

    // 逐个删除选中的测试用例
    for (const testcase of selectedTestCases.value) {
      try {
        await api.delete(`/testcases/${testcase.id}/`)
        successCount++
      } catch (error) {
        console.error(`删除测试用例 ${testcase.id} 失败:`, error)
        failCount++
      }
    }

    // 显示删除结果
    if (successCount > 0) {
      ElMessage.success(`成功删除 ${successCount} 个测试用例${failCount > 0 ? `，${failCount} 个失败` : ''}`)
    } else {
      ElMessage.error('删除失败')
    }

    // 清空选择并重新加载列表
    selectedTestCases.value = []
    tableRef.value?.clearSelection()
    await refreshAfterDelete()

  } catch (error) {
    if (error !== 'cancel') {
      console.error('批量删除失败:', error)
      ElMessage.error('批量删除失败: ' + (error.message || '未知错误'))
    }
  } finally {
    isDeleting.value = false
  }
}

const getPriorityText = (priority) => {
  const textMap = {
    low: '低',
    medium: '中',
    high: '高',
    critical: '紧急'
  }
  return textMap[priority] || priority
}

const getStatusType = (status) => {
  const typeMap = {
    draft: 'info',
    active: 'success',
    deprecated: 'warning'
  }
  return typeMap[status] || 'info'
}

const getStatusText = (status) => {
  const textMap = {
    draft: '草稿',
    active: '激活',
    deprecated: '废弃'
  }
  return textMap[status] || status
}

const getSourceType = (source) => {
  const typeMap = {
    ai: 'success',
    manual: 'primary',
    unknown: 'info'
  }
  return typeMap[source] || 'info'
}

const getSourceText = (source) => {
  const textMap = {
    ai: 'AI生成',
    manual: '人工',
    unknown: '未标记'
  }
  return textMap[source] || '未标记'
}

const getTypeText = (type) => {
  const textMap = {
    functional: '功能测试',
    integration: '集成测试',
    api: 'API测试',
    ui: 'UI测试',
    performance: '性能测试',
    security: '安全测试'
  }
  return textMap[type] || '-'
}

const formatDate = (dateString) => {
  return dayjs(dateString).format('YYYY-MM-DD HH:mm')
}

const getVersionsTooltip = (versions) => {
  return versions.map(v => v.name + (v.is_baseline ? ' (基线)' : '')).join('、')
}

const getFeatureModulesTooltip = (modules) => {
  return modules.map(m => m.name).join('、')
}

const getTestPointsTooltip = (points) => {
  return points.map(p => p.name).join('、')
}

// 将HTML的<br>标签转换为换行符（用于Excel导出）
const convertBrToNewline = (text) => {
  if (!text) return ''
  return text.replace(/<br\s*\/?>/gi, '\n')
}

const exportToExcel = async () => {
  try {
    loading.value = true
    
    // 获取所有测试用例数据（不分页）
    const response = await api.get('/testcases/', { 
      params: { 
        page_size: 9999, // 获取所有数据
        search: searchText.value,
        project: projectFilter.value,
        priority: priorityFilter.value,
        status: statusFilter.value,
        version: versionFilter.value,
        feature_module: featureModuleFilter.value,
        test_point: testPointFilter.value
      } 
    })
    
    const allTestCases = response.data.results || []
    
    if (allTestCases.length === 0) {
      ElMessage.warning('没有测试用例数据可导出')
      return
    }
    
    // 创建工作簿
    const workbook = XLSX.utils.book_new()
    
    // 准备Excel数据
    const worksheetData = [
      ['测试用例编号', '用例标题', '关联项目', '关联版本', '功能模块', '测试点', '前置条件', '操作步骤', '预期结果', '优先级', '状态', '测试类型', '作者', '创建时间']
    ]
    
    allTestCases.forEach((testcase, index) => {
      const versions = testcase.versions && testcase.versions.length > 0 
        ? testcase.versions.map(v => v.name + (v.is_baseline ? '(基线)' : '')).join('、')
        : '未关联版本'
      
      const featureModules = testcase.feature_modules && testcase.feature_modules.length > 0
        ? testcase.feature_modules.map(m => m.name).join('、')
        : '-'
      
      const testPoints = testcase.test_point && testcase.test_point.name
        ? testcase.test_point.name
        : '-'
      
      worksheetData.push([
        `TC${String(index + 1).padStart(3, '0')}`,
        testcase.title || '',
        testcase.project?.name || '',
        versions,
        featureModules,
        testPoints,
        convertBrToNewline(testcase.preconditions || ''),
        convertBrToNewline(testcase.steps || ''),
        convertBrToNewline(testcase.expected_result || ''),
        getPriorityText(testcase.priority),
        getStatusText(testcase.status),
        getTypeText(testcase.test_type),
        testcase.author?.username || '',
        formatDate(testcase.created_at)
      ])
    })
    
    // 创建工作表
    const worksheet = XLSX.utils.aoa_to_sheet(worksheetData)
    
    // 设置列宽
    const colWidths = [
      { wch: 15 }, // 测试用例编号
      { wch: 30 }, // 用例标题
      { wch: 20 }, // 关联项目
      { wch: 25 }, // 关联版本
      { wch: 20 }, // 功能模块
      { wch: 20 }, // 测试点
      { wch: 30 }, // 前置条件
      { wch: 40 }, // 操作步骤
      { wch: 30 }, // 预期结果
      { wch: 10 }, // 优先级
      { wch: 10 }, // 状态
      { wch: 15 }, // 测试类型
      { wch: 15 }, // 作者
      { wch: 20 }  // 创建时间
    ]
    worksheet['!cols'] = colWidths
    
    // 设置表头样式
    for (let col = 0; col < worksheetData[0].length; col++) {
      const cellAddress = XLSX.utils.encode_cell({ r: 0, c: col })
      if (!worksheet[cellAddress]) continue
      worksheet[cellAddress].s = {
        font: { bold: true },
        alignment: { horizontal: 'center', vertical: 'center', wrapText: true }
      }
    }
    
    // 设置其他行的样式
    for (let row = 1; row < worksheetData.length; row++) {
      for (let col = 0; col < worksheetData[row].length; col++) {
        const cellAddress = XLSX.utils.encode_cell({ r: row, c: col })
        if (worksheet[cellAddress]) {
          worksheet[cellAddress].s = {
            alignment: { vertical: 'top', wrapText: true }
          }
        }
      }
    }
    
    // 添加工作表到工作簿
    XLSX.utils.book_append_sheet(workbook, worksheet, '测试用例')
    
    // 生成文件名
    const fileName = `测试用例_${new Date().toISOString().slice(0, 10)}.xlsx`
    
    // 导出文件
    XLSX.writeFile(workbook, fileName)
    
    ElMessage.success('测试用例导出成功')
  } catch (error) {
    console.error('导出测试用例失败:', error)
    ElMessage.error('导出测试用例失败: ' + (error.message || '未知错误'))
  } finally {
    loading.value = false
  }
}

const fetchProjects = async () => {
  try {
    const response = await api.get('/projects/')
    projects.value = response.data.results || response.data || []
  } catch (error) {
    ElMessage.error('获取项目列表失败')
  }
}

// =============== 导入测试用例 ===============
const showImportDialog = ref(false)
const importProject = ref(null)
const importVersions = ref([])
const importFileList = ref([])
const importParsedCases = ref([])
const importPreview = ref(null)
const importing = ref(false)

// 当前导入对话框中，按所选项目过滤的版本列表
const importProjectVersions = computed(() => {
  if (!importProject.value) return []
  const pid = Number(importProject.value)
  return allVersions.value.filter(v => {
    if (v.projects && Array.isArray(v.projects)) {
      return v.projects.some(p => p.id === pid)
    }
    return false
  })
})

// 满足「项目 + 版本 + 已解析文件」三个条件才允许导入
const canImport = computed(() => {
  return !!importProject.value && importVersions.value.length > 0 && importParsedCases.value.length > 0
})

const openImportDialog = () => {
  showImportDialog.value = true
}

const importProjectChange = () => {
  // 切换项目后清空已选版本（版本归属项目可能变化）
  importVersions.value = []
}

const resetImport = () => {
  importProject.value = null
  importVersions.value = []
  importFileList.value = []
  importParsedCases.value = []
  importPreview.value = null
  importing.value = false
}

const handleImportFileRemove = () => {
  importParsedCases.value = []
  importPreview.value = null
}

const handleImportFileChange = (uploadFile) => {
  const raw = uploadFile.raw
  if (!raw) return
  const reader = new FileReader()
  reader.onload = (e) => {
    try {
      const data = new Uint8Array(e.target.result)
      const workbook = XLSX.read(data, { type: 'array' })
      const sheetName = workbook.SheetNames[0]
      if (!sheetName) {
        ElMessage.warning('Excel 中未找到工作表')
        return
      }
      const worksheet = workbook.Sheets[sheetName]
      const rows = XLSX.utils.sheet_to_json(worksheet, { defval: '' })
      const cases = []
      let valid = 0
      let invalid = 0
      for (const row of rows) {
        const get = (...keys) => {
          for (const k of keys) {
            const val = row[k]
            if (val !== undefined && val !== null && String(val).trim() !== '') {
              return String(val).trim()
            }
          }
          return ''
        }
        const title = get('用例标题', '标题', 'title')
        const expected = get('预期结果', '预期', 'pass_criteria', 'expected_result', 'expectedResult')
        cases.push({
          title,
          expected_result: expected,
          description: get('用例描述', '描述', 'description'),
          preconditions: get('前置条件', 'preconditions'),
          steps: get('操作步骤', '步骤', 'steps'),
          priority: get('优先级', 'priority'),
          status: get('状态', 'status'),
          test_type: get('测试类型', '用例类型', 'testType'),
          feature_modules: get('功能模块', '所属模块', '模块', 'featureModules'),
          test_point: get('测试点', 'testPoint'),
          tags: get('标签', '关键词', 'tags')
        })
        if (title && expected) valid++
        else invalid++
      }
      importParsedCases.value = cases
      importPreview.value = { total: cases.length, valid, invalid }
      if (cases.length === 0) {
        ElMessage.warning('文件中未解析到任何数据行')
      }
    } catch (err) {
      console.error('解析 Excel 失败:', err)
      ElMessage.error('解析 Excel 失败，请确认文件格式是否为合法的 .xlsx/.xls')
    }
  }
  reader.readAsArrayBuffer(raw)
}

const confirmImport = async () => {
  if (!importProject.value) {
    ElMessage.warning('请选择所属项目')
    return
  }
  if (importVersions.value.length === 0) {
    ElMessage.warning('请至少选择一个关联版本')
    return
  }
  if (importParsedCases.value.length === 0) {
    ElMessage.warning('请先选择包含用例的 Excel 文件')
    return
  }
  importing.value = true
  try {
    const res = await api.post('/testcases/import/', {
      project_id: importProject.value,
      version_ids: importVersions.value,
      cases: importParsedCases.value
    })
    ElMessage.success(res.data?.message || '导入成功')
    showImportDialog.value = false
    await fetchTestCases()
  } catch (error) {
    const data = error.response?.data
    let msg = '导入失败'
    if (data?.error) {
      msg = data.error
    } else if (data && typeof data === 'object') {
      const fieldErrors = Object.values(data).flat()
      if (fieldErrors.length > 0) msg = fieldErrors[0]
    }
    ElMessage.error(msg)
  } finally {
    importing.value = false
  }
}

const fetchVersions = async () => {
  try {
    const response = await api.get('/versions/')
    allVersions.value = response.data.results || response.data || []
  } catch (error) {
    console.error('获取版本列表失败:', error)
  }
}

const fetchFeatureModules = async (projectId) => {
  try {
    const params = {}
    if (projectId) params.project = projectId
    const response = await api.get('/feature-modules/', { params })
    allFeatureModules.value = response.data.results || response.data || []
  } catch (error) {
    console.error('获取功能模块列表失败:', error)
  }
}

onMounted(async () => {
  fetchProjects()
  fetchVersions()
  // 关键：先加载功能模块列表，等 options 就位后再恢复筛选值渲染
  await fetchFeatureModules(projectFilter.value)
  // 来源筛选项 + 本版本在用模块（依赖已恢复的项目/版本筛选值）
  fetchSourceTags()
  fetchUsedModules()
  // 如果 URL 中恢复了功能模块筛选，加载对应的测试点列表
  if (featureModuleFilter.value) {
    await fetchTestPoints(featureModuleFilter.value)
  }
  fetchTestCases()
})
</script>

<style lang="scss" scoped>
.table-wrapper {
  width: 100%;
  overflow-x: auto;
}

/* ---------- 展示方式切换 ---------- */
.view-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.view-bar-label {
  font-size: 13px;
  color: #909399;
}
.view-bar-hint {
  font-size: 12px;
  color: #b1b3b8;
}

/* ---------- 卡片视图 ---------- */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(560px, 1fr));
  align-items: start;
  gap: 14px;
  min-height: 60px;
}
.card-empty {
  padding: 40px 0;
  text-align: center;
  color: #909399;
  font-size: 13px;
}
.case-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px 12px;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.case-card:hover {
  border-color: #c6e2ff;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}
.case-card.is-selected {
  border-color: #409eff;
  box-shadow: 0 0 0 1px #409eff inset;
}

.card-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.card-index {
  flex: none;
  margin-top: 1px;
  font-size: 12px;
  color: #909399;
  font-variant-numeric: tabular-nums;
}
.card-title {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 500;
  line-height: 1.5;
  word-break: break-word;
}

.card-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

/* 用例正文：前置条件 / 操作步骤 / 预期结果 */
.card-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 320px;
  overflow-y: auto;
  padding: 10px 12px;
  background: #fafbfc;
  border: 1px solid #f0f2f5;
  border-radius: 6px;
}
.card-body::-webkit-scrollbar {
  width: 6px;
}
.card-body::-webkit-scrollbar-thumb {
  background: #dcdfe6;
  border-radius: 3px;
}
.card-block {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.block-label {
  flex: none;
  font-size: 12px;
  font-weight: 600;
  color: #606266;
}
.block-label::before {
  content: '';
  display: inline-block;
  width: 3px;
  height: 11px;
  margin-right: 6px;
  vertical-align: -1px;
  background: #409eff;
  border-radius: 2px;
}
.block-text {
  font-size: 12px;
  line-height: 1.75;
  color: #303133;
  word-break: break-word;
  /* 实测 steps/expected_result 是「1. xxx\n2. xxx」纯文本，不是 HTML；
     不加 pre-wrap 会被折叠成一行。富文本紧凑 HTML 下 pre-wrap 也不影响渲染。 */
  white-space: pre-wrap;
}
/* v-html 内容不受 scoped 约束，需 deep 才能收紧默认段落间距 */
.card-body :deep(.block-text p) {
  margin: 0 0 4px;
}
.card-body :deep(.block-text p:last-child) {
  margin-bottom: 0;
}
.card-body :deep(.block-text ol),
.card-body :deep(.block-text ul) {
  margin: 2px 0;
  padding-left: 20px;
}
.card-body :deep(.block-text li) {
  margin-bottom: 2px;
}
.card-body :deep(.block-text table) {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.card-body :deep(.block-text th),
.card-body :deep(.block-text td) {
  border: 1px solid #ebeef5;
  padding: 3px 6px;
}
.card-type {
  font-size: 12px;
  color: #909399;
}
.card-muted {
  font-size: 12px;
  color: #b1b3b8;
}

.card-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 16px;
  padding-top: 2px;
}
.card-field {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
  line-height: 1.6;
}
.card-field b {
  flex: none;
  width: 40px;
  color: #909399;
  font-weight: 400;
}
.card-field em {
  min-width: 0;
  color: #303133;
  font-style: normal;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-field .mini-tag {
  margin-right: 4px;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding-top: 8px;
  border-top: 1px dashed #ebeef5;
}
.card-sub {
  font-size: 12px;
  color: #909399;
}
.card-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.card-actions .el-button {
  margin-left: 0;
}

.filter-bar {
  margin-bottom: 20px;
  padding: 16px 20px;
  background: var(--el-fill-color-lighter, #f5f7fa);
  border-radius: 8px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
}

.filter-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  
  & + .filter-row {
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px dashed var(--el-border-color-light, #e4e7ed);
  }
}

.filter-item {
  flex: 0 0 auto;
  min-width: 110px;
  
  &--wide {
    flex: 1 1 220px;
    min-width: 200px;
    max-width: 340px;
  }
  
  &--module {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 180px;
    
    .module-select {
      flex: 1;
      min-width: 140px;
    }
  }
}

.quick-add-btn {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  font-size: 14px;
  border: 1px dashed var(--el-border-color, #dcdfe6);
  color: var(--el-color-primary);
  background: transparent;
  transition: all 0.2s;
  
  &:hover:not(:disabled) {
    background: var(--el-color-primary-light-9);
    border-color: var(--el-color-primary);
    border-style: solid;
  }
  
  &:disabled {
    border-color: var(--el-border-color-lighter, #ebeef5);
    color: var(--el-text-color-placeholder);
    cursor: not-allowed;
  }
}

.cascade-arrow {
  flex-shrink: 0;
  width: 20px;
  text-align: center;
  color: var(--el-color-primary-light-3);
  font-size: 18px;
  font-weight: bold;
  user-select: none;
}

.filter-hint {
  font-size: 12px;
  color: var(--el-text-color-placeholder, #c0c4cc);
  white-space: nowrap;
  margin-left: 4px;
}

.filter-spacer {
  flex: 1;
  min-width: 20px;
}

.filter-actions {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.pagination-container {
  margin-top: 20px;
  display: flex;
  justify-content: center;
}

.header-actions {
  display: flex;
  gap: 10px;
}

.priority-tag {
  &.low { color: #67c23a; }
  &.medium { color: #e6a23c; }
  &.high { color: #f56c6c; }
  &.critical { color: #f56c6c; font-weight: bold; }
}

.version-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  
  .version-tag {
    margin: 0;
  }
}

.no-version {
  color: #909399;
  font-size: 12px;
  font-style: italic;
}

:deep(.el-table .cell) {
  white-space: nowrap;
}
</style>
