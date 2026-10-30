<template>
  <div class="test-case-manager">
    <div class="page-header">
      <h1 class="page-title">{{ isAppModule ? '真机录制' : '测试用例管理' }}</h1>
      <div class="header-actions">
        <el-select v-model="projectId" placeholder="选择项目" style="width: 200px; margin-right: 15px" @change="onProjectChange">
          <el-option-group label="UI自动化项目">
            <el-option v-for="project in uiProjects" :key="project.id" :label="project.name" :value="project.id" />
          </el-option-group>
          <el-option-group label="AI用例项目">
            <el-option v-for="project in aiProjects" :key="project.id" :label="project.name" :value="project.id" />
          </el-option-group>
        </el-select>
        <el-button type="primary" @click="showCreateDialog = true">
          <el-icon><Plus /></el-icon>
          新建测试用例
        </el-button>
        <el-button v-if="isAppModule" @click="openImportDialog">
          <el-icon><Upload /></el-icon>
          导入 Airtest 脚本
        </el-button>
      </div>
    </div>

    <div class="main-content">
      <!-- 左侧：测试用例列表 -->
      <div class="left-panel">
        <div class="panel-header">
          <h3>测试用例列表</h3>
          <el-input
            v-model="searchKeyword"
            placeholder="搜索测试用例..."
            clearable
            size="small"
            style="width: 200px"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </div>

        <div class="test-case-list">
          <div
            v-for="testCase in filteredTestCases"
            :key="testCase.id"
            class="test-case-item"
            :class="{ active: selectedTestCase?.id === testCase.id }"
            @click="selectTestCase(testCase)"
          >
            <div class="case-header">
              <div class="case-info">
                <h4 class="case-name">{{ testCase.name }}</h4>
                <p class="case-description">{{ testCase.description || '暂无描述' }}</p>
              </div>
              <div class="case-actions">
                <el-button size="small" text @click.stop="runTestCase(testCase)">
                  <el-icon><CaretRight /></el-icon>
                </el-button>
                <el-button size="small" text @click.stop="editTestCase(testCase)">
                  <el-icon><Edit /></el-icon>
                </el-button>
                <el-button size="small" text @click.stop="copyTestCase(testCase)">
                  <el-icon><CopyDocument /></el-icon>
                </el-button>
                <el-button size="small" text type="danger" @click.stop="deleteTestCase(testCase)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>
            <div class="case-meta">
              <!-- 移除状态显示 -->
              <span class="step-count">{{ testCase.steps?.length || 0 }} 步骤</span>
              <span class="update-time">{{ formatTime(testCase.updated_at) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 右侧：测试用例详情和步骤编辑 -->
      <div class="right-panel">
        <div v-if="selectedTestCase" class="test-case-detail">
          <div class="detail-header">
            <h3>{{ selectedTestCase.name }}</h3>
            <div class="detail-actions">
              <el-button size="small" @click="addStep">
                <el-icon><Plus /></el-icon>
                添加步骤
              </el-button>
              <el-button size="small" type="primary" @click="saveTestCase">
                <el-icon><Check /></el-icon>
                保存
              </el-button>
              <!-- 继续录制：对已有用例追加新步骤 -->
              <el-button
                v-if="(selectedEngine === 'appium' || selectedEngine === 'airtest') && selectedTestCase"
                size="small"
                type="warning"
                @click="openContinueRecording"
              >
                <el-icon><VideoCamera /></el-icon>
                继续录制
              </el-button>
              <el-select v-model="selectedEngine" placeholder="选择引擎" size="small" style="width: 130px; margin-right: 10px">
                <!-- App自动化模块只显示 Appium / Airtest；Web 模块显示全部 -->
                <template v-if="isAppModule">
                  <el-option label="Appium" value="appium" />
                  <el-option label="Airtest" value="airtest" />
                </template>
                <template v-else>
                  <el-option label="Playwright" value="playwright" />
                  <el-option label="Midscene Web (AI)" value="midscene_web" />
                  <el-option label="Selenium" value="selenium" />
                  <el-option label="Appium" value="appium" />
                  <el-option label="Airtest" value="airtest" />
                </template>
              </el-select>
              <!-- Web 引擎选项（仅 Playwright/Selenium 需要浏览器和运行模式） -->
              <template v-if="selectedEngine === 'playwright' || selectedEngine === 'selenium' || selectedEngine === 'midscene_web'">
                <el-select v-model="selectedBrowser" placeholder="选择浏览器" size="small" style="width: 120px; margin-right: 10px">
                  <el-option label="Chrome" value="chrome" />
                  <el-option label="Firefox" value="firefox" />
                  <el-option label="Safari" value="safari" />
                  <el-option label="Edge" value="edge" />
                </el-select>
                <el-select v-model="headlessMode" placeholder="运行模式" size="small" style="width: 110px; margin-right: 10px">
                  <el-option label="有头模式" :value="false" />
                  <el-option label="无头模式" :value="true" />
                </el-select>
              </template>
              <!-- Appium / Airtest 选项（都需要设备+应用） -->
              <template v-if="selectedEngine === 'appium' || selectedEngine === 'airtest'">
                <el-radio-group v-model="recordingEngine" size="small" style="margin-right: 10px" v-if="selectedEngine === 'appium'">
                  <el-radio-button label="appium">真机 (Appium)</el-radio-button>
                  <el-radio-button label="airtest">纯 Airtest</el-radio-button>
                </el-radio-group>
                <el-select v-model="selectedDeviceId" placeholder="选择设备" size="small" style="width: 150px; margin-right: 10px" filterable>
                  <el-option
                    v-for="d in appDevices"
                    :key="d.device_id || d.id"
                    :label="`${d.name} (${d.platform})`"
                    :value="d.device_id"
                    :disabled="d.status !== 'online'"
                  />
                </el-select>
                <el-select v-model="selectedAppConfigId" placeholder="选择应用" size="small" style="width: 140px; margin-right: 10px" filterable>
                  <el-option
                    v-for="a in appConfigs"
                    :key="a.id"
                    :label="`${a.name}`"
                    :value="a.id"
                  />
                </el-select>
                <el-button size="small" type="warning" @click="showDumpDialog = true">
                  <el-icon><View /></el-icon>
                  探测页面
                </el-button>
                <el-button size="small" type="danger" :loading="isRecordingBusy" @click="openRecording">
                  <el-icon v-if="!isRecordingBusy"><VideoCamera /></el-icon>
                  {{ isRecordingBusy ? '连接设备中...' : '开始录制' }}
                </el-button>
              </template>
              <!-- 执行中：按钮直接变成「停止执行」，避免用户只能干等 -->
              <el-button
                v-if="isRunning"
                size="small"
                type="danger"
                :loading="aborting"
                :disabled="!runningExecutionId"
                @click="stopExecution"
              >
                <el-icon v-if="!aborting"><CircleClose /></el-icon>
                {{ aborting ? '正在停止...' : '停止执行' }}
              </el-button>
              <el-button v-else size="small" type="success" @click="runTestCase(selectedTestCase)">
                <el-icon><CaretRight /></el-icon>
                运行
              </el-button>
              <el-button size="small" v-if="executionResult" @click="toggleView">
                <el-icon><component :is="showSteps ? 'View' : 'Edit'" /></el-icon>
                {{ showSteps ? '查看执行结果' : '编辑步骤' }}
              </el-button>
              <el-button
                size="small"
                v-if="executionResult && !showSteps"
                type="success"
                @click="runTestCase(selectedTestCase)"
                :loading="isRunning"
              >
                <el-icon v-if="!isRunning"><Refresh /></el-icon>
                重新运行
              </el-button>
            </div>
          </div>

          <!-- 测试步骤编辑 -->
          <div class="steps-container" v-show="showSteps">
            <div class="steps-header">
              <h4>测试步骤</h4>
              <el-button size="small" text @click="expandAllSteps">
                {{ allStepsExpanded ? '折叠全部' : '展开全部' }}
              </el-button>
            </div>

            <div class="steps-scroll-container">
              <div class="steps-list">
                <draggable
                  v-model="currentSteps"
                  item-key="id"
                  handle=".drag-handle"
                  @change="onStepsReorder"
                >
                  <template #item="{ element, index }">
                    <div class="step-item" :class="{ expanded: element.expanded }">
                      <div class="step-header">
                        <div class="step-left">
                          <el-icon class="drag-handle"><Rank /></el-icon>
                          <span class="step-number">{{ index + 1 }}</span>
                          <el-select
                            v-model="element.action_type"
                            placeholder="选择操作"
                            size="small"
                            style="width: 120px"
                            @change="onActionTypeChange(element)"
                          >
                            <el-option label="点击" value="click" />
                            <el-option label="轻触" value="tap" />
                            <el-option label="双击" value="double_tap" />
                            <el-option label="长按" value="long_press" />
                            <el-option label="输入文本" value="fill" />
                            <el-option label="输入" value="input" />
                            <el-option label="清空" value="clear" />
                            <el-option label="获取文本" value="getText" />
                            <el-option label="等待元素" value="waitFor" />
                            <el-option label="悬停" value="hover" />
                            <el-option label="滚动" value="scroll" />
                            <el-option label="滑动" value="swipe" />
                            <el-option label="截图" value="screenshot" />
                            <el-option label="断言" value="assert" />
                            <el-option label="等待" value="wait" />
                            <el-option label="切换标签页" value="switchTab" />
                            <el-option label="启动应用" value="launch_app" />
                            <el-option label="关闭应用" value="close_app" />
                            <el-option label="返回" value="back" />
                            <el-option label="主页" value="home" />
                            <el-option-group label="Midscene AI">
                              <el-option label="AI操作" value="aiAct" />
                              <el-option label="AI点击" value="aiTap" />
                              <el-option label="AI输入" value="aiInput" />
                              <el-option label="AI查询" value="aiQuery" />
                              <el-option label="AI断言" value="aiAssert" />
                              <el-option label="导航" value="navigate" />
                            </el-option-group>
                          </el-select>
                          <el-select
                            v-if="needsElement(element.action_type)"
                            v-model="element.element_id"
                            placeholder="选择元素"
                            size="small"
                            style="width: 200px"
                            filterable
                            @change="onElementChange(element)"
                          >
                            <el-option
                              v-for="elem in availableElements"
                              :key="elem.id"
                              :label="`${elem.name} (${elem.locator_value})`"
                              :value="elem.id"
                            />
                          </el-select>
                        </div>
                        <div class="step-right">
                          <el-button
                            size="small"
                            text
                            @click="element.expanded = !element.expanded"
                          >
                            <el-icon>
                              <component :is="element.expanded ? 'ArrowUp' : 'ArrowDown'" />
                            </el-icon>
                          </el-button>
                          <el-button size="small" text type="danger" @click="removeStep(index)">
                            <el-icon><Delete /></el-icon>
                          </el-button>
                        </div>
                      </div>

                      <div v-if="element.expanded" class="step-content">
                        <!-- 输入参数 -->
                        <div v-if="needsInputValue(element.action_type)" class="step-param">
                          <label>输入值：</label>
                          <div style="display: flex; gap: 5px; flex: 1">
                            <el-input
                              v-model="element.input_value"
                              :placeholder="element.action_type === 'switchTab' ? '输入索引(0,1...)或留空切换到最新' : '请输入内容，支持变量如 ${random_phone()}'"
                              size="small"
                            />
                            <el-tooltip content="插入动态变量" placement="top" v-if="element.action_type !== 'switchTab'">
                              <el-button size="small" @click="openVariableHelper(element, 'input_value')">
                                <el-icon><MagicStick /></el-icon>
                              </el-button>
                            </el-tooltip>
                          </div>
                        </div>

                        <!-- 等待时间 -->
                        <div v-if="needsWaitTime(element.action_type)" class="step-param">
                          <label>等待时间（毫秒）：</label>
                          <el-input-number
                            v-model="element.wait_time"
                            :min="100"
                            :max="30000"
                            :step="100"
                            size="small"
                          />
                        </div>

                        <!-- 断言参数 -->
                        <div v-if="element.action_type === 'assert'" class="step-param">
                          <label>断言类型：</label>
                          <el-select v-model="element.assert_type" size="small" style="width: 150px">
                            <el-option label="文本包含" value="textContains" />
                            <el-option label="文本等于" value="textEquals" />
                            <el-option label="元素可见" value="isVisible" />
                            <el-option label="元素存在" value="exists" />
                            <el-option label="属性值" value="hasAttribute" />
                          </el-select>
                          <div style="display: flex; align-items: center; margin-left: 10px; width: 240px">
                            <el-input
                              v-model="element.assert_value"
                              placeholder="期望值"
                              size="small"
                              style="flex: 1"
                            />
                            <el-tooltip content="插入动态变量" placement="top">
                              <el-button size="small" style="margin-left: 5px" @click="openVariableHelper(element, 'assert_value')">
                                <el-icon><MagicStick /></el-icon>
                              </el-button>
                            </el-tooltip>
                          </div>
                        </div>

                        <!-- 步骤描述 -->
                        <div class="step-param">
                          <label>步骤描述：</label>
                          <el-input
                            v-model="element.description"
                            placeholder="描述这个步骤的作用"
                            size="small"
                          />
                        </div>
                      </div>
                    </div>
                  </template>
                </draggable>
              </div>
            </div>
          </div>

          <!-- 执行记录历史 -->
          <div v-if="selectedTestCase" class="execution-history">
            <div class="history-header" @click="historyCollapsed = !historyCollapsed">
              <el-icon><Clock /></el-icon>
              <span class="history-title">执行记录</span>
              <el-tag v-if="executionHistory.length" size="small" type="info" round>{{ executionHistory.length }}</el-tag>
              <span class="history-spacer" />
              <el-icon class="collapse-icon" :class="{ 'is-collapsed': historyCollapsed }"><ArrowDown /></el-icon>
            </div>
            <div v-show="!historyCollapsed" class="history-body">
              <div v-if="historyLoading" class="history-loading">
                <el-icon class="is-loading"><Loading /></el-icon> 加载中...
              </div>
              <el-empty v-else-if="executionHistory.length === 0" description="暂无执行记录" :image-size="60" />
              <div v-else class="history-list">
                <div
                  v-for="rec in executionHistory"
                  :key="rec.id"
                  class="history-item"
                  :class="{ active: selectedHistoryId === rec.id }"
                  @click="viewExecution(rec)"
                >
                  <el-tag :type="getStatusTag(rec.status)" size="small">{{ getStatusText(rec.status) }}</el-tag>
                  <span class="history-engine">{{ rec.engine || '-' }}</span>
                  <span class="history-time">{{ formatTime(rec.created_at) }}</span>
                  <span class="history-duration" v-if="rec.execution_time">· {{ rec.execution_time }}s</span>
                  <span class="history-creator" v-if="rec.created_by_name">· {{ rec.created_by_name }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 执行结果 -->
          <div v-if="executionResult" class="execution-result" v-show="!showSteps">
            <div class="result-header">
              <h4>执行结果</h4>
              <el-tag :type="resultTagType">
                {{ resultTagText }}
              </el-tag>
              <span v-if="!executionResult.success && failedStepCount > 0" class="failed-step-hint">
                失败于第 {{ failedStepCount }} 步
              </span>
            </div>
            <div class="result-content">
              <!-- 全局错误信息（连接失败等无法归因到单步的错误） -->
              <div v-if="executionResult.error_message && stepResultList.length === 0 && parsedExecutionLogs.length === 0"
                   class="global-error-banner">
                <el-alert :type="resultTagType" :title="'错误信息'" :closable="false" show-icon>
                  <pre class="error-message">{{ executionResult.error_message }}</pre>
                </el-alert>
              </div>
              <el-tabs v-model="resultActiveTab">
                <el-tab-pane label="步骤结果" name="steps">
                  <div class="logs-container">
                    <div v-if="stepResultList.length > 0">
                      <div
                        v-for="(step, index) in stepResultList"
                        :key="index"
                        class="log-item"
                        :class="{ 'log-item-failed': step.success === false }"
                      >
                        <div class="log-header">
                          <el-tag :type="step.success ? 'success' : 'danger'" size="small">
                            步骤 {{ step.step_number }}
                          </el-tag>
                          <span class="log-action">{{ getActionText(step.action_type) }}</span>
                          <span class="log-desc">{{ step.description }}</span>
                          <!-- 每步截图缩略图 -->
                          <div v-if="step.screenshot" class="step-screenshot-thumb" @click.stop>
                            <el-image
                              :src="step.screenshot"
                              fit="cover"
                              :preview-src-list="getStepScreenshotList()"
                              :initial-index="getStepScreenshotIndex(index)"
                              :hide-on-click-modal="true"
                            />
                          </div>
                        </div>
                        <div v-if="step.error" class="log-error">
                          <el-icon><WarningFilled /></el-icon>
                          <pre class="error-message">{{ step.error }}</pre>
                        </div>
                      </div>
                    </div>
                    <el-empty v-else description="暂无步骤结果" />
                  </div>
                </el-tab-pane>
                <el-tab-pane label="执行日志" name="logs">
                  <div class="logs-container">
                    <div v-if="parsedExecutionLogs.length > 0">
                      <div v-for="(step, index) in parsedExecutionLogs" :key="index" class="log-item">
                        <div class="log-header">
                          <el-tag :type="step.success ? 'success' : 'danger'" size="small">
                            步骤 {{ step.step_number }}
                          </el-tag>
                          <span class="log-action">{{ getActionText(step.action_type) }}</span>
                          <span class="log-desc">{{ step.description }}</span>
                        </div>
                        <div v-if="step.error" class="log-error">
                          <el-icon><WarningFilled /></el-icon>
                          <pre class="error-message">{{ step.error }}</pre>
                        </div>
                      </div>
                    </div>
                    <el-empty v-else description="暂无执行日志" />
                  </div>
                </el-tab-pane>
                <el-tab-pane label="失败截图" name="screenshots" v-if="executionResult.screenshots && executionResult.screenshots.length > 0">
                  <div class="screenshots-container">
                    <div
                      v-for="(screenshot, index) in executionResult.screenshots"
                      :key="index"
                      class="screenshot-item"
                      @click="previewScreenshot(screenshot)"
                    >
                      <div class="screenshot-wrapper">
                        <img
                          :src="screenshot.url"
                          :alt="`截图 ${index + 1}`"
                          :data-index="index"
                          @error="handleImageError"
                          @load="handleImageLoad"
                        />
                        <div class="screenshot-placeholder" v-if="!screenshot.loaded">
                          <el-icon><Picture /></el-icon>
                          <span>加载中...</span>
                        </div>
                        <div class="screenshot-error" v-if="screenshot.error">
                          <el-icon><Warning /></el-icon>
                          <span>图片加载失败</span>
                        </div>
                        <div class="screenshot-overlay">
                          <el-icon class="zoom-icon"><ZoomIn /></el-icon>
                        </div>
                      </div>
                      <div class="screenshot-info">
                        <p class="screenshot-description">{{ screenshot.description || `截图 ${index + 1}` }}</p>
                        <p class="screenshot-meta" v-if="screenshot.step_number">步骤 {{ screenshot.step_number }}</p>
                        <p class="screenshot-time" v-if="screenshot.timestamp">{{ formatTime(screenshot.timestamp) }}</p>
                      </div>
                    </div>
                  </div>
                </el-tab-pane>
                <el-tab-pane label="错误信息" name="errors" v-if="executionResult.errors && executionResult.errors.length > 0">
                  <div class="errors-container">
                    <div
                      v-for="(error, index) in executionResult.errors"
                      :key="index"
                      class="error-item"
                    >
                      <div class="error-header">
                        <el-tag type="danger" size="large">
                          <el-icon><WarningFilled /></el-icon>
                          {{ error.message || error }}
                        </el-tag>
                        <span v-if="error.step_number" class="error-step">
                          步骤 {{ error.step_number }}
                        </span>
                      </div>

                      <div v-if="error.action_type || error.element || error.description" class="error-meta">
                        <div v-if="error.action_type" class="meta-item">
                          <span class="meta-label">操作类型:</span>
                          <span class="meta-value">{{ error.action_type }}</span>
                        </div>
                        <div v-if="error.element" class="meta-item">
                          <span class="meta-label">目标元素:</span>
                          <span class="meta-value">{{ error.element }}</span>
                        </div>
                        <div v-if="error.description" class="meta-item">
                          <span class="meta-label">步骤描述:</span>
                          <span class="meta-value">{{ error.description }}</span>
                        </div>
                      </div>

                      <div v-if="error.details || error.stack" class="error-details">
                        <div class="details-header">详细错误信息:</div>
                        <pre class="details-content">{{ error.details || error.stack }}</pre>
                      </div>
                    </div>
                  </div>
                </el-tab-pane>
              </el-tabs>
            </div>
          </div>
        </div>

        <div v-else class="no-selection">
          <el-empty description="请选择一个测试用例" />
        </div>
      </div>
    </div>

    <!-- 新建/编辑测试用例对话框 -->
    <el-dialog
      v-model="showCreateDialog"
      :title="editingTestCase ? '编辑测试用例' : '新建测试用例'"
      :close-on-click-modal="false"
      width="500px"
    >
      <el-form :model="testCaseForm" label-width="100px">
        <el-form-item label="用例名称" required>
          <el-input v-model="testCaseForm.name" placeholder="请输入测试用例名称" />
        </el-form-item>
        <el-form-item label="用例描述">
          <el-input
            v-model="testCaseForm.description"
            type="textarea"
            :rows="3"
            placeholder="请输入测试用例描述"
          />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="testCaseForm.priority" style="width: 100%">
            <el-option label="高" value="high" />
            <el-option label="中" value="medium" />
            <el-option label="低" value="low" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <span class="dialog-footer">
          <el-button @click="showCreateDialog = false">取消</el-button>
          <el-button type="primary" @click="saveTestCaseForm">确定</el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 导入 Airtest / Poco 脚本对话框 -->
    <el-dialog
      v-model="showImportDialog"
      title="📥 导入 Airtest / Poco 脚本"
      width="820px"
      :close-on-click-modal="false"
      @close="closeImportDialog"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 16px"
        title="把 Airtest IDE 录制导出的 .py / .air 脚本粘贴进来（或上传文件），系统会自动解析为统一的测试用例步骤并入库。"
      />
      <el-form label-width="110px">
        <el-form-item label="目标项目" prop="project">
          <el-select
            v-model="importProjectId"
            placeholder="选择导入目标项目"
            filterable
            clearable
            style="width: 100%"
            :disabled="!allProjects || allProjects.length === 0"
            @change="onImportProjectChange"
          >
            <el-option
              v-for="p in allProjects"
              :key="p.id"
              :label="p.name"
              :value="p.id"
            />
            <template #empty>
              <div style="padding: 10px; text-align: center; color: #909399;">
                <p>暂无可用项目</p>
                <p style="font-size: 12px; margin-top: 4px;">请先在 AI 生成用例模块创建项目</p>
              </div>
            </template>
          </el-select>
        </el-form-item>
        <el-form-item label="用例名称">
          <el-input v-model="importForm.case_name" placeholder="留空则自动命名（Airtest导入_时间戳）" />
        </el-form-item>
        <el-form-item label="应用包名">
          <el-input v-model="importForm.app_package" placeholder="可选，如 com.example.app，用于「启动应用」步骤描述" />
        </el-form-item>
        <el-form-item label="脚本内容">
          <div style="width: 100%">
            <div style="margin-bottom: 8px">
              <el-upload
                :auto-upload="false"
                :show-file-list="false"
                accept=".py,.air"
                :on-change="onAirtestFileChange"
              >
                <el-button size="small">
                  <el-icon><Upload /></el-icon>
                  上传 .air / .py 文件
                </el-button>
              </el-upload>
              <span style="margin-left: 10px; font-size: 12px; color: #909399">
                支持 poco.click / set_text / swipe、touch(Template/坐标)、text()、keyevent、assert_exists、snapshot 等
              </span>
            </div>
            <el-input
              v-model="importForm.script"
              type="textarea"
              :rows="12"
              placeholder="在此粘贴 Airtest / Poco 录制脚本，例如：&#10;poco('com.example:id/btn').click()&#10;touch(Template(r'xxx.png'))&#10;swipe((500,1000),(500,200))"
            />
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="showImportDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="isImporting"
          :disabled="!realProjectId || !importForm.script.trim()"
          @click="submitImport"
        >
          解析并生成用例
        </el-button>
      </template>
    </el-dialog>

    <!-- 截图预览对话框 -->
    <el-dialog
      v-model="showScreenshotPreview"
      title="失败截图预览"
      width="80%"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :modal="true"
      :destroy-on-close="false"
    >
      <div v-if="currentScreenshot" class="screenshot-preview">
        <div class="preview-info">
          <h4>{{ currentScreenshot.description }}</h4>
          <p v-if="currentScreenshot.step_number">失败步骤: 步骤 {{ currentScreenshot.step_number }}</p>
          <p v-if="currentScreenshot.timestamp">截图时间: {{ formatTime(currentScreenshot.timestamp) }}</p>
        </div>
        <div class="preview-image">
          <img :src="currentScreenshot.url" :alt="currentScreenshot.description" />
        </div>
      </div>
    </el-dialog>

    <!-- 变量助手对话框 -->
    <el-dialog
      :close-on-press-escape="false"
      :modal="true"
      :destroy-on-close="false"
      v-model="showVariableHelper"
      title="变量助手 (点击插入)"
      :close-on-click-modal="false"
      width="800px"
    >
      <el-tabs tab-position="left" style="height: 400px">
        <el-tab-pane
          v-for="(category, index) in variableCategories"
          :key="index"
          :label="category.label"
        >
          <div style="height: 400px; overflow-y: auto">
            <el-table :data="category.variables" style="width: 100%" @row-click="insertVariable" highlight-current-row cursor="pointer">
              <el-table-column prop="name" label="函数名" width="150">
                <template #default="{ row }">
                  <el-tag size="small">{{ row.name }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="desc" label="描述" width="150" />
              <el-table-column prop="syntax" label="语法" show-overflow-tooltip />
              <el-table-column label="操作" width="80">
                <template #default="{ row }">
                  <el-button link type="primary" size="small">插入</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>

    <!-- 页面元素探测弹窗 -->
    <el-dialog
      v-model="showDumpDialog"
      title="📱 页面元素探测"
      width="800px"
      :close-on-click-modal="false"
      @opened="doDumpPage"
    >
      <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 16px">
        <el-button type="primary" @click="doDumpPage" :loading="isDumping">
          <el-icon><View /></el-icon>
          {{ isDumping ? '探测中...' : '重新探测' }}
        </el-button>
        <el-tag v-if="dumpedElements.length > 0" type="success">发现 {{ dumpedElements.length }} 个控件</el-tag>
        <span v-if="selectedDeviceId && selectedAppConfigId" style="font-size: 13px; color: #909399">
          设备: {{ getDeviceLabel() }} | 应用: {{ getAppConfigLabel() }}
        </span>
      </div>
      <el-empty v-if="dumpedElements.length === 0 && !isDumping" description="点击下方按钮探测当前页面元素" />
      <el-table v-if="dumpedElements.length > 0" :data="dumpedElements" max-height="400" stripe border>
        <el-table-column prop="chinese_name" label="元素名称" min-width="120" show-overflow-tooltip />
        <el-table-column label="策略" width="70">
          <template #default="{ row }">
            <el-tag :type="row.strategy === 'id' ? '' : 'warning'" size="small">{{ row.strategy === 'id' ? 'ID' : 'XPath' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="value" label="定位值" min-width="200" show-overflow-tooltip />
        <el-table-column prop="text" label="文本" min-width="100" show-overflow-tooltip />
        <el-table-column prop="class_name" label="类型" width="140" show-overflow-tooltip />
        <el-table-column prop="clickable" label="可点击" width="80">
          <template #default="{ row }">
            <el-tag :type="row.clickable ? 'success' : 'info'" size="small">{{ row.clickable ? '是' : '否' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" @click="addDumpedElementAsStep(row)">
              添加步骤
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 操作录制弹窗 -->
    <el-dialog
      v-model="showRecordDialog"
      title="🎬 操作录制"
      width="1340px"
      :close-on-click-modal="false"
      @close="onRecordDialogClose"
    >
      <div v-if="recording" class="recording-banner">
        <span class="rec-dot" />
        <template v-if="recordAutoCapture">
          录制中… 直接在真机上操作即可自动记录（输入框打字等）；也可在左侧镜像上点击控件即时记录
        </template>
        <template v-else>
          录制中… 在左侧手机镜像上点击控件即可自动记录每一步（点选 + 选择操作类型）
        </template>
        <el-tag v-if="recordAutoCapture" size="small" type="success" style="margin-left: 8px">自动捕获</el-tag>
        <el-tag size="small" type="info" style="margin-left: 8px">{{ recordCaseName }}</el-tag>
      </div>
      <div v-else-if="recordedSteps.length" class="recording-banner stopped">
        ✅ 已停止实时录制，请确认下方步骤后点击「生成用例」
      </div>

      <div class="recording-body">
        <!-- 左：实时元素 -->
        <div class="rec-elements">
          <div class="rec-panel-title">
            实时页面元素
            <el-button size="small" text type="primary" :loading="isRecordingBusy" @click="refreshRecordPage">
              重新探测
            </el-button>
            <el-tag v-if="recordedElements.length" type="success" size="small">
              共 {{ recordedElements.length }}（显示 {{ filteredRecordElements.length }}）
            </el-tag>
          </div>
          <div class="rec-screenshot">
            <div v-if="recordScreenshot">
              <div class="rec-screenshot-tip" v-if="!recording">👆 在截图上点选目标控件，自动选中对应元素</div>
              <div class="rec-screenshot-tip rec-auto" v-else>
                👆 录制中：点击镜像上的控件即自动记录（当前：{{ recOpModeLabel }}）。也可点下方按钮做全局操作。
              </div>
              <img :src="'data:image/png;base64,' + recordScreenshot" class="rec-screenshot-img" @click="onScreenshotClick" />
            </div>
            <div v-else class="rec-screenshot-empty">
              <el-icon><Picture /></el-icon>
              <span>手机镜像截图中…</span>
              <span class="rec-screenshot-empty-hint">若长时间未显示，请点击「重新探测」</span>
            </div>
          </div>
          <div class="rec-element-filter">
            <el-input
              v-model="recordElementFilter"
              size="small"
              placeholder="搜索名称 / 文本 / 定位值"
              clearable
              style="flex: 1"
            />
            <el-checkbox v-model="recordOnlyClickable" size="small" style="margin-left: 8px">
              仅可点击
            </el-checkbox>
          </div>
          <div class="rec-element-list">
            <div
              v-for="(el, i) in filteredRecordElements"
              :key="i"
              class="rec-element-item"
              :class="{ active: selectedRecordElement && selectedRecordElement.value === el.value }"
              @click="selectedRecordElement = el"
            >
              <span class="rec-el-name">{{ el.chinese_name || el.text }}</span>
              <el-tag :type="el.strategy === 'id' ? '' : 'warning'" size="small">
                {{ el.strategy === 'id' ? 'ID' : 'XPath' }}
              </el-tag>
              <span v-if="!el.clickable" class="rec-el-noclick" title="该元素不可点击">不可点</span>
              <span class="rec-el-val">{{ el.value }}</span>
            </div>
            <el-empty v-if="!filteredRecordElements.length" :description="recordedElements.length ? '无匹配元素' : '暂无元素，点重新探测'" :image-size="60" />
          </div>
        </div>

        <!-- 右：操作 + 步骤 -->
        <div class="rec-right">
          <div class="rec-panel-title">
            记录操作
            <el-radio-group v-if="recording" v-model="recOpMode" size="small" style="margin-left: 8px">
              <el-radio-button label="click">点击</el-radio-button>
              <el-radio-button label="long_press">长按</el-radio-button>
              <el-radio-button label="input">输入</el-radio-button>
              <el-radio-button label="swipe">滑动</el-radio-button>
            </el-radio-group>
          </div>
          <div class="rec-actions" v-if="!recording">
            <el-button size="small" type="primary" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordClick">
              记录点击
            </el-button>
            <el-button size="small" type="primary" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordInput">
              记录输入
            </el-button>
            <el-button size="small" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordLongPress">
              记录长按
            </el-button>
            <el-button size="small" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordAssert">
              记录断言
            </el-button>
          </div>
          <div class="rec-actions" v-if="recording && recOpMode === 'swipe'">
            <span class="rec-sub">滑动(先点选一个元素，再点方向)：</span>
            <el-button size="small" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordSwipe('up')">上</el-button>
            <el-button size="small" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordSwipe('down')">下</el-button>
            <el-button size="small" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordSwipe('left')">左</el-button>
            <el-button size="small" :disabled="!selectedRecordElement" :loading="isRecordingBusy" @click="recordSwipe('right')">右</el-button>
          </div>
          <div class="rec-actions" v-if="recording && recOpMode === 'swipe'">
            <span class="rec-sub">{{ recSwipeStart ? '再点终点控件完成滑动' : '先点起点控件' }}</span>
          </div>
          <div class="rec-actions">
            <span class="rec-sub">全局：</span>
            <el-button size="small" :loading="isRecordingBusy" @click="recordBack">返回</el-button>
            <el-button size="small" :loading="isRecordingBusy" @click="recordHome">主页</el-button>
            <el-button size="small" :loading="isRecordingBusy" @click="recordScreenshotAction">截图</el-button>
            <el-button size="small" :loading="isRecordingBusy" @click="recordWait">等待</el-button>
          </div>

          <div class="rec-panel-title" style="margin-top: 12px">已记录步骤 ({{ recordedSteps.length }})</div>
          <div class="rec-steps">
            <div v-for="st in recordedSteps" :key="st.step_number" class="rec-step-item">
              <span class="rec-step-no">{{ st.step_number }}</span>
              <span class="rec-step-desc">{{ st.description }}</span>
              <el-tag v-if="st.element" size="small" type="info">{{ st.element.name }}</el-tag>
              <span v-if="st.input_value" class="rec-step-input">「{{ st.input_value }}」</span>
            </div>
            <el-empty v-if="!recordedSteps.length" description="还没有记录任何步骤" :image-size="60" />
          </div>
        </div>

        <!-- 右二：实时生成的 Airtest 代码（边录边看，纯 Airtest 模式显示） -->
        <div v-if="recordingEngine === 'airtest'" class="rec-code">
          <div class="rec-panel-title">
            实时生成的 Airtest 代码
            <el-tag v-if="recordingEngine === 'airtest'" size="small" type="success">纯 Airtest</el-tag>
            <el-tag v-else size="small" type="info">Appium 模式无代码</el-tag>
            <el-button size="small" text type="primary" :disabled="!recordScript" @click="copyRecordScript">
              复制
            </el-button>
          </div>
          <pre class="rec-code-pre">{{ recordScript || '# 选择「纯 Airtest」引擎并开始录制后，这里实时显示生成的 Airtest 脚本' }}</pre>
        </div>
      </div>

      <template #footer>
        <el-input v-model="recordCaseName" placeholder="用例名称" size="small" style="width: 240px; margin-right: 10px" />
        <el-button :loading="isRecordingBusy" type="primary" @click="generateCase">生成用例</el-button>
        <el-button v-if="recording" :loading="isRecordingBusy" @click="stopRecord">停止录制</el-button>
        <el-button v-else type="danger" text @click="discardRecord">放弃录制</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Search, Plus, Edit, Delete, Check, CaretRight, ArrowUp, ArrowDown, Rank, Picture, Warning, View, ZoomIn, Refresh, WarningFilled, MagicStick, VideoCamera, Upload, Clock, Loading, CircleClose
} from '@element-plus/icons-vue'
import draggable from 'vuedraggable'
import { useUnifiedProjects } from '@/utils/useUnifiedProjects'

import {
  getUiProjects,
  getElements,
  createElement,
  createTestCase,
  updateTestCase,
  deleteTestCase as deleteTestCaseApi,
  getTestCases,
  runTestCase as runTestCaseApi,
  getTestCaseExecution,
  getTestCaseExecutions,
  abortTestCaseExecution,
  copyTestCase as copyTestCaseApi,
  getLocatorStrategies,
  getAppDevices,
  getAppConfigs,
  dumpPageElements,
  startRecording,
  recordingPage,
  recordAction,
  stopRecording,
  generateRecordingCase,
  importAirtestScript
} from '@/api/ui_automation'

import { getDeviceList as getNewDeviceList } from '@/api/app-automation'

// 响应式数据
const { allProjects, uiProjects, aiProjects, loadProjects: loadAllProjects, resolveUiProjectId } = useUnifiedProjects()
const route = useRoute()
// 是否为 App 自动化模块：App 模块保留 Airtest 相关功能（纯 Airtest 录制 / 导入 Airtest 脚本），UI(Web) 模块隐藏
const isAppModule = computed(() => route.path.startsWith('/app-automation'))
const projectId = ref('') // 格式: "ui_1" 或 "proj_2"
const realProjectId = ref(null) // 实际 UiProject ID
const testCases = ref([])
const selectedTestCase = ref(null)
const currentSteps = ref([])
const availableElements = ref([])
const searchKeyword = ref('')
const showCreateDialog = ref(false)
const editingTestCase = ref(null)
const executionResult = ref(null)
const resultActiveTab = ref('logs')
const allStepsExpanded = ref(false)
const showSteps = ref(true)
const showScreenshotPreview = ref(false)
const currentScreenshot = ref(null)
const isRunning = ref(false)
// 停止执行：记录当前正在跑的 execution_id 与「已发过终止信号」状态
const runningExecutionId = ref(null)
const aborting = ref(false)
// 执行记录历史
const executionHistory = ref([])
const historyLoading = ref(false)
const selectedHistoryId = ref(null)
const historyCollapsed = ref(false)
const selectedEngine = ref('appium')  // 默认使用Appium（真机录制）
const recordingEngine = ref('appium')  // 录制引擎：appium（经 Appium Server）或 airtest（直连设备 ADB）
const selectedBrowser = ref('chrome')  // 默认使用Chrome
const headlessMode = ref(false)  // 默认使用有头模式
// Appium 相关
const selectedDeviceId = ref(null)
const selectedAppConfigId = ref(null)
const appDevices = ref([])
const appConfigs = ref([])
const showVariableHelper = ref(false)
const currentEditingStep = ref(null)
const currentEditingField = ref('')
// 页面探测
const showDumpDialog = ref(false)
const dumpedElements = ref([])
const isDumping = ref(false)

// 表单数据
const testCaseForm = reactive({
  name: '',
  description: '',
  priority: 'medium'
})

// 计算属性
const filteredTestCases = computed(() => {
  if (!searchKeyword.value) return testCases.value
  return testCases.value.filter(tc =>
    tc.name.includes(searchKeyword.value) ||
    tc.description?.includes(searchKeyword.value)
  )
})

// 解析执行日志
const parsedExecutionLogs = computed(() => {
  if (!executionResult.value || !executionResult.value.logs) return []
  try {
    return typeof executionResult.value.logs === 'string'
      ? JSON.parse(executionResult.value.logs)
      : executionResult.value.logs
  } catch (e) {
    console.error('解析执行日志失败:', e)
    return []
  }
})

// 优先使用结构化的步骤结果（后端 step_results，含每步成功/失败与错误），
// Appium 的文本日志无法被 JSON.parse 时也能正确展示"失败到第几步"
const stepResultList = computed(() => {
  const r = executionResult.value
  if (!r) return []
  if (Array.isArray(r.step_results) && r.step_results.length > 0) {
    return r.step_results
  }
  return parsedExecutionLogs.value
})

// 失败步骤序号（取第一个失败步骤），用于标题提示
const failedStepCount = computed(() => {
  const failed = stepResultList.value.find((s) => s.success === false)
  return failed ? failed.step_number : 0
})

// 每步截图预览辅助
const getStepScreenshotList = () => {
  return (executionResult.value?.step_results || [])
    .map(s => s.screenshot).filter(Boolean)
}
const getStepScreenshotIndex = (idx) => {
  let screenshotIdx = -1
  const steps = executionResult.value?.step_results || []
  for (let i = 0; i <= idx && i < steps.length; i++) {
    if (steps[i]?.screenshot) screenshotIdx++
  }
  return Math.max(0, screenshotIdx)
}

// 结果标题：根据真实状态展示 执行中/执行成功/执行失败
const resultTagType = computed(() => {
  const status = executionResult.value?.status
  if (status === 'passed') return 'success'
  if (status === 'running') return 'warning'
  if (status === 'failed' || status === 'error') return 'danger'
  return executionResult.value?.success ? 'success' : 'danger'
})
const resultTagText = computed(() => {
  const status = executionResult.value?.status
  if (status === 'running') return '执行中...'
  if (status === 'passed') return '执行成功'
  if (status === 'failed') return '执行失败'
  if (status === 'error') return '执行异常'
  return executionResult.value?.success ? '执行成功' : '执行失败'
})

// 方法定义
const loadProjects = async () => {
  await loadAllProjects()
}

const loadTestCases = async () => {
  if (!realProjectId.value) {
    testCases.value = []
    return
  }

  try {
    const response = await getTestCases({ project: realProjectId.value })
    testCases.value = response.data.results || response.data
  } catch (error) {
    console.error('获取测试用例失败:', error)
  }
}

const loadElements = async () => {
  if (!realProjectId.value) {
    availableElements.value = []
    return
  }

  try {
    // 必须传 page_size 避免默认分页导致新创建的 Element 不在首页
    const response = await getElements({ project: realProjectId.value, page_size: 999 })
    availableElements.value = response.data.results || response.data
  } catch (error) {
    console.error('获取元素列表失败:', error)
  }
}

const onProjectChange = async () => {
  selectedTestCase.value = null
  currentSteps.value = []
  executionResult.value = null

  realProjectId.value = await resolveUiProjectId(projectId.value)
  await loadAppDevicesAndConfigs()
  fillAppPackageFromProject()
  await Promise.all([
    loadTestCases(),
    loadElements()
  ]).catch((e) => {
    console.error('加载用例或元素失败:', e)
  })
}

// 根据当前选中项目在应用配置里的包名，自动填充导入 Airtest 脚本的「应用包名」
// 优先匹配 UiProject(project)，其次匹配 AI 项目(ai_project)；取第一个匹配的 AppConfig 包名
const fillAppPackageFromProject = () => {
  if (!realProjectId.value) return
  const projectIdNum = Number(realProjectId.value)
  const unified = projectId.value || ''
  // 找到绑定该项目的应用配置
  const matched = appConfigs.value.find(a => {
    if (unified.startsWith('proj_')) {
      return Number(a.ai_project) === projectIdNum
    }
    // ui_xxx 或纯数字：匹配 UiProject
    return Number(a.project) === projectIdNum
  })
  if (matched && matched.package_name) {
    importForm.app_package = matched.package_name
  }
}

const selectTestCase = async (testCase) => {
  // 如果点击的是同一个用例，不做任何处理
  if (selectedTestCase.value && selectedTestCase.value.id === testCase.id) {
    return
  }

  selectedTestCase.value = testCase
  // 先刷新元素列表（确保录制新建的 Element 已包含在内）
  await loadElements()
  // 确保步骤数据格式正确，添加前端需要的字段
  if (testCase.steps && testCase.steps.length > 0) {
    const existingIds = new Set(availableElements.value.map(e => e.id))
    currentSteps.value = testCase.steps.map(step => {
      let eid = ''
      if (step.element && typeof step.element === 'object') {
        eid = step.element.id || ''
        // 兜底：如果该元素不在 availableElements 中（分页/延迟等原因），
        // 将步骤自带的元素信息补入下拉选项
        if (eid && !existingIds.has(eid) && step.element.locator_value) {
          availableElements.value.push({
            id: eid,
            name: step.element.name || '未知元素',
            locator_value: step.element.locator_value,
          })
          existingIds.add(eid)
        }
      } else {
        eid = step.element || step.element_id || ''
      }
      return { ...step, element_id: eid, expanded: false }
    })
  } else {
    currentSteps.value = []
  }
  // 只有在切换到不同用例时才清空执行结果
  executionResult.value = null
  showSteps.value = true
  // 加载该用例的执行历史记录
  executionHistory.value = []
  selectedHistoryId.value = null
  loadExecutionHistory(testCase.id)
}

// 加载用例的执行历史记录
const loadExecutionHistory = async (testCaseId) => {
  if (!testCaseId) return
  historyLoading.value = true
  try {
    const response = await getTestCaseExecutions({ test_case: testCaseId, page_size: 20 })
    const data = response.data
    executionHistory.value = data.results || data || []
  } catch (e) {
    executionHistory.value = []
  } finally {
    historyLoading.value = false
  }
}

// 查看某条历史执行记录，回显步骤结果/日志/截图
const viewExecution = async (record) => {
  selectedHistoryId.value = record.id
  try {
    const res = await getTestCaseExecution(record.id)
    const d = res.data
    executionResult.value = {
      success: d.status === 'passed',
      status: d.status,
      logs: d.execution_logs || '',
      step_results: d.step_results || [],
      error_message: d.error_message || '',
      screenshots: d.screenshots || [],
      execution_time: d.execution_time || 0
    }
    resultActiveTab.value = 'steps'
    showSteps.value = false
  } catch (e) {
    ElMessage.error('加载执行记录失败')
  }
}

const addStep = () => {
  const newStep = {
    id: Date.now(),
    action_type: 'click',
    element_id: '',
    input_value: '',
    wait_time: 1000,
    assert_type: 'textContains',
    assert_value: '',
    description: '',
    expanded: true
  }
  currentSteps.value.push(newStep)
}

const removeStep = (index) => {
  currentSteps.value.splice(index, 1)
}

const onStepsReorder = () => {
  // 步骤重新排序后的处理
  console.log('步骤已重新排序')
}

const onActionTypeChange = (step) => {
  // 根据操作类型重置相关参数
  if (!['fill', 'input', 'swipe', 'aiInput', 'navigate'].includes(step.action_type)) {
    step.input_value = ''
  }
  if (step.action_type !== 'wait') {
    step.wait_time = 1000
  }
  if (!['assert', 'aiAssert'].includes(step.action_type)) {
    step.assert_type = 'textContains'
    step.assert_value = ''
  }
  // Midscene 动作不需要元素选择，清空 element_id
  if (['aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiAssert', 'navigate'].includes(step.action_type)) {
    step.element_id = null
  }
}

const onElementChange = (step) => {
  // 元素变化时的处理
  const element = availableElements.value.find(e => e.id === step.element_id)
  if (element && !step.description) {
    step.description = `${getActionTypeText(step.action_type)}${element.name}`
  }
}

const needsInputValue = (actionType) => {
  return ['fill', 'input', 'swipe', 'long_press', 'aiInput', 'navigate'].includes(actionType)
}

const needsWaitTime = (actionType) => {
  return ['wait', 'waitFor'].includes(actionType)
}

const needsElement = (actionType) => {
  return !['wait', 'switchTab', 'screenshot', 'swipe', 'launch_app', 'close_app', 'back', 'home', 'aiAct', 'aiTap', 'aiInput', 'aiQuery', 'aiAssert', 'navigate'].includes(actionType)
}

const expandAllSteps = () => {
  allStepsExpanded.value = !allStepsExpanded.value
  currentSteps.value.forEach(step => {
    step.expanded = allStepsExpanded.value
  })
}

const saveTestCase = async () => {
  if (!selectedTestCase.value) return

  try {
    const updateData = {
      ...selectedTestCase.value,
      steps: currentSteps.value
    }

    await updateTestCase(selectedTestCase.value.id, updateData)
    ElMessage.success('测试用例保存成功')

    // 更新本地数据
    const index = testCases.value.findIndex(tc => tc.id === selectedTestCase.value.id)
    if (index !== -1) {
      testCases.value[index] = { ...updateData }
      selectedTestCase.value = { ...updateData }
    }
  } catch (error) {
    console.error('保存测试用例失败:', error)
    ElMessage.error('保存测试用例失败')
  }
}

const runTestCase = async (testCase) => {
  // Appium / Airtest 引擎校验（都需要设备+应用）
  if (selectedEngine.value === 'appium' || selectedEngine.value === 'airtest') {
    if (!selectedDeviceId.value) {
      ElMessage.warning('请选择测试设备')
      return
    }
    if (!selectedAppConfigId.value) {
      ElMessage.warning('请选择应用配置')
      return
    }
  }

  isRunning.value = true
  try {
    if (selectedEngine.value === 'appium') {
      ElMessage.info(`开始执行 App 自动化测试... (引擎: Appium, 设备: ${selectedDeviceId.value})`)
    } else if (selectedEngine.value === 'airtest') {
      ElMessage.info(`开始执行 Airtest 自动化测试... (引擎: Airtest, 设备: ${selectedDeviceId.value})`)
    } else {
      const modeText = headlessMode.value ? '无头模式' : '有头模式'
      ElMessage.info(`开始执行测试用例... (引擎: ${selectedEngine.value.toUpperCase()}, 浏览器: ${selectedBrowser.value.toUpperCase()}, ${modeText})`)
    }

    const requestData = {
      project_id: realProjectId.value,
      engine: selectedEngine.value,
      browser: selectedBrowser.value,
      headless: headlessMode.value
    }
    // Appium / Airtest 额外参数（都需要设备+应用）
    if (selectedEngine.value === 'appium' || selectedEngine.value === 'airtest') {
      requestData.device_id = selectedDeviceId.value
      requestData.app_config_id = selectedAppConfigId.value
    }

    const response = await runTestCaseApi(testCase.id, requestData)

    // run() 接口只表示"任务已提交"，真实结果在后台线程异步执行。
    // 必须轮询执行详情，拿真实状态(status)和每步结果(step_results)。
    const executionId = response.data.execution_id
    if (executionId) {
      runningExecutionId.value = executionId
      await pollExecutionResult(executionId)
    } else {
      // 无 execution_id（极少数异常），退回原即时响应
      executionResult.value = response.data
      resultActiveTab.value = 'logs'
      showSteps.value = false
      if (response.data.success) {
        ElMessage.success('测试用例执行成功')
      } else {
        ElMessage.error('测试用例执行失败')
      }
    }
  } catch (error) {
    console.error('执行测试用例失败:', error)

    // 即使出错也要设置执行结果,显示错误信息
    const errorMessage = error.response?.data?.message || error.message || '执行失败'
    const errorLogs = error.response?.data?.logs || `测试用例执行出错\n\n错误信息: ${errorMessage}`

    // 格式化错误信息为统一的对象格式
    const errors = error.response?.data?.errors || [{
      message: errorMessage,
      details: error.stack || '',
      step_number: null,
      action_type: '',
      element: '',
      description: ''
    }]

    executionResult.value = {
      success: false,
      status: 'error',
      logs: errorLogs,
      step_results: [],
      screenshots: error.response?.data?.screenshots || [],
      execution_time: 0,
      errors: errors
    }
    resultActiveTab.value = 'logs'
    showSteps.value = false  // 切换到结果视图显示错误

    ElMessage.error(`执行测试用例失败: ${errorMessage}`)
  } finally {
    isRunning.value = false
    runningExecutionId.value = null
    aborting.value = false
  }
}

// 停止执行：只置服务端信号，不粗暴中断轮询 —— 轮询要继续，
// 这样才能把「已完成的步骤结果」实时展示出来（用户点停止后最想看的就是这个）。
const stopExecution = async () => {
  if (!runningExecutionId.value) {
    ElMessage.warning('当前没有可停止的执行')
    return
  }
  try {
    await ElMessageBox.confirm(
      '确认终止当前执行？执行线程会在当前步骤结束后停止，已完成的步骤结果会保留。',
      '终止执行',
      { type: 'warning', confirmButtonText: '确认终止', cancelButtonText: '取消' }
    )
  } catch (e) {
    return  // 用户取消
  }
  aborting.value = true
  try {
    const res = await abortTestCaseExecution(runningExecutionId.value)
    ElMessage.success(res.data?.message || '已发送终止信号')
  } catch (error) {
    const msg = error.response?.data?.message || error.message || '终止失败'
    ElMessage.error(msg)
  } finally {
    aborting.value = false
  }
}

// 轮询执行真实状态，直到执行完成（passed/failed/error）
const pollExecutionResult = async (executionId) => {
  const TERMINAL = ['passed', 'failed', 'error']
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  let lastData = null
  for (let i = 0; i < 120; i++) {  // 最多约 3 分钟
    try {
      const res = await getTestCaseExecution(executionId)
      lastData = res.data
      // 实时回显进行中的日志/步骤结果
      executionResult.value = {
        success: lastData.status === 'passed',
        status: lastData.status,
        logs: lastData.execution_logs || '',
        step_results: lastData.step_results || [],
        error_message: lastData.error_message || '',
        screenshots: lastData.screenshots || [],
        execution_time: lastData.execution_time || 0
      }
      resultActiveTab.value = 'logs'
      showSteps.value = false
      // 记录本次 poll 对应的执行 id，供「停止执行」使用
      runningExecutionId.value = executionId
      if (TERMINAL.includes(lastData.status)) break
    } catch (e) {
      // 轮询失败不中断，等待下次重试
    }
    await sleep(1500)
  }

  const finalStatus = lastData?.status
  const isSuccess = finalStatus === 'passed'
  executionResult.value = {
    ...executionResult.value,
    success: isSuccess,
    status: finalStatus,
    step_results: lastData?.step_results || [],
    error_message: lastData?.error_message || ''
  }
  // 完成后优先展示"步骤结果"，让用户直接看到失败到第几步
  resultActiveTab.value = 'steps'
  showSteps.value = false
  if (isSuccess) {
    ElMessage.success('测试用例执行成功')
  } else if (lastData?.error_message && lastData.error_message.includes('中止')) {
    // 人工中止不是缺陷，用 warning 而不是 error，避免误判成用例有问题
    ElMessage.warning('执行已中止，已完成步骤的结果已保留')
  } else {
    ElMessage.error(`测试用例执行失败（${finalStatus === 'failed' ? '步骤执行失败' : '执行异常'}）`)
  }
  // 执行完成后刷新历史记录列表
  if (selectedTestCase.value?.id) {
    loadExecutionHistory(selectedTestCase.value.id)
  }
}

const toggleView = () => {
  showSteps.value = !showSteps.value
}

const editTestCase = (testCase) => {
  editingTestCase.value = testCase
  testCaseForm.name = testCase.name
  testCaseForm.description = testCase.description || ''
  testCaseForm.priority = testCase.priority || 'medium'
  showCreateDialog.value = true
}

const deleteTestCase = async (testCase) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除测试用例"${testCase.name}"吗？`,
      '确认删除',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await deleteTestCaseApi(testCase.id)
    ElMessage.success('删除成功')

    // 从列表中移除
    const index = testCases.value.findIndex(tc => tc.id === testCase.id)
    if (index !== -1) {
      testCases.value.splice(index, 1)
    }

    // 如果删除的是当前选中的用例，清空选择
    if (selectedTestCase.value?.id === testCase.id) {
      selectedTestCase.value = null
      currentSteps.value = []
      executionResult.value = null
    }
  } catch (error) {
    if (error !== 'cancel') {
      console.error('删除测试用例失败:', error)
      ElMessage.error('删除失败')
    }
  }
}

const copyTestCase = async (testCase) => {
  try {
    await ElMessageBox.confirm(
      `确定要复制测试用例"${testCase.name}"吗？`,
      '确认复制',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'info'
      }
    )

    const response = await copyTestCaseApi(testCase.id)
    ElMessage.success('复制成功')

    // 找到原用例的位置
    const index = testCases.value.findIndex(tc => tc.id === testCase.id)
    if (index !== -1) {
      // 在原用例下方插入新用例
      testCases.value.splice(index + 1, 0, response.data)
    } else {
      // 如果找不到，就添加到末尾
      testCases.value.push(response.data)
    }
  } catch (error) {
    if (error !== 'cancel') {
      console.error('复制测试用例失败:', error)
      ElMessage.error('复制失败')
    }
  }
}

const variableCategories = [
  {
    label: '随机数',
    variables: [
      { name: 'random_int', syntax: '${random_int(min, max)}', desc: '生成随机整数', example: '${random_int(100, 999)}' },
      { name: 'random_float', syntax: '${random_float(min, max, decimals)}', desc: '生成随机浮点数', example: '${random_float(0, 1, 2)}' },
      { name: 'random_digits', syntax: '${random_digits(length)}', desc: '生成随机数字字符串', example: '${random_digits(6)}' }
    ]
  },
  {
    label: '随机字符串',
    variables: [
      { name: 'random_string', syntax: '${random_string(length)}', desc: '生成随机字母数字字符串', example: '${random_string(8)}' },
      { name: 'random_letters', syntax: '${random_letters(length)}', desc: '生成随机字母字符串', example: '${random_letters(8)}' },
      { name: 'random_chinese', syntax: '${random_chinese(length)}', desc: '生成随机中文字符', example: '${random_chinese(2)}' }
    ]
  },
  {
    label: '业务数据',
    variables: [
      { name: 'random_phone', syntax: '${random_phone()}', desc: '生成随机手机号', example: '${random_phone()}' },
      { name: 'random_email', syntax: '${random_email()}', desc: '生成随机邮箱', example: '${random_email()}' },
      { name: 'random_id_card', syntax: '${random_id_card()}', desc: '生成随机身份证号', example: '${random_id_card()}' },
      { name: 'random_name', syntax: '${random_name()}', desc: '生成随机中文姓名', example: '${random_name()}' },
      { name: 'random_company', syntax: '${random_company()}', desc: '生成随机公司名称', example: '${random_company()}' },
      { name: 'random_address', syntax: '${random_address()}', desc: '生成随机地址', example: '${random_address()}' }
    ]
  },
  {
    label: '时间日期',
    variables: [
      { name: 'timestamp', syntax: '${timestamp()}', desc: '当前时间戳(毫秒)', example: '${timestamp()}' },
      { name: 'datetime', syntax: '${datetime(format)}', desc: '格式化日期时间', example: '${datetime(YYYY-MM-DD HH:mm:ss)}' },
      { name: 'date', syntax: '${date(format)}', desc: '格式化日期', example: '${date(YYYY-MM-DD)}' },
      { name: 'time', syntax: '${time(format)}', desc: '格式化时间', example: '${time(HH:mm:ss)}' },
      { name: 'date_offset', syntax: '${date_offset(days, format)}', desc: '日期偏移', example: '${date_offset(1, YYYY-MM-DD)}' }
    ]
  },
  {
    label: '其他',
    variables: [
      { name: 'uuid', syntax: '${uuid()}', desc: '生成UUID', example: '${uuid()}' },
      { name: 'base64', syntax: '${base64(text)}', desc: 'Base64编码', example: '${base64(123456)}' },
      { name: 'md5', syntax: '${md5(text)}', desc: 'MD5哈希', example: '${md5(123456)}' }
    ]
  }
]

const openVariableHelper = (step, field) => {
  currentEditingStep.value = step
  currentEditingField.value = field
  showVariableHelper.value = true
}

const insertVariable = (variable) => {
  if (currentEditingStep.value && currentEditingField.value) {
    const example = variable.example
    const currentValue = currentEditingStep.value[currentEditingField.value] || ''
    
    // 简单起见，这里直接追加到末尾，或者如果为空则替换
    if (!currentValue) {
      currentEditingStep.value[currentEditingField.value] = example
    } else {
      currentEditingStep.value[currentEditingField.value] = currentValue + example
    }
    
    ElMessage.success(`已插入变量: ${variable.name}`)
    showVariableHelper.value = false
  }
}

const saveTestCaseForm = async () => {
  if (!testCaseForm.name.trim()) {
    ElMessage.warning('请输入测试用例名称')
    return
  }

  try {
    const data = {
      name: testCaseForm.name,
      description: testCaseForm.description,
      priority: testCaseForm.priority,
      project: realProjectId.value,
      steps: []
    }

    if (editingTestCase.value) {
      // 编辑现有用例
      await updateTestCase(editingTestCase.value.id, data)
      ElMessage.success('测试用例更新成功')

      // 更新本地数据
      const index = testCases.value.findIndex(tc => tc.id === editingTestCase.value.id)
      if (index !== -1) {
        testCases.value[index] = { ...testCases.value[index], ...data }
      }
    } else {
      // 创建新用例
      const response = await createTestCase(data)
      ElMessage.success('测试用例创建成功')
      testCases.value.push(response.data)
    }

    showCreateDialog.value = false
    editingTestCase.value = null
    resetForm()
  } catch (error) {
    console.error('保存测试用例失败:', error)
    ElMessage.error('保存失败')
  }
}

const resetForm = () => {
  testCaseForm.name = ''
  testCaseForm.description = ''
  testCaseForm.priority = 'medium'
}

// 辅助方法
const getStatusTag = (status) => {
  const tagMap = {
    'draft': 'info',
    'ready': 'success',
    'running': 'warning',
    'passed': 'success',
    'failed': 'danger'
  }
  return tagMap[status] || 'info'
}

const getStatusText = (status) => {
  const textMap = {
    'draft': '草稿',
    'ready': '就绪',
    'running': '执行中',
    'passed': '通过',
    'failed': '失败'
  }
  return textMap[status] || '未知'
}

const getActionTypeText = (actionType) => {
  const textMap = {
    'click': '点击',
    'tap': '轻触',
    'double_tap': '双击',
    'long_press': '长按',
    'fill': '输入',
    'input': '输入',
    'clear': '清空',
    'getText': '获取文本',
    'waitFor': '等待',
    'hover': '悬停',
    'scroll': '滚动',
    'swipe': '滑动',
    'screenshot': '截图',
    'assert': '断言',
    'wait': '等待',
    'switchTab': '切换标签页',
    'launch_app': '启动应用',
    'close_app': '关闭应用',
    'back': '返回',
    'home': '主页',
    'aiAct': 'AI操作',
    'aiTap': 'AI点击',
    'aiInput': 'AI输入',
    'aiQuery': 'AI查询',
    'aiAssert': 'AI断言',
    'navigate': '导航'
  }
  return textMap[actionType] || actionType
}

const formatTime = (timestamp) => {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return date.toLocaleString()
}

// 获取操作类型文本
const getActionText = (actionType) => {
  const actionMap = {
    'click': '点击',
    'tap': '轻触',
    'double_tap': '双击',
    'long_press': '长按',
    'fill': '填写',
    'input': '输入',
    'clear': '清空',
    'getText': '获取文本',
    'waitFor': '等待元素',
    'hover': '悬停',
    'scroll': '滚动',
    'swipe': '滑动',
    'screenshot': '截图',
    'assert': '断言',
    'wait': '等待',
    'switchTab': '切换标签页',
    'launch_app': '启动应用',
    'close_app': '关闭应用',
    'back': '返回',
    'home': '主页',
    'aiAct': 'AI操作',
    'aiTap': 'AI点击',
    'aiInput': 'AI输入',
    'aiQuery': 'AI查询',
    'aiAssert': 'AI断言',
    'navigate': '导航'
  }
  return actionMap[actionType] || actionType
}

// 图片处理方法
const handleImageError = (event) => {
  const img = event.target
  const screenshotIndex = parseInt(img.dataset.index)
  if (executionResult.value && executionResult.value.screenshots) {
    executionResult.value.screenshots[screenshotIndex].error = true
    executionResult.value.screenshots[screenshotIndex].loaded = true
  }
}

const handleImageLoad = (event) => {
  const img = event.target
  const screenshotIndex = parseInt(img.dataset.index)
  if (executionResult.value && executionResult.value.screenshots) {
    executionResult.value.screenshots[screenshotIndex].loaded = true
    executionResult.value.screenshots[screenshotIndex].error = false
  }
}

const previewScreenshot = (screenshot) => {
  currentScreenshot.value = screenshot
  showScreenshotPreview.value = true
}

// 加载 App 自动化设备和应用（合并旧版 ui-automation + 新版 app-automation 设备源）
const loadAppDevicesAndConfigs = async () => {
  try {
    const [devicesRes, newDevicesRes, configsRes] = await Promise.allSettled([
      getAppDevices({ status: 'online', page_size: 999 }),
      getNewDeviceList({ status: 'online', page_size: 999 }),
      // 不带 project 过滤：很多项目未单独配应用（AppConfig.project 为 null），
      // 若按项目过滤会得到空列表导致应用下拉显示"无数据"。
      // 应用下拉统一展示全量 AppConfig，用户按需切换；切项目时仅做"是否仍命中"的兜底。
      getAppConfigs({ page_size: 999 })
    ])
    // 合并新旧设备源，按 udid/device_id 去重（新版优先）
    const oldDevices = (devicesRes.status === 'fulfilled')
      ? (devicesRes.value.data.results || devicesRes.value.data || [])
      : []
    const newDevices = (newDevicesRes.status === 'fulfilled')
      ? (newDevicesRes.value.data.results || newDevicesRes.value.data || [])
      : []
    const idSet = new Set()
    const merged = []
    for (const d of [...newDevices, ...oldDevices]) {
      const idKey = d.udid || d.device_id || d.id
      if (!idSet.has(idKey)) {
        idSet.add(idKey)
        // 统一字段，兼容新旧两套设备表（旧表用 udid，新表用 device_id）
        d.device_id = d.udid || d.device_id || d.id
        if (!d.platform) d.platform = 'android'  // 新表仅 Android
        if (!d.name) d.name = d.device_id
        merged.push(d)
      }
    }
    appDevices.value = merged
    if (configsRes.status === 'fulfilled') {
      appConfigs.value = configsRes.value.data.results || configsRes.value.data || []
    }
    // 默认选中：当前选中项不在新列表里时，自动选第一个，避免用户每次进页面都得手动点
    // （常见场景：项目未配置专属应用，回退到全量列表里的第一项）
    if (appConfigs.value.length && !appConfigs.value.some(a => a.id === selectedAppConfigId.value)) {
      selectedAppConfigId.value = appConfigs.value[0].id
    }
    if (appDevices.value.length && !appDevices.value.some(d => d.device_id === selectedDeviceId.value)) {
      selectedDeviceId.value = appDevices.value[0].device_id
    }
  } catch (error) {
    // 加载失败不影响主流程
  }
}

// 页面元素探测
const doDumpPage = async () => {
  if (!selectedDeviceId.value) {
    ElMessage.warning('请先选择设备')
    return
  }
  if (!selectedAppConfigId.value) {
    ElMessage.warning('请先选择应用')
    return
  }
  isDumping.value = true
  try {
    const response = await dumpPageElements({
      device_id: selectedDeviceId.value,
      app_config_id: selectedAppConfigId.value,
      app_name: '万年历',
      engine: recordingEngine.value  // 跟随用户当前选择的录制引擎（airtest/appium）
    })
    if (response.data.success) {
      dumpedElements.value = response.data.candidates || response.data.elements || []
      ElMessage.success(response.data.message || '探测完成')
      // 刷新本地元素列表，使新探测到的元素可以显示在步骤下拉框中
      await loadElements()
    } else {
      ElMessage.error(response.data.message || '探测失败')
      dumpedElements.value = []
    }
  } catch (error) {
    console.error('探测页面失败:', error)
    ElMessage.error('探测页面失败: ' + (error.response?.data?.message || error.message))
    dumpedElements.value = []
  } finally {
    isDumping.value = false
  }
}

const addDumpedElementAsStep = async (element) => {
  if (!realProjectId.value) {
    ElMessage.warning('未选择项目，无法创建元素')
    return
  }
  try {
    // 1. 创建/复用 Element 记录（strategy 映射: id->1, xpath->3）
    let elementId = null
    const existing = availableElements.value.find(
      e => e.name === element.chinese_name && e.locator_value === element.value
    )
    if (existing) {
      elementId = existing.id
    } else {
      const strategyMap = { id: 1, xpath: 3 }
      const strategyId = element.strategy ? strategyMap[element.strategy] : null
      const createRes = await createElement({
        project_id: realProjectId.value,
        name: element.chinese_name,
        description: `页面探测: ${element.text || element.class_name || ''}`,
        element_type: element.clickable ? 'BUTTON' : 'TEXT',
        locator_strategy_id: strategyId,
        locator_value: element.value
      })
      elementId = createRes.data.id
      // 刷新元素列表
      await loadElements()
    }
    // 2. 添加步骤
    const newStep = {
      id: Date.now(),
      action_type: 'click',
      element_id: elementId,
      input_value: '',
      wait_time: 1000,
      assert_type: 'textContains',
      assert_value: '',
      description: `点击${element.chinese_name}`,
      expanded: true
    }
    currentSteps.value.push(newStep)
    ElMessage.success(`已添加步骤：点击${element.chinese_name}`)
  } catch (error) {
    console.error('添加步骤失败:', error)
    ElMessage.error('添加步骤失败: ' + (error.response?.data?.detail || error.message))
  }
}

const getDeviceLabel = () => {
  const d = appDevices.value.find(x => x.device_id === selectedDeviceId.value)
  return d ? `${d.name} (${d.platform})` : ''
}

const getAppConfigLabel = () => {
  const a = appConfigs.value.find(x => x.id === selectedAppConfigId.value)
  return a ? a.name : ''
}

// ==================== 导入 Airtest / Poco 脚本 ====================
const showImportDialog = ref(false)
const isImporting = ref(false)
const importForm = reactive({
  script: '',
  case_name: '',
  app_package: ''
})
// 导入的原始文件（.air 压缩包 / .py），用于上传模板图
const importFile = ref(null)
// 导入弹窗内独立维护的目标项目（unified id 与 resolved UiProject id），
// 不污染右上角 projectId / realProjectId 主状态
const importProjectId = ref('')
const importRealProjectId = ref(null)

// 当前导入项目名（用于包名自动填充匹配 AppConfig.ai_project）
const importCurrentProjectName = computed(() => {
  const p = allProjects.value.find(x => x.id === importProjectId.value)
  return p ? p.name : ''
})

// 选择项目后：resolve 到 UiProject id，并自动按新项目填充包名
const onImportProjectChange = async () => {
  importRealProjectId.value = await resolveUiProjectId(importProjectId.value)
  // 用新的 importProjectId / importRealProjectId 重算包名
  fillAppPackageFromImportProject()
}

// 按当前 importProjectId 从 AppConfig 里找包名填入
const fillAppPackageFromImportProject = () => {
  if (!importRealProjectId.value) return
  const projectIdNum = Number(importRealProjectId.value)
  const unified = importProjectId.value || ''
  const matched = appConfigs.value.find(a => {
    if (unified.startsWith('proj_')) {
      return Number(a.ai_project) === projectIdNum
    }
    return Number(a.project) === projectIdNum
  })
  if (matched && matched.package_name) {
    importForm.app_package = matched.package_name
  }
}
const currentProjectName = computed(() => {
  const p = allProjects.value.find(x => x.id === projectId.value)
  return p ? p.name : ''
})

const openImportDialog = async () => {
  if (!projectId.value) {
    ElMessage.warning('请先在右上角选择目标项目')
    return
  }
  // 默认继承右上角当前项目（弹窗内可改选）
  importProjectId.value = projectId.value
  importRealProjectId.value = realProjectId.value
  // 打开时若当前项目已配置应用包名，自动填入
  fillAppPackageFromImportProject()
  showImportDialog.value = true
}

const onAirtestFileChange = (file) => {
  const raw = file?.raw
  if (!raw) return
  importFile.value = raw
  const isArchive = /\.(air|zip)$/i.test(raw.name)
  if (isArchive) {
    // .air 压缩包：保留原始文件供上传提取模板图；文本留空（后端解压解析）
    importForm.script = ''
    ElMessage.success(`已选择压缩包：${raw.name}（将解析脚本并提取模板图）`)
    return
  }
  const reader = new FileReader()
  reader.onload = (e) => {
    importForm.script = e.target?.result || ''
    ElMessage.success(`已读取文件：${file.name}`)
  }
  reader.onerror = () => ElMessage.error('文件读取失败')
  reader.readAsText(raw)
}

const resetImportForm = () => {
  importForm.script = ''
  importForm.case_name = ''
  importForm.app_package = ''
  importProjectId.value = ''
  importRealProjectId.value = null
  importFile.value = null
}

const closeImportDialog = () => {
  resetImportForm()
}

const submitImport = async () => {
  if (!importProjectId.value) {
    ElMessage.warning('请选择目标项目')
    return
  }
  if (!importRealProjectId.value) {
    // 兜底：万一未触发 change（例如直接打开后未动下拉）
    importRealProjectId.value = await resolveUiProjectId(importProjectId.value)
    if (!importRealProjectId.value) {
      ElMessage.error('项目解析失败')
      return
    }
  }
  const hasArchive = importFile.value && /\.(air|zip)$/i.test(importFile.value.name)
  if (!hasArchive && !importForm.script.trim()) {
    ElMessage.warning('请粘贴或上传脚本内容')
    return
  }
  isImporting.value = true
  try {
    let payload
    if (hasArchive) {
      // .air 压缩包：FormData 上传，后端解压提取脚本+模板图
      const fd = new FormData()
      fd.append('script_file', importFile.value)
      fd.append('project_id', importRealProjectId.value)
      if (importForm.case_name.trim()) fd.append('case_name', importForm.case_name.trim())
      if (importForm.app_package.trim()) fd.append('app_package', importForm.app_package.trim())
      payload = fd
    } else {
      payload = {
        project_id: importRealProjectId.value,
        case_name: importForm.case_name.trim(),
        app_package: importForm.app_package.trim(),
        script: importForm.script
      }
    }
    const response = await importAirtestScript(payload)
    if (response.data.success) {
      ElMessage.success(response.data.message)
      showImportDialog.value = false
      await Promise.all([loadTestCases(), loadElements()])
      resetImportForm()
    } else {
      ElMessage.error(response.data.message || '导入失败')
    }
  } catch (error) {
    console.error('导入 Airtest 脚本失败:', error)
    ElMessage.error('导入失败: ' + (error.response?.data?.message || error.message))
  } finally {
    isImporting.value = false
  }
}

// ==================== 操作录制 ====================
const showRecordDialog = ref(false)
const recording = ref(false)
const recordedElements = ref([])
const selectedRecordElement = ref(null)
const recordedSteps = ref([])
const isRecordingBusy = ref(false)
const recordCaseName = ref('')
// 自动捕获（真机触摸监听）是否启用：控制录制横幅文案
const recordAutoCapture = ref(false)
// 录制中"镜像点击即录制"的操作类型：点击/长按/输入/滑动（与 Airtest IDE 一致）
const recOpMode = ref('click')
const recOpModeLabel = computed(() => ({
  click: '点击', long_press: '长按', input: '输入', swipe: '滑动',
}[recOpMode.value] || '点击'))
// 滑动模式下的起点控件缓存
const recSwipeStart = ref(null)
// 实时生成的 Airtest 脚本（纯 Airtest 录制模式下边录边看）
const recordScript = ref('')
const recordElementFilter = ref('')
const recordOnlyClickable = ref(true)
const recordScreenshot = ref('')
// 步骤轮询定时器：录制中自动拉取最新步骤（含自动捕获的触摸步骤）
let stepPollTimer = null
const _startStepPolling = () => {
  _stopStepPolling()
  stepPollTimer = setInterval(async () => {
    if (!recording.value) return
    try {
      const response = await recordingPage({})
      if (response.data.success) {
        // 仅在步骤数变化时更新（避免频繁渲染）
        const newSteps = response.data.steps || []
        if (newSteps.length !== recordedSteps.value.length) {
          recordedSteps.value = newSteps
          recordScript.value = response.data.airtest_script || ''
        }
      }
    } catch (_) {
      // 静默失败，下次轮询重试
    }
  }, 2000)
}
const _stopStepPolling = () => {
  if (stepPollTimer) { clearInterval(stepPollTimer); stepPollTimer = null }
}
const filteredRecordElements = computed(() => {
  let list = recordedElements.value
  if (recordOnlyClickable.value) {
    list = list.filter(e => e.clickable)
  }
  const kw = recordElementFilter.value.trim().toLowerCase()
  if (kw) {
    list = list.filter(e =>
      (e.chinese_name || '').toLowerCase().includes(kw) ||
      (e.text || '').toLowerCase().includes(kw) ||
      (e.value || '').toLowerCase().includes(kw)
    )
  }
  return list
})

const openRecording = async () => {
  if (!selectedDeviceId.value) {
    ElMessage.warning('请先选择设备')
    return
  }
  if (!selectedAppConfigId.value) {
    ElMessage.warning('请先选择应用')
    return
  }
  if (!realProjectId.value) {
    ElMessage.warning('请先选择项目')
    return
  }
  isRecordingBusy.value = true
  try {
    const response = await startRecording({
      device_id: selectedDeviceId.value,
      app_config_id: selectedAppConfigId.value,
      project_id: realProjectId.value,
      case_name: '',
      engine: recordingEngine.value
    })
    if (response.data.success) {
      recording.value = true
      recOpMode.value = 'click'
      recSwipeStart.value = null
      recordedElements.value = response.data.elements || []
      recordedSteps.value = response.data.steps || []
      recordScript.value = response.data.airtest_script || ''
      recordCaseName.value = response.data.case_name || ''
      recordAutoCapture.value = !!response.data.auto_capture
      recordScreenshot.value = response.data.screenshot || ''
      selectedRecordElement.value = null
      showRecordDialog.value = true
      _startStepPolling()
      ElMessage.success('录制已开始')
    } else {
      ElMessage.error(response.data.message || '开始录制失败')
    }
  } catch (error) {
    console.error('开始录制失败:', error)
    ElMessage.error('开始录制失败: ' + (error.response?.data?.message || error.message))
  } finally {
    isRecordingBusy.value = false
  }
}

const openContinueRecording = async () => {
  if (!selectedDeviceId.value) {
    ElMessage.warning('请先选择设备')
    return
  }
  if (!selectedAppConfigId.value) {
    ElMessage.warning('请先选择应用')
    return
  }
  if (!selectedTestCase.value) {
    ElMessage.warning('请先选择用例')
    return
  }
  isRecordingBusy.value = true
  try {
    const response = await startRecording({
      device_id: selectedDeviceId.value,
      app_config_id: selectedAppConfigId.value,
      project_id: realProjectId.value,
      case_name: selectedTestCase.value.name,
      engine: recordingEngine.value,
      continue_case_id: selectedTestCase.value.id,  // 关键：告知后端这是继续录制
    })
    if (response.data.success) {
      recording.value = true
      recOpMode.value = 'click'
      recSwipeStart.value = null
      recordedElements.value = response.data.elements || []
      recordedSteps.value = response.data.steps || []  // 预加载已有步骤！
      recordScript.value = response.data.airtest_script || ''
      recordCaseName.value = response.data.case_name || ''
      recordAutoCapture.value = !!response.data.auto_capture
      recordScreenshot.value = response.data.screenshot || ''
      selectedRecordElement.value = null
      showRecordDialog.value = true
      _startStepPolling()
      ElMessage.success(`继续录制已开始（已加载 ${recordedSteps.value.length} 个已有步骤）`)
    } else {
      ElMessage.error(response.data.message || '开始录制失败')
    }
  } catch (error) {
    console.error('继续录制失败:', error)
    ElMessage.error('继续录制失败: ' + (error.response?.data?.message || error.message))
  } finally {
    isRecordingBusy.value = false
  }
}

const refreshRecordPage = async () => {
  isRecordingBusy.value = true
  try {
    const response = await recordingPage({})
    if (response.data.success) {
      recordedElements.value = response.data.elements || []
      recordedSteps.value = response.data.steps || []
      recordScript.value = response.data.airtest_script || ''
      recordScreenshot.value = response.data.screenshot || ''
    } else {
      ElMessage.error(response.data.message || '刷新页面失败')
    }
  } catch (error) {
    ElMessage.error('刷新页面失败: ' + (error.response?.data?.message || error.message))
  } finally {
    isRecordingBusy.value = false
  }
}

const parseBounds = (b) => {
  if (!b) return null
  const m = String(b).match(/\[(\d+),(\d+)\]\[(\d+),(\d+)\]/)
  if (!m) return null
  return { x1: +m[1], y1: +m[2], x2: +m[3], y2: +m[4] }
}

const onScreenshotClick = async (e) => {
  const img = e.currentTarget
  const rect = img.getBoundingClientRect()
  if (!rect.width || !rect.height) return
  const ratioX = (e.clientX - rect.left) / rect.width
  const ratioY = (e.clientY - rect.top) / rect.height
  const devX = Math.round(ratioX * (img.naturalWidth || rect.width))
  const devY = Math.round(ratioY * (img.naturalHeight || rect.height))
  let best = null
  let bestArea = Infinity
  for (const el of recordedElements.value) {
    const b = parseBounds(el.bounds)
    if (!b) continue
    if (devX >= b.x1 && devX <= b.x2 && devY >= b.y1 && devY <= b.y2) {
      const area = (b.x2 - b.x1) * (b.y2 - b.y1)
      if (area < bestArea) {
        bestArea = area
        best = el
      }
    }
  }
  if (best) {
    // 用用户实际点击的设备坐标覆盖元素中心坐标，确保真机 input tap 精确命中
    // 用户手指点到的位置（如日历模式卡片中心）可能与反查到最小面积子元素的中心（如 itemImage）偏移很大
    best = { ...best, center_x: devX, center_y: devY }
    selectedRecordElement.value = best
    // 录制中：点击镜像即自动记录一步（与 Airtest IDE 在设备镜像上点击录制一致）
    if (recording.value) {
      if (recOpMode.value === 'swipe') {
        // 滑动：第一次点起点，第二次点终点，组合成一次滑动步骤
        if (!recSwipeStart.value) {
          recSwipeStart.value = best
          ElMessage.success(`滑动起点已记录：${best.chinese_name || best.text || best.value}，请再点终点控件`)
        } else {
          const start = recSwipeStart.value
          recSwipeStart.value = null
          const sx = start.center_x, sy = start.center_y
          const ex = best.center_x, ey = best.center_y
          if (sx == null || sy == null || ex == null || ey == null) {
            ElMessage.warning('起点或终点缺少坐标，已取消本次滑动')
          } else {
            await doRecord({
              action_type: 'swipe',
              element: { ...best, center_x: sx, center_y: sy },
              input_value: `${sx},${sy},${ex},${ey}`,
            })
          }
        }
      } else if (recOpMode.value === 'input') {
        try {
          const { value } = await ElMessageBox.prompt('请输入要输入到该控件的文本', '记录输入', {
            inputValue: '',
            confirmButtonText: '记录',
            cancelButtonText: '取消',
          })
          if (value != null && value !== '') {
            await doRecord({ action_type: 'input', element: best, input_value: value })
          }
        } catch (err) {
          /* 用户取消 */
        }
      } else if (recOpMode.value === 'long_press') {
        await doRecord({ action_type: 'long_press', element: best })
      } else {
        await doRecord({ action_type: 'click', element: best })
      }
    } else {
      ElMessage.success(`已选中：${best.chinese_name || best.text || best.value}`)
    }
  } else {
    ElMessage.warning('该位置未识别到可操作控件，可尝试「重新探测」或放大截图后重试')
  }
}

const doRecord = async (payload) => {
  isRecordingBusy.value = true
  try {
    const response = await recordAction(payload)
    if (response.data.success) {
      recordedElements.value = response.data.elements || []
      recordedSteps.value = response.data.steps || []
      recordScript.value = response.data.airtest_script || ''
      // 操作可能改变了页面，更新截图以便在新页面继续点选元素
      if (response.data.screenshot) {
        recordScreenshot.value = response.data.screenshot
      }
      selectedRecordElement.value = null
      ElMessage.success(response.data.message)
    } else {
      ElMessage.error(response.data.message || '记录失败')
    }
  } catch (error) {
    ElMessage.error('记录失败: ' + (error.response?.data?.message || error.message))
  } finally {
    isRecordingBusy.value = false
  }
}

const recordClick = () => {
  if (!selectedRecordElement.value) return
  doRecord({ action_type: 'click', element: selectedRecordElement.value })
}
const recordLongPress = () => {
  if (!selectedRecordElement.value) return
  doRecord({ action_type: 'long_press', element: selectedRecordElement.value })
}
const recordSwipe = (dir) => {
  if (!selectedRecordElement.value) return
  doRecord({ action_type: 'swipe', element: selectedRecordElement.value, input_value: dir })
}
const recordBack = () => doRecord({ action_type: 'back' })
const recordHome = () => doRecord({ action_type: 'home' })
const recordScreenshotAction = () => doRecord({ action_type: 'screenshot' })

const recordInput = async () => {
  if (!selectedRecordElement.value) return
  try {
    const { value } = await ElMessageBox.prompt('请输入要输入到该控件的文本', '记录输入', {
      confirmButtonText: '记录',
      cancelButtonText: '取消',
      inputValue: selectedRecordElement.value.text || ''
    })
    doRecord({ action_type: 'fill', element: selectedRecordElement.value, input_value: value })
  } catch (e) {
    // 用户取消
  }
}

const recordAssert = async () => {
  if (!selectedRecordElement.value) return
  try {
    const { value } = await ElMessageBox.prompt('请输入期望包含的文本内容', '记录断言', {
      confirmButtonText: '记录',
      cancelButtonText: '取消',
      inputValue: selectedRecordElement.value.text || ''
    })
    doRecord({
      action_type: 'assert',
      element: selectedRecordElement.value,
      assert_type: 'textContains',
      assert_value: value
    })
  } catch (e) {
    // 用户取消
  }
}

const recordWait = async () => {
  try {
    const { value } = await ElMessageBox.prompt('请输入等待秒数', '记录等待', {
      confirmButtonText: '记录',
      cancelButtonText: '取消',
      inputValue: '1'
    })
    doRecord({ action_type: 'wait', input_value: value })
  } catch (e) {
    // 用户取消
  }
}

const generateCase = async () => {
  isRecordingBusy.value = true
  try {
    const response = await generateRecordingCase({ case_name: recordCaseName.value })
    if (response.data.success) {
      ElMessage.success(response.data.message)
      recording.value = false
      showRecordDialog.value = false
      // 纯 Airtest 录制：额外导出可直接 airtest run 的脚本
      if (response.data.airtest_script) {
        downloadAirtestScript(response.data.airtest_script, response.data.case_name || 'airtest_case')
        ElMessage.success('已同时导出 Airtest 脚本（.py）')
      }
      await loadTestCases()
      // 生成用例时会新建 Element，刷新元素下拉框以便打开用例后能看到选中项
      await loadElements()
    } else {
      ElMessage.error(response.data.message || '生成用例失败')
    }
  } catch (error) {
    ElMessage.error('生成用例失败: ' + (error.response?.data?.message || error.message))
  } finally {
    isRecordingBusy.value = false
  }
}

const downloadAirtestScript = (text, name) => {
  const blob = new Blob([text], { type: 'text/x-python' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(name || 'airtest_case').replace(/[\\/:*?"<>|]/g, '_')}.py`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

const copyRecordScript = async () => {
  if (!recordScript.value) return
  try {
    await navigator.clipboard.writeText(recordScript.value)
    ElMessage.success('已复制 Airtest 脚本')
  } catch (e) {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

const stopRecord = async () => {
  // 仅停止实时录制（退出录制中模式），保留会话与已记录步骤，便于点击「生成用例」
  recording.value = false
  recSwipeStart.value = null
  _stopStepPolling()
  // 立刻拉取最新步骤（含自动捕获的触摸步骤）
  await refreshRecordPage()
  ElMessage.info('已停止实时录制，请确认步骤后点击「生成用例」')
}

const discardRecord = async () => {
  // 放弃录制：销毁后端会话并关闭弹窗
  _stopStepPolling()
  try {
    await stopRecording({})
  } catch (e) {
    // 忽略
  }
  recording.value = false
  recSwipeStart.value = null
  showRecordDialog.value = false
}

const onRecordDialogClose = () => {
  // 关闭弹窗时兜底清理后端录制会话（生成用例已内部销毁，此处为重复点击/叉号关闭兜底）
  _stopStepPolling()
  if (recording.value || recordedSteps.value.length) {
    stopRecording({}).catch(() => {})
  }
  recording.value = false
  recSwipeStart.value = null
}

// 监听引擎切换，加载 Appium/Airtest 数据
watch(selectedEngine, (newVal) => {
  // 引擎切换时同步录制引擎：选 Airtest → 录制也走 Airtest；选 Appium → 默认 Appium（用户可切纯 Airtest）
  if (newVal === 'airtest') {
    recordingEngine.value = 'airtest'
  } else if (newVal === 'appium') {
    recordingEngine.value = 'appium'
  }
  if (newVal === 'appium' || newVal === 'airtest') {
    loadAppDevicesAndConfigs()
  }
})

// 组件挂载
onMounted(async () => {
  await loadProjects()

  if (allProjects.value.length > 0) {
    const qp = route.query.project
    const initId = (qp && allProjects.value.some(p => p.id === qp)) ? qp : allProjects.value[0].id
    projectId.value = initId
    await onProjectChange()
  }

  // 初始即加载设备与应用配置列表，避免首次进入下拉框显示"无选项"
  if (selectedEngine.value === 'appium' || selectedEngine.value === 'airtest') {
    await loadAppDevicesAndConfigs()
  }
})

</script>

<style scoped>
.test-case-manager {
  height: 100vh;
  display: flex;
  flex-direction: column;
}

/* 操作录制弹窗样式 */
.recording-banner {
  display: flex;
  align-items: center;
  background: #fef0f0;
  color: #f56c6c;
  padding: 8px 12px;
  border-radius: 4px;
  margin-bottom: 12px;
  font-size: 13px;
}
.recording-banner.stopped {
  background: #f0f9eb;
  color: #67c23a;
}
.rec-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #f56c6c;
  margin-right: 8px;
  animation: rec-blink 1s infinite;
}
@keyframes rec-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
.recording-body {
  display: flex;
  gap: 16px;
  min-height: 420px;
}
.rec-elements {
  width: 340px;
  border-right: 1px solid #ebeef5;
  padding-right: 16px;
}
.rec-right {
  flex: 1;
}
.rec-panel-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.rec-element-filter {
  display: flex;
  align-items: center;
  margin: 8px 0;
}
.rec-screenshot {
  margin: 8px 0;
}
.rec-screenshot-tip {
  font-size: 12px;
  color: #409eff;
  margin-bottom: 4px;
}
.rec-screenshot-img {
  width: 100%;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  cursor: crosshair;
  display: block;
}
.rec-screenshot-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 30px;
  border: 1px dashed #dcdfe6;
  border-radius: 4px;
  color: #909399;
  font-size: 13px;
  gap: 8px;
  min-height: 200px;
}
.rec-screenshot-empty .el-icon {
  font-size: 32px;
  color: #c0c4cc;
}
.rec-screenshot-empty-hint {
  font-size: 12px;
  color: #b0b0b0;
}
.rec-el-noclick {
  color: #c0c4cc;
  font-size: 11px;
  border: 1px solid #ebeef5;
  border-radius: 3px;
  padding: 0 4px;
}
.rec-element-list {
  max-height: 380px;
  overflow-y: auto;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}
.rec-element-item {
  padding: 6px 10px;
  border-bottom: 1px solid #f2f2f2;
  cursor: pointer;
  font-size: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.rec-element-item:hover { background: #f5f7fa; }
.rec-element-item.active { background: #ecf5ff; }
.rec-el-name { font-weight: 600; min-width: 90px; }
.rec-el-val {
  color: #909399;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rec-actions {
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.rec-sub { font-size: 13px; color: #606266; }
.rec-steps {
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid #ebeef5;
  border-radius: 4px;
}
.rec-code {
  width: 440px;
  border-left: 1px solid #ebeef5;
  padding-left: 16px;
  display: flex;
  flex-direction: column;
}
.rec-code-pre {
  flex: 1;
  margin: 0;
  padding: 10px;
  background: #1e1e1e;
  color: #d4d4d4;
  border-radius: 4px;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
  overflow: auto;
  max-height: 460px;
}
.rec-step-item {
  padding: 6px 10px;
  border-bottom: 1px solid #f2f2f2;
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.rec-step-no {
  background: #409eff;
  color: #fff;
  border-radius: 50%;
  width: 20px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}
.rec-step-desc { font-weight: 500; }
.rec-step-input { color: #67c23a; }

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px;
  border-bottom: 1px solid #e6e6e6;
  background: white;
}

.page-title {
  margin: 0;
  font-size: 24px;
}

.header-actions {
  display: flex;
  align-items: center;
}

.main-content {
  flex: 1;
  display: flex;
  overflow: hidden;
}

.left-panel {
  width: 350px;
  border-right: 1px solid #e6e6e6;
  background: white;
  display: flex;
  flex-direction: column;
}

.panel-header {
  padding: 15px;
  border-bottom: 1px solid #e6e6e6;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-header h3 {
  margin: 0;
}

.test-case-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}

.test-case-item {
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  margin-bottom: 10px;
  padding: 15px;
  cursor: pointer;
  transition: all 0.3s;
}

.test-case-item:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
}

.test-case-item.active {
  border-color: #409eff;
  background-color: #f0f8ff;
}

.case-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 10px;
}

.case-info {
  flex: 1;
}

.case-name {
  margin: 0 0 5px 0;
  font-size: 16px;
  font-weight: 600;
}

.case-description {
  margin: 0;
  color: #666;
  font-size: 14px;
  line-height: 1.4;
}

.case-actions {
  display: flex;
  gap: 5px;
}

.case-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: #888;
}

.step-count {
  color: #409eff;
  font-weight: 500;
}

.right-panel {
  flex: 1;
  background: white;
  display: flex;
  flex-direction: column;
}

.test-case-detail {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 20px;
  overflow: hidden;
  height: 100%;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 15px;
  border-bottom: 1px solid #e6e6e6;
}

.detail-header h3 {
  margin: 0;
}

.detail-actions {
  display: flex;
  gap: 10px;
}

.steps-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-bottom: 20px;
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  background: #fafafa;
  overflow: hidden;
}

.steps-container.has-steps {
  max-height: 50%;
}

.steps-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
}

.steps-header h4 {
  margin: 0;
}

.steps-list {
  padding: 10px;
  padding-bottom: 20px;
}

.steps-scroll-container {
  overflow-y: auto;
  flex: 1;
  min-height: 0;
  padding: 10px;
  padding-right: 5px;
}

.steps-scroll-container::-webkit-scrollbar {
  width: 6px;
}

.steps-scroll-container::-webkit-scrollbar-track {
  background: #f5f5f5;
  border-radius: 3px;
}

.steps-scroll-container::-webkit-scrollbar-thumb {
  background: #ccc;
  border-radius: 3px;
}

.steps-scroll-container::-webkit-scrollbar-thumb:hover {
  background: #999;
}

.step-item {
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  margin-bottom: 10px;
  background: white;
  transition: all 0.3s;
}

.step-item:hover {
  border-color: #409eff;
}

.step-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 15px;
  background: #fafafa;
  border-radius: 6px 6px 0 0;
}

.step-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.drag-handle {
  cursor: move;
  color: #999;
}

.step-number {
  background: #409eff;
  color: white;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: bold;
}

.step-right {
  display: flex;
  gap: 5px;
}

.step-content {
  padding: 15px;
  border-top: 1px solid #e6e6e6;
}

.step-param {
  display: flex;
  align-items: center;
  margin-bottom: 10px;
  gap: 10px;
}

.step-param label {
  width: 120px;
  font-weight: 500;
  color: #333;
}

.execution-result {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid #e6e6e6;
  border-radius: 6px;
  background: white;
  overflow: hidden;
}

.execution-result.with-steps {
  margin-top: 0;
}

.execution-result .result-header {
  padding: 15px;
  border-bottom: 1px solid #e6e6e6;
  background: #fafafa;
  border-radius: 6px 6px 0 0;
}

.global-error-banner {
  padding: 12px 16px 0;
}
.global-error-banner :deep(.el-alert) {
  margin-bottom: 12px;
}
.global-error-banner :deep(.error-message) {
  margin: 8px 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 13px;
  line-height: 1.5;
  max-height: 300px;
  overflow-y: auto;
}

.execution-result .result-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 15px;
}

.result-content {
  flex: 1;
  overflow: hidden;
}

/* 为el-tabs和el-tab-pane添加flex布局支持 */
.result-content :deep(.el-tabs) {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.result-content :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.result-content :deep(.el-tab-pane) {
  height: 100%;
  overflow: auto;
}

/* .result-header 已在 .execution-result 中定义 */

.result-header h4 {
  margin: 0;
}

.logs-container {
  max-height: 500px;
  overflow-y: auto;
  background: #f5f7fa;
  padding: 15px;
  border-radius: 4px;
}

.log-item {
  margin-bottom: 15px;
  padding: 12px;
  background: white;
  border-radius: 4px;
  border-left: 3px solid #409eff;
}

.log-item:last-child {
  margin-bottom: 0;
}

.log-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;

  .step-screenshot-thumb {
    flex: 0 0 auto;
    width: 72px;
    height: 44px;
    border-radius: 4px;
    overflow: hidden;
    cursor: pointer;
    border: 1px solid #dcdfe6;
    transition: border-color 0.2s;

    &:hover { border-color: #409eff; }

    :deep(.el-image) {
      width: 100%;
      height: 100%;
      display: block;
    }
  }
}

.log-action {
  font-weight: 500;
  color: #606266;
}

.log-desc {
  color: #909399;
  font-size: 14px;
}

.log-error {
  display: flex;
  align-items: flex-start;  /* 改为 flex-start，适配多行文本 */
  gap: 8px;
  color: #f56c6c;
  background: #fef0f0;
  padding: 8px 12px;
  border-radius: 4px;
  margin-top: 8px;
  font-size: 14px;

  .error-message {
    margin: 0;
    padding: 0;
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;  /* 保留换行符和空格 */
    word-break: break-word;  /* 长单词换行 */
    flex: 1;
  }

  .el-icon {
    margin-top: 2px;  /* 图标与文本顶部对齐 */
    flex-shrink: 0;  /* 图标不缩小 */
  }
}

.screenshots-container {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 20px;
  padding: 10px;
}

.screenshot-item {
  display: flex;
  flex-direction: column;
  cursor: pointer;
  transition: transform 0.2s ease;
}

.screenshot-item:hover {
  transform: translateY(-4px);
}

.screenshot-wrapper {
  position: relative;
  width: 100%;
  min-height: 200px;
  background: #f5f5f5;
  border-radius: 8px;
  border: 2px solid #e6e6e6;
  overflow: hidden;
  transition: border-color 0.3s ease;
}

.screenshot-item:hover .screenshot-wrapper {
  border-color: #409eff;
}

.screenshot-wrapper img {
  width: 100%;
  height: auto;
  display: block;
  transition: opacity 0.3s ease;
}

.screenshot-overlay {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  transition: opacity 0.3s ease;
}

.screenshot-item:hover .screenshot-overlay {
  opacity: 1;
}

.zoom-icon {
  font-size: 48px;
  color: white;
}

.screenshot-placeholder,
.screenshot-error {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  color: #999;
  font-size: 14px;
}

.screenshot-placeholder .el-icon,
.screenshot-error .el-icon {
  font-size: 32px;
  margin-bottom: 8px;
}

.screenshot-error {
  color: #f56c6c;
}

.screenshot-info {
  margin-top: 10px;
}

.screenshot-description {
  margin: 0 0 5px 0;
  font-size: 14px;
  font-weight: 500;
  color: #333;
  text-align: left;
}

.screenshot-meta {
  margin: 0 0 3px 0;
  font-size: 12px;
  color: #666;
  text-align: left;
}

.screenshot-time {
  margin: 0;
  font-size: 11px;
  color: #999;
  text-align: left;
}

/* 截图预览对话框样式 */
.screenshot-preview {
  display: flex;
  flex-direction: column;
}

.preview-info {
  margin-bottom: 20px;
  padding: 15px;
  background: #f5f7fa;
  border-radius: 6px;
}

.preview-info h4 {
  margin: 0 0 10px 0;
  font-size: 16px;
  color: #333;
}

.preview-info p {
  margin: 5px 0;
  font-size: 14px;
  color: #666;
}

.preview-image {
  display: flex;
  justify-content: center;
  align-items: center;
  background: #f5f5f5;
  border-radius: 8px;
  padding: 20px;
  max-height: 70vh;
  overflow: auto;
}

.preview-image img {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
}

.errors-container {
  padding: 10px;
  height: 100%;
  overflow-y: auto;
}

.error-item {
  background: #fff;
  border: 2px solid #f56c6c;
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 15px;
}

.error-item:last-child {
  margin-bottom: 0;
}

.error-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 15px;
  padding-bottom: 15px;
  border-bottom: 1px solid #f5f5f5;
}

.error-header .el-tag {
  font-size: 16px;
  padding: 10px 15px;
  font-weight: 600;
}

.error-header .el-icon {
  margin-right: 5px;
}

.error-step {
  background: #fef0f0;
  color: #f56c6c;
  padding: 5px 12px;
  border-radius: 4px;
  font-weight: 600;
  font-size: 14px;
}

.error-meta {
  background: #f9f9f9;
  padding: 15px;
  border-radius: 6px;
  margin-bottom: 15px;
}

.meta-item {
  display: flex;
  align-items: flex-start;
  margin-bottom: 8px;
}

.meta-item:last-child {
  margin-bottom: 0;
}

.meta-label {
  font-weight: 600;
  color: #606266;
  min-width: 80px;
  margin-right: 10px;
}

.meta-value {
  color: #303133;
  flex: 1;
}

.error-details {
  background: #2d2d2d;
  border-radius: 6px;
  overflow: hidden;
}

.details-header {
  background: #1e1e1e;
  color: #fff;
  padding: 10px 15px;
  font-weight: 600;
  font-size: 14px;
  border-bottom: 1px solid #3d3d3d;
}

.details-content {
  color: #ff6b6b;
  padding: 15px;
  margin: 0;
  font-family: 'Courier New', Courier, monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-wrap: break-word;
  max-height: 400px;
  overflow-y: auto;
}

.details-content::-webkit-scrollbar {
  width: 6px;
}

.details-content::-webkit-scrollbar-track {
  background: #1e1e1e;
}

.details-content::-webkit-scrollbar-thumb {
  background: #555;
  border-radius: 3px;
}

.details-content::-webkit-scrollbar-thumb:hover {
  background: #777;
}

.no-selection {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}
/* 执行记录历史面板 */
.execution-history {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  margin-bottom: 16px;
  background: #fafafa;
}
.history-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  font-weight: 600;
  font-size: 14px;
  color: #303133;
}
.history-header .history-title {
  flex: 0 0 auto;
}
.history-header .history-spacer {
  flex: 1;
}
.history-header .collapse-icon {
  transition: transform 0.2s;
}
.history-header .collapse-icon.is-collapsed {
  transform: rotate(-90deg);
}
.history-body {
  padding: 0 14px 12px 14px;
}
.history-loading {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #909399;
  font-size: 13px;
  padding: 8px 0;
}
.history-list {
  max-height: 240px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.history-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  background: white;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.history-item:hover {
  border-color: #409eff;
  background: #f0f8ff;
}
.history-item.active {
  border-color: #409eff;
  background: #ecf5ff;
}
.history-engine {
  color: #606266;
  font-weight: 500;
}
.history-time {
  color: #909399;
}
.history-duration,
.history-creator {
  color: #c0c4cc;
}

</style>