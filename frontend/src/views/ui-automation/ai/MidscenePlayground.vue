<template>
  <div class="midscene-playground">
    <div class="page-header">
      <h1>🛝 Midscene Playground</h1>
      <p class="subtitle">
        用自然语言调试 APP 真机操作。确认稳定后，再把验证过的 Prompt 沉淀到正式测试框架（AI 用例管理）。
      </p>
    </div>

    <div class="pg-layout">
      <!-- 左侧：控制面板 -->
      <div class="pg-panel">
        <el-form label-position="top">
          <el-form-item label="应用配置">
            <el-select v-model="form.app_config_id" placeholder="选择应用" style="width: 100%">
              <el-option
                v-for="cfg in appConfigs"
                :key="cfg.id"
                :label="cfg.name + (cfg.package_name ? ' (' + cfg.package_name + ')' : '')"
                :value="cfg.id"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="执行设备">
            <el-select v-model="form.device_id" placeholder="选择设备" style="width: 100%">
              <el-option
                v-for="dev in devices"
                :key="dev.id"
                :label="dev.name + (dev.udid ? ' (' + dev.udid + ')' : '')"
                :value="dev.id"
              />
            </el-select>
          </el-form-item>

          <el-form-item label="操作类型">
            <el-radio-group v-model="form.action">
              <el-radio value="aiAct">aiAct（自然语言执行）</el-radio>
              <el-radio value="aiTap">aiTap（点击）</el-radio>
              <el-radio value="aiInput">aiInput（输入）</el-radio>
              <el-radio value="aiQuery">aiQuery（提取）</el-radio>
            </el-radio-group>
          </el-form-item>

          <el-form-item label="自然语言指令">
            <el-input
              v-model="form.prompt"
              type="textarea"
              :rows="4"
              placeholder="用语义描述，禁止写坐标！例如：点击右下角的「我的」Tab，进入个人中心"
            />
            <div class="prompt-examples">
              <span class="examples-label">示例：</span>
              <el-tag
                v-for="ex in promptExamples"
                :key="ex"
                size="small"
                class="example-tag"
                @click="form.prompt = ex"
              >{{ ex }}</el-tag>
            </div>
            <div class="prompt-warn">⚠️ 禁止写死像素/坐标（如 980,2100），否则换机型会失效，系统会拦截。</div>

            <!-- A：边打字边提示会拆成几步 + 一键拆步入口。
                 之前只有「存为用例步骤」（整句存 1 步）和「AI 拆成多步」两个按钮，
                 长得像但语义完全不同，用户以为会自动拆，结果存进去还是 1 步。 -->
            <div v-if="form.prompt.trim()" class="split-hint">
              <span v-if="detectedActions >= 2">
                💡 检测到约 <strong>{{ detectedActions }}</strong> 个操作，建议拆成多步执行 ——
                这样报告里每一步都有独立截图，哪步失败一眼能看出。
              </span>
              <span v-else>
                💡 看起来是单个操作，直接「执行」即可；也可以拆成标准步骤存进用例。
              </span>
              <el-button
                type="primary"
                link
                size="small"
                :loading="generatingSteps"
                @click="openGenerateDialog"
              >立即拆成多步 →</el-button>
            </div>
          </el-form-item>

          <template v-if="form.action === 'aiInput'">
            <el-form-item label="定位提示（可选）">
              <el-input v-model="form.selector_hint" placeholder="aiInput 优先用此定位，如「搜索框」" />
            </el-form-item>
            <el-form-item label="输入值">
              <el-input v-model="form.value" placeholder="要输入到该输入框的内容" />
            </el-form-item>
          </template>

          <el-form-item>
            <el-checkbox v-model="form.launch_app">执行前先启动应用</el-checkbox>
          </el-form-item>

          <!-- 按钮区分两行，避免 4 个按钮挤在一行溢出面板：
               第一行 = 执行 + 保存（动作类）；第二行 = 拆步（AI 辅助类，单独突出）。
               面板宽度在窄屏会被压到 360px，所以用 flex-wrap 让它自适应换行。 -->
          <div class="pg-actions">
            <!-- 执行中：按钮变成「终止执行」。Playground 是同步请求，
                 不提供终止就只能干等（aiAct 撞 replanning 上限要几分钟）。 -->
            <el-button
              v-if="running"
              type="danger"
              :loading="aborting"
              @click="abortPlayground"
            >
              {{ aborting ? '正在终止...' : '■ 终止执行' }}
            </el-button>
            <el-button
              v-else
              type="primary"
              :disabled="!form.app_config_id || !form.device_id || !form.prompt.trim()"
              @click="runPlayground"
            >
              ▶ 执行
            </el-button>
            <el-button
              type="success"
              plain
              :disabled="!form.prompt.trim()"
              @click="openSaveActionDialog"
            >
              存为公共步骤
            </el-button>
            <el-tooltip
              content="把当前这一句原样存成 1 条步骤，不做拆分。适合单个操作。"
              placement="top"
            >
              <el-button
                type="warning"
                plain
                :disabled="!form.prompt.trim()"
                @click="openSaveStepDialog"
              >
                存为用例步骤（1 步）
              </el-button>
            </el-tooltip>
          </div>
          <!-- 一句话 → 多条原子步骤：这里写的通常是整段操作流程。
             与「存为用例步骤」的区别：后者原样存 1 条（整句交给模型自己拆），
             这个是先让 AI 拆成 N 条再逐条写入，报告里能一步步定位 + 每步一张图。 -->
          <el-tooltip
            content="让 AI 把这段话拆成多条原子步骤，预览确认后写入用例。推荐用于整段操作流程。"
            placement="top"
          >
            <el-button
              class="gen-btn"
              type="primary"
              :loading="generatingSteps"
              :disabled="!form.prompt.trim()"
              @click="openGenerateDialog"
            >
              <el-icon><MagicStick /></el-icon>
              AI 拆成多步（推荐）
            </el-button>
          </el-tooltip>
        </el-form>

        <!-- 写入用例步骤对话框（可追加到已有用例，也可新建用例） -->
        <el-dialog
          v-model="saveStepDialogVisible"
          title="写入 AI 用例步骤"
          width="520px"
          :close-on-click-modal="false"
        >
          <el-form label-width="100px">
            <el-form-item label="目标用例" required>
              <el-select
                v-model="saveStepForm.ai_case_id"
                placeholder="选择要追加步骤的用例，或新建"
                filterable
                style="width: 100%"
              >
                <el-option
                  :label="'+ 新建用例（这个操作本身就是一个测试场景）'"
                  :value="NEW_CASE_VALUE"
                />
                <el-option
                  v-for="c in aiCases"
                  :key="c.id"
                  :label="c.name + (c.execution_mode === 'midscene' ? '（midscene）' : '（text）')"
                  :value="c.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item v-if="saveStepForm.ai_case_id === NEW_CASE_VALUE" label="新用例名" required>
              <el-input v-model="saveStepForm.new_case_name" placeholder="如：万年历-查看天气" />
              <div class="form-hint">设备与应用配置会自动沿用上方当前选择</div>
            </el-form-item>
            <el-form-item label="步骤名">
              <el-input v-model="saveStepForm.name" placeholder="可选，留空自动编号" />
            </el-form-item>
            <el-alert
              type="info"
              :closable="false"
              show-icon
              title="把当前操作类型与自然语言指令写成一条步骤。选已有用例=追加到它的流程里；选「新建用例」=这个操作独立成一条可执行用例。之后到「AI 用例管理」即可执行重放，无需重新试错。"
            />
          </el-form>
          <template #footer>
            <el-button @click="saveStepDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="savingStep" @click="saveStepToCase">
              {{ saveStepForm.ai_case_id === NEW_CASE_VALUE ? '创建用例' : '追加' }}
            </el-button>
          </template>
        </el-dialog>

        <!-- AI 拆成多步：先预览再写入 -->
        <el-dialog
          v-model="generateDialogVisible"
          title="AI 拆成多步"
          width="720px"
          :close-on-click-modal="false"
        >
          <el-alert
            type="info"
            :closable="false"
            show-icon
            title="AI 会把上面那段指令拆成多条原子步骤（一个动作一步）。确认后一次性写入目标用例，报告里每一步都有独立截图，失败时能定位到具体哪一步。"
            style="margin-bottom: 12px"
          />

          <!-- 步骤列表：每条 = 一个输入框（一个步骤）。
               可手工添加/删除/上下移动 —— AI 拆得不理想时不必重新生成。 -->
          <div v-if="generatedSteps.length" class="gen-steps">
            <div v-for="(s, i) in generatedSteps" :key="i" class="gen-step">
              <div class="gen-step-head">
                <span class="gen-no">步骤 {{ i + 1 }}</span>
                <el-select v-model="s.action" size="small" style="width: 118px">
                  <el-option label="aiAct 操作" value="aiAct" />
                  <el-option label="aiTap 点击" value="aiTap" />
                  <el-option label="aiInput 输入" value="aiInput" />
                  <el-option label="aiQuery 提取" value="aiQuery" />
                  <el-option label="launch 启动" value="launch" />
                  <el-option label="back 返回" value="back" />
                  <el-option label="home 回主屏" value="home" />
                  <el-option label="wait 等待" value="wait" />
                  <el-option label="screenshot 截图" value="screenshot" />
                </el-select>
                <el-input v-model="s.name" size="small" placeholder="步骤名" style="width: 150px" />
                <div class="gen-step-ops">
                  <el-button size="small" text :disabled="i === 0" title="上移"
                             @click="moveStep(i, -1)">↑</el-button>
                  <el-button size="small" text :disabled="i === generatedSteps.length - 1" title="下移"
                             @click="moveStep(i, 1)">↓</el-button>
                  <el-button size="small" text type="danger" title="删除"
                             @click="generatedSteps.splice(i, 1)">×</el-button>
                </div>
              </div>
              <el-input
                v-model="s.prompt"
                size="small"
                type="textarea"
                :rows="1"
                :autosize="{ minRows: 1, maxRows: 3 }"
                :placeholder="promptOptional(s.action) ? '该动作不需要指令（可留空）' : '给视觉模型的指令'"
              />
              <el-input
                v-if="s.action === 'aiInput'"
                v-model="s.value"
                size="small"
                placeholder="要输入的内容"
                style="margin-top: 6px"
              />
            </div>

            <el-button size="small" style="width: 100%; margin-top: 8px" @click="appendStep">
              + 添加步骤
            </el-button>
          </div>
          <div v-else class="gen-empty">
            <el-empty description="还没有步骤" :image-size="60" />
            <el-button size="small" type="primary" plain @click="appendStep">+ 手工添加步骤</el-button>
          </div>

          <el-form label-width="100px" style="margin-top: 12px">
            <el-form-item label="写入到" required>
              <el-select
                v-model="generateForm.ai_case_id"
                placeholder="选择要写入的用例，或新建"
                filterable
                style="width: 100%"
              >
                <el-option
                  :label="'+ 新建用例（这段流程本身就是一条用例）'"
                  :value="NEW_CASE_VALUE"
                />
                <el-option
                  v-for="c in aiCases"
                  :key="c.id"
                  :label="c.name + (c.execution_mode === 'midscene' ? '（midscene）' : '（text）')"
                  :value="c.id"
                />
              </el-select>
            </el-form-item>
            <el-form-item v-if="generateForm.ai_case_id === NEW_CASE_VALUE" label="新用例名" required>
              <el-input v-model="generateForm.new_case_name" placeholder="如：万年历-查看天气" />
              <div class="form-hint">设备与应用配置会自动沿用上方当前选择</div>
            </el-form-item>
          </el-form>

          <template #footer>
            <el-button @click="generateDialogVisible = false">取消</el-button>
            <el-button :loading="generatingSteps" @click="doGenerateSteps">
              <el-icon><Refresh /></el-icon>
              重新生成
            </el-button>
            <el-button
              type="primary"
              :loading="savingGenerated"
              :disabled="!generatedSteps.length"
              @click="saveGeneratedSteps"
            >
              写入 {{ generatedSteps.length }} 条步骤
            </el-button>
          </template>
        </el-dialog>

        <!-- 保存为公共步骤对话框 -->
        <el-dialog
          v-model="saveActionDialogVisible"
          title="保存为公共步骤（可被多条用例复用）"
          width="520px"
          :close-on-click-modal="false"
        >
          <el-form label-width="100px">
            <el-form-item label="名称" required>
              <el-input
                v-model="saveActionForm.name"
                placeholder="如 login / clear-cache（英文、唯一）"
              />
              <div class="form-hint">用例里靠这个名字引用，创建后不建议改</div>
            </el-form-item>
            <el-form-item label="分类">
              <el-select v-model="saveActionForm.category" style="width: 100%">
                <el-option
                  v-for="c in ACTION_CATEGORIES"
                  :key="c.value"
                  :label="c.label"
                  :value="c.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="描述">
              <el-input v-model="saveActionForm.description" placeholder="可选，说明这个步骤做什么" />
            </el-form-item>
            <el-form-item label="动作类型">
              <el-select v-model="saveActionForm.action" style="width: 100%">
                <el-option value="aiAct" label="aiAct（自然语言执行）" />
                <el-option value="aiTap" label="aiTap（点击）" />
                <el-option value="aiInput" label="aiInput（输入）" />
                <el-option value="aiQuery" label="aiQuery（提取）" />
                <el-option value="assert" label="assert（断言）" />
              </el-select>
            </el-form-item>
            <el-form-item label="语义指令">
              <el-input v-model="saveActionForm.prompt" type="textarea" :rows="3" />
            </el-form-item>
            <el-form-item label="定位提示">
              <el-input v-model="saveActionForm.selector_hint" placeholder="可选" />
            </el-form-item>
            <el-form-item label="输入值">
              <el-input v-model="saveActionForm.value" placeholder="aiInput 用，可选" />
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="saveActionDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="savingAction" @click="saveBusinessAction">保存</el-button>
          </template>
        </el-dialog>

        <div v-if="logs.length" class="pg-logs">
          <div class="logs-title">执行日志</div>
          <pre class="logs-content">{{ logs.join('\n') }}</pre>
        </div>
      </div>

      <!-- 右侧：截图回看 -->
      <div class="pg-preview">
        <div class="preview-title">真机截图</div>
        <div v-if="screenshot" class="preview-body">
          <img :src="screenshot" alt="screenshot" class="preview-img" />
        </div>
        <div v-else class="preview-empty">
          <el-icon :size="48"><Monitor /></el-icon>
          <p>执行后将在此显示真机截图</p>
        </div>
        <el-alert
          v-if="resultError"
          :title="'执行出错: ' + resultError"
          type="error"
          show-icon
          :closable="false"
          style="margin-top: 12px"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Monitor, MagicStick, Refresh } from '@element-plus/icons-vue'
import {
  getAppConfigs,
  getAppDevices,
  getAICases,
  createBusinessAction,
  saveMidsceneStep,
  generateCaseSteps
} from '@/api/ui_automation'
import api from '@/utils/api'

const appConfigs = ref([])
const devices = ref([])
const aiCases = ref([])

const form = reactive({
  app_config_id: null,
  device_id: null,
  action: 'aiAct',
  prompt: '',
  selector_hint: '',
  value: '',
  launch_app: true
})

const running = ref(false)
const aborting = ref(false)
const screenshot = ref('')
const logs = ref([])
const resultError = ref('')

// 保存为公共步骤（业务动作）
// NEW_CASE_VALUE 用作「新建用例」的哨兵值，后端收到 ai_case_id 为空即创建
const NEW_CASE_VALUE = '__new_case__'
const ACTION_CATEGORIES = [
  { value: 'precondition', label: '前置准备', desc: '登录 / 切账号 / 开权限' },
  { value: 'business', label: '业务操作', desc: '下单 / 搜索 / 加入购物车' },
  { value: 'extract', label: '数据提取', desc: '问页面要数据写入上下文' },
  { value: 'teardown', label: '收尾清理', desc: '退出登录 / 清缓存' },
  { value: 'other', label: '其他', desc: '暂未归类' }
]
const saveActionDialogVisible = ref(false)
const savingAction = ref(false)

// 粗略估算用户这段话里包含几个操作，用于提示"建议拆成多步"。
// 只做启发式计数（不调模型，避免打字时反复请求）：按顿号/逗号/箭头/换行/序号切分，
// 再加上"点击/输入/选择/进入/返回/启动/打开"这类动作动词的出现次数，取较大值。
// 只是为了引导用户发现「AI 拆成多步」，数字不精确也没关系。
const ACTION_VERBS = ['点击', '点按', '输入', '选择', '进入', '返回', '启动', '打开',
  '关闭', '滑动', '长按', '勾选', '切换', '添加', '删除', '保存', '提交', '完成']

const detectedActions = computed(() => {
  const text = (form.prompt || '').trim()
  if (!text) return 0
  // 分隔符切分：中文顿号/逗号、英文逗号、分号、箭头、换行、编号
  const parts = text
    .split(/[，,、;；\n\r]+|->|→|(?:\d+[.、)）])/)
    .map((s) => s.trim())
    .filter(Boolean)
  let verbHits = 0
  for (const v of ACTION_VERBS) {
    verbHits += text.split(v).length - 1
  }
  // 取较大值：有时用户不写分隔符，只连写动词
  return Math.max(parts.length, verbHits)
})

// ---- AI 拆成多步：把上面那段指令拆成多条原子步骤，预览确认后批量写入 ----
const generateDialogVisible = ref(false)
const generatingSteps = ref(false)
const savingGenerated = ref(false)
const generatedSteps = ref([])
const generateForm = reactive({ ai_case_id: '', new_case_name: '' })

const openGenerateDialog = async () => {
  if (!form.prompt.trim()) {
    ElMessage.warning('请先填写自然语言指令')
    return
  }
  generateDialogVisible.value = true
  generateForm.ai_case_id = ''
  generateForm.new_case_name = ''
  await doGenerateSteps()
}

const doGenerateSteps = async () => {
  generatingSteps.value = true
  try {
    const res = await generateCaseSteps({
      text: form.prompt,
      app_config: form.app_config_id || undefined
    })
    const data = res.data || {}
    if (!data.success) {
      ElMessage.error(data.message || '生成失败')
      return
    }
    const steps = Array.isArray(data.steps) ? data.steps : []
    if (!steps.length) {
      ElMessage.warning('没有拆出可用步骤，请把指令写得更具体些')
      return
    }
    generatedSteps.value = steps.map((s) => ({
      action: s.action || 'aiAct',
      name: s.name || '',
      prompt: s.prompt || '',
      value: s.value || '',
      wait_ms: s.wait_ms || 0
    }))
    ElMessage.success(`已拆成 ${steps.length} 条步骤，确认后写入`)
  } catch (error) {
    ElMessage.error(error?.response?.data?.message || '生成失败，请检查模型配置是否可用')
  } finally {
    generatingSteps.value = false
  }
}

// 这些动作天然不需要指令，占位文案要跟着变，否则用户以为必须填
const PROMPTLESS_ACTIONS = ['back', 'home', 'wait', 'screenshot']
const promptOptional = (action) => PROMPTLESS_ACTIONS.includes(action)

const appendStep = () => {
  generatedSteps.value.push({
    action: 'aiAct', name: '', prompt: '', value: '', wait_ms: 0
  })
}

const moveStep = (index, delta) => {
  const target = index + delta
  if (target < 0 || target >= generatedSteps.value.length) return
  const list = generatedSteps.value
  const [item] = list.splice(index, 1)
  list.splice(target, 0, item)
}

const saveGeneratedSteps = async () => {
  const isNewCase = generateForm.ai_case_id === NEW_CASE_VALUE
  if (!generateForm.ai_case_id) {
    ElMessage.warning('请选择目标用例，或选择「+ 新建用例」')
    return
  }
  if (isNewCase && !generateForm.new_case_name.trim()) {
    ElMessage.warning('请填写新用例名称')
    return
  }
  // 只丢掉"既没指令、动作又需要指令"的空步骤；
  // back/home/wait/screenshot 本来就没指令，不能一起被过滤掉。
  const steps = generatedSteps.value.filter(
    (s) => (s.prompt || '').trim() || promptOptional(s.action)
  )
  if (!steps.length) {
    ElMessage.warning('所有步骤的指令都是空的，没有可写入的内容')
    return
  }

  savingGenerated.value = true
  try {
    const res = await saveMidsceneStep({
      ai_case_id: isNewCase ? null : generateForm.ai_case_id,
      new_case_name: isNewCase ? generateForm.new_case_name.trim() : '',
      app_config_id: form.app_config_id || null,
      device_id: form.device_id || null,
      project_id: form.project_id || null,
      // 批量写入：后端一次性 append，避免中途失败写出半截用例
      steps: steps.map((s) => ({
        action: s.action,
        prompt: s.prompt,
        name: s.name,
        value: s.value,
        wait_ms: s.wait_ms || 0
      }))
    })
    ElMessage.success(res.data?.message || '已写入用例步骤')
    if (res.data?.created_case) {
      await loadOptions()
    }
    generateDialogVisible.value = false
  } catch (e) {
    console.error('批量写入失败', e)
    ElMessage.error('写入失败: ' + (e.response?.data?.error || e.message))
  } finally {
    savingGenerated.value = false
  }
}
const saveActionForm = reactive({
  name: '',
  category: 'business',
  description: '',
  action: 'aiAct',
  prompt: '',
  selector_hint: '',
  value: ''
})

// 追加到用例（沉淀为 midscene 结构化步骤）
// ai_case_id 为空 + new_case_name 有值 = 现场新建一条独立用例
const saveStepDialogVisible = ref(false)
const savingStep = ref(false)
const saveStepForm = reactive({
  ai_case_id: null,
  name: '',
  new_case_name: ''
})

const promptExamples = [
  '点击右下角的「我的」Tab，进入个人中心',
  '在顶部搜索框输入「天气」并搜索',
  '点击页面中间的「立即体验」按钮',
  '返回上一页'
]

const loadOptions = async () => {
  try {
    const [cfgRes, devRes, caseRes] = await Promise.all([
      getAppConfigs({ page_size: 200 }),
      getAppDevices({ page_size: 200 }),
      getAICases({ page_size: 200 })
    ])
    appConfigs.value = cfgRes.data.results || []
    devices.value = devRes.data.results || []
    aiCases.value = caseRes.data.results || []
  } catch (e) {
    console.error('加载应用/设备失败', e)
    ElMessage.error('加载应用配置或设备失败')
  }
}

const runPlayground = async () => {
  running.value = true
  screenshot.value = ''
  logs.value = []
  resultError.value = ''
  try {
    const res = await api.post('/ui-automation/ai-cases/midscene-playground/', {
      app_config_id: form.app_config_id,
      device_id: form.device_id,
      action: form.action,
      prompt: form.prompt,
      launch_app: form.launch_app
    }, {
      // 连真机执行（含 Midscene 首次自举 npm install）可能耗时数分钟，单独放宽超时。
      // 这里再兜一层「无响应」保护：真机/WDA 不通时后端 RPC 可能长时间无返回，
      // 15 分钟后强制给出可见失败原因，避免界面永远停在「执行中」。
      timeout: 900000
    })
    const data = res.data || {}
    screenshot.value = data.screenshot_base64 || ''
    logs.value = data.logs || []
    if (data.query_result !== undefined) {
      logs.value.push('[aiQuery 返回] ' + JSON.stringify(data.query_result))
    }
    if (data.status === 'aborted') {
      // 人工终止不是缺陷，用 warning，避免被误判成指令有问题
      ElMessage.warning('执行已终止')
    } else if (data.status === 'failed') {
      resultError.value = data.error || '未知错误'
    } else {
      ElMessage.success('执行成功')
    }
  } catch (e) {
    console.error('Playground 执行失败', e)
    resultError.value = e.response?.data?.error || e.message
    ElMessage.error('执行失败: ' + (e.response?.data?.error || e.message))
  } finally {
    running.value = false
    aborting.value = false
  }
}

// 终止执行：只发终止请求，不主动结束 running ——
// 让原来那个执行请求自己返回（它会带回 aborted 状态和日志），
// 这样用户能看到"被终止"而不是一个悬空的界面。
const abortPlayground = async () => {
  try {
    await ElMessageBox.confirm(
      '确认终止当前执行？会强制停止真机操作（未完成的指令不会执行完）。',
      '终止执行',
      { type: 'warning', confirmButtonText: '确认终止', cancelButtonText: '取消' }
    )
  } catch (e) {
    return  // 用户取消
  }
  aborting.value = true
  try {
    const res = await api.post('/ui-automation/ai-cases/midscene-playground/abort/')
    ElMessage.success(res.data?.message || '已终止执行')
    logs.value.push('[终止] ' + (res.data?.message || '已发送终止信号'))
  } catch (e) {
    ElMessage.error('终止失败: ' + (e.response?.data?.message || e.message))
  } finally {
    aborting.value = false
  }
}

const openSaveActionDialog = () => {
  saveActionForm.name = ''
  saveActionForm.category = 'business'
  saveActionForm.description = ''
  saveActionForm.action = form.action
  saveActionForm.prompt = form.prompt
  // aiInput 时把当前表单的输入值与定位提示带上，避免清空丢失
  saveActionForm.selector_hint = form.selector_hint || ''
  saveActionForm.value = form.value || ''
  saveActionDialogVisible.value = true
}

const saveBusinessAction = async () => {
  if (!saveActionForm.name.trim()) {
    ElMessage.warning('请填写名称')
    return
  }
  if (!saveActionForm.prompt.trim()) {
    ElMessage.warning('语义指令不能为空')
    return
  }
  savingAction.value = true
  try {
    await createBusinessAction({
      name: saveActionForm.name.trim(),
      category: saveActionForm.category,
      description: saveActionForm.description,
      action: saveActionForm.action,
      prompt: saveActionForm.prompt,
      selector_hint: saveActionForm.selector_hint,
      value: saveActionForm.value
    })
    ElMessage.success('已保存为公共步骤: ' + saveActionForm.name.trim() + '，可在「公共步骤库」查看')
    saveActionDialogVisible.value = false
  } catch (e) {
    console.error('保存公共步骤失败', e)
    ElMessage.error('保存失败: ' + (e.response?.data?.name?.[0] || e.response?.data?.message || e.message))
  } finally {
    savingAction.value = false
  }
}

const openSaveStepDialog = () => {
  saveStepForm.ai_case_id = null
  saveStepForm.name = ''
  saveStepForm.new_case_name = ''
  saveStepDialogVisible.value = true
}

const saveStepToCase = async () => {
  const isNewCase = saveStepForm.ai_case_id === NEW_CASE_VALUE
  if (!saveStepForm.ai_case_id) {
    ElMessage.warning('请选择目标用例，或选择「+ 新建用例」')
    return
  }
  if (isNewCase && !saveStepForm.new_case_name.trim()) {
    ElMessage.warning('请填写新用例名称')
    return
  }
  savingStep.value = true
  try {
    const res = await saveMidsceneStep({
      // 新建用例时不传 ai_case_id，后端会创建并写入；设备/应用配置沿用当前选择
      ai_case_id: isNewCase ? null : saveStepForm.ai_case_id,
      new_case_name: isNewCase ? saveStepForm.new_case_name.trim() : '',
      app_config_id: form.app_config_id || null,
      device_id: form.device_id || null,
      project_id: form.project_id || null,
      action: form.action,
      prompt: form.prompt,
      selector_hint: form.selector_hint || '',
      value: form.value || '',
      name: saveStepForm.name
    })
    ElMessage.success(res.data?.message || '已写入用例步骤')
    if (res.data?.created_case) {
      await loadOptions()
    }
    saveStepDialogVisible.value = false
  } catch (e) {
    console.error('写入步骤失败', e)
    ElMessage.error('写入失败: ' + (e.response?.data?.error || e.message))
  } finally {
    savingStep.value = false
  }
}

onMounted(loadOptions)
</script>

<style lang="scss" scoped>
.midscene-playground {
  .page-header {
    margin-bottom: 16px;
    h1 { margin: 0 0 4px; font-size: 22px; }
    .subtitle { margin: 0; color: #888; font-size: 13px; }
  }
  .form-hint { font-size: 12px; color: #909399; line-height: 1.4; margin-top: 4px; }
  .pg-layout {
    display: flex;
    flex-wrap: wrap;   /* 窄屏时预览列换到下一行，而不是把编辑面板挤变形 */
    gap: 16px;
    align-items: flex-start;
  }
  .pg-panel {
    flex: 1;
    min-width: 340px;
    max-width: 480px;
    background: #fff;
    border-radius: 8px;
    padding: 18px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);

    /* 按钮不铺满时不要被 el-button 默认的相邻 margin 撑破 */
    :deep(.el-button + .el-button) {
      margin-left: 0;
    }
  }

  /* 窄屏（笔记本半屏 / 分屏）：上下堆叠，面板占满宽度，
     按钮在大屏两行、小屏自然换行，不会溢出面板。 */
  @media (max-width: 900px) {
    .pg-layout { flex-direction: column; }
    .pg-panel,
    .pg-preview {
      min-width: 0;
      max-width: 100%;
      width: 100%;
    }
  }

  /* 动作类按钮一行，聊得下就并排、聊不下自动换行（面板最窄 360px） */
  .pg-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;

    :deep(.el-button) {
      margin-left: 0;
    }
  }

  /* 拆步按钮单独一行、撑满，作为最主要的推荐动作 */
  .gen-btn {
    width: 100%;
    margin-top: 8px;
    margin-left: 0;
  }
  .prompt-examples {
    margin-top: 8px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    .examples-label { color: #888; font-size: 12px; }
    .example-tag {
      cursor: pointer;
      user-select: none;
      &:hover { opacity: 0.8; }
    }
  }
  .prompt-warn {
    margin-top: 8px;
    font-size: 12px;
    color: #e6a23c;
  }
  .pg-logs {
    margin-top: 16px;
    .logs-title { font-weight: 600; margin-bottom: 6px; }
    .logs-content {
      background: #1e1e1e;
      color: #d4d4d4;
      padding: 10px;
      border-radius: 6px;
      font-size: 12px;
      max-height: 220px;
      overflow: auto;
      white-space: pre-wrap;
    }
  }
  .pg-preview {
    flex: 1;
    min-width: 320px;
    background: #fff;
    border-radius: 8px;
    padding: 18px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
    .preview-title { font-weight: 600; margin-bottom: 12px; }
    .preview-body { text-align: center; }
    .preview-img {
      max-width: 100%;
      border: 1px solid #eee;
      border-radius: 8px;
    }
    .preview-empty {
      height: 320px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      color: #bbb;
      border: 1px dashed #ddd;
      border-radius: 8px;
      p { margin-top: 10px; }
    }
  }
}

/* AI 拆步预览：每条步骤可单独改名/改指令/删除，确认后再写入 */
.gen-steps {
  max-height: 320px;
  overflow-y: auto;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 10px;
  background: #fafafa;

  .gen-step {
    background: #fff;
    border: 1px solid #ebeef5;
    border-radius: 4px;
    padding: 8px;
    margin-bottom: 8px;

    &:last-child { margin-bottom: 0; }
  }

  .gen-step-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  .gen-no {
    font-size: 12px;
    font-weight: 600;
    color: #409eff;
    min-width: 48px;
  }

  .gen-step-ops {
    margin-left: auto;
    display: flex;
    align-items: center;
  }
}

.gen-empty {
  text-align: center;
  padding: 8px 0;
  border: 1px dashed #ebeef5;
  border-radius: 6px;
}

/* A：输入框下方的实时拆步提示 */
.split-hint {
  margin-top: 8px;
  padding: 8px 10px;
  background: #f0f9eb;
  border: 1px solid #e1f3d8;
  border-radius: 4px;
  font-size: 12px;
  color: #529b2e;
  line-height: 1.6;

  strong { color: #67c23a; }
}
</style>
