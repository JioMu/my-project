<template>
  <div class="aimode-model-config">
    <div class="page-header">
      <div>
        <h2>模型配置</h2>
        <p class="sub">
          仅用于「AI 智能模式」模块（APK 智能回归 / Midscene 真机执行）。
          与「配置中心 → AI 模式」是两套独立配置，互不影响。
        </p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
    </div>

    <el-alert type="info" :closable="false" show-icon class="tip">
      <template #title>
        两个角色必须分开配：分析用文本模型（便宜快即可），执行用<b>多模态视觉模型</b>（必须能看图）。
      </template>
    </el-alert>

    <div class="role-grid">
      <el-card
        v-for="role in roles"
        :key="role.value"
        shadow="never"
        class="role-card"
        :class="{ missing: !activeMap[role.value]?.configured }"
      >
        <template #header>
          <div class="role-head">
            <div class="role-title">
              <span class="role-name">{{ role.label }}</span>
              <el-tag v-if="role.need_vision" type="warning" size="small">需多模态</el-tag>
            </div>
            <el-tag
              :type="activeMap[role.value]?.configured ? 'success' : 'danger'"
              size="small"
            >
              {{ activeMap[role.value]?.configured ? '已就绪' : '未配置' }}
            </el-tag>
          </div>
        </template>

        <p class="role-desc">{{ role.description }}</p>

        <div v-if="activeMap[role.value]?.configured" class="active-box">
          <div class="active-line">
            <span class="k">当前生效</span>
            <span class="v">{{ activeMap[role.value].config.name }}</span>
          </div>
          <div class="active-line">
            <span class="k">模型</span>
            <span class="v mono">{{ activeMap[role.value].config.model_name }}</span>
          </div>
          <div class="active-line">
            <span class="k">地址</span>
            <span class="v mono small">{{ activeMap[role.value].config.resolved_base_url }}</span>
          </div>
          <div v-if="role.need_vision" class="active-line">
            <span class="k">Family</span>
            <span class="v mono">{{ activeMap[role.value].config.resolved_family || '（自动）' }}</span>
          </div>
        </div>
        <el-alert
          v-else
          type="warning"
          :closable="false"
          :title="activeMap[role.value]?.error || '尚未配置启用模型'"
          class="mini-alert"
        />

        <div class="config-list">
          <div v-for="c in configsByRole(role.value)" :key="c.id" class="config-row">
            <div class="row-main">
              <el-switch
                :model-value="c.is_active"
                :loading="c._toggling"
                @change="(v) => toggleActive(c, v)"
              />
              <span class="row-name" :class="{ dim: !c.is_active }">{{ c.name }}</span>
              <span class="row-model mono">{{ c.model_name }}</span>
            </div>
            <div class="row-actions">
              <el-button link size="small" :loading="c._testing" @click="testExisting(c)">
                测试
              </el-button>
              <el-button link size="small" @click="openEdit(c)">编辑</el-button>
              <el-button link size="small" type="danger" @click="remove(c)">删除</el-button>
            </div>
          </div>
          <div v-if="!configsByRole(role.value).length" class="empty-row">暂无配置</div>
        </div>

        <el-button class="add-btn" :icon="Plus" @click="openCreate(role.value)">
          添加{{ role.label }}模型
        </el-button>
      </el-card>
    </div>

    <!-- 新增/编辑 -->
    <el-dialog
      v-model="dialogVisible"
      :title="form.id ? '编辑模型配置' : '新增模型配置'"
      width="640px"
      @closed="resetForm"
    >
      <el-form label-width="120px" label-position="right">
        <el-form-item label="配置名称" required>
          <el-input v-model="form.name" placeholder="例如：APK 分析-DeepSeek" />
        </el-form-item>

        <el-form-item label="用途角色" required>
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="r in roles" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
          <div class="hint">{{ currentRoleDesc }}</div>
        </el-form-item>

        <el-form-item label="模型提供商" required>
          <el-select v-model="form.model_type" style="width: 100%" @change="onProviderChange">
            <el-option
              v-for="m in modelTypes"
              :key="m.value"
              :label="m.label"
              :value="m.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="模型名称" required>
          <el-input v-model="form.model_name" placeholder="deepseek-chat / qwen3.7-plus" />
          <div class="hint">
            视觉角色请填多模态模型（如 qwen3.7-plus、glm-4v、doubao-vision），
            纯文本模型在真机执行时必然失败。
          </div>
        </el-form-item>

        <el-form-item label="API Base URL">
          <el-input
            v-model="form.base_url"
            :placeholder="form.model_type ? (defaultBaseUrls[form.model_type] || 'https://your-host/v1') : 'https://your-host/v1'"
          />
          <div class="hint">留空则用该提供商的默认地址</div>
        </el-form-item>

        <el-form-item label="API Key" :required="!form.id">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="form.id ? '留空表示不修改' : 'sk-...'"
          />
        </el-form-item>

        <el-form-item v-if="form.role === 'vision'" label="Midscene Family">
          <el-input v-model="form.model_family" placeholder="留空自动推断（推荐）" />
          <div class="hint">
            可选值：qwen3-vl / qwen2.5-vl / doubao-vision / gemini / glm-v / gpt-5。
            写错会被 midscene 判为非法值，拿不准就留空。
          </div>
        </el-form-item>

        <el-form-item label="最大 Token">
          <el-input-number v-model="form.max_tokens" :min="256" :max="65536" :step="512" />
        </el-form-item>

        <el-form-item label="温度">
          <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" :precision="2" />
        </el-form-item>

        <el-form-item label="启用">
          <el-switch v-model="form.is_active" />
          <span class="hint inline">启用后将替换该角色当前生效的模型</span>
        </el-form-item>

        <el-form-item label="备注">
          <el-input v-model="form.notes" type="textarea" :rows="2" />
        </el-form-item>

        <el-alert
          v-if="testResult"
          :type="testResult.success ? 'success' : 'error'"
          :closable="false"
          show-icon
          :title="testResult.message"
          class="test-result"
        />
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button :loading="testing" @click="testForm">测试连接</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Plus } from '@element-plus/icons-vue'
import {
  getRoleMeta, listConfigs, createConfig, updateConfig, deleteConfig,
  testUnsaved, testSaved, getActiveConfigs
} from '@/api/ai-mode-model'

const roles = ref([])
const modelTypes = ref([])
const defaultBaseUrls = ref({})
const configs = ref([])
const activeMap = ref({})
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const dialogVisible = ref(false)
const testResult = ref(null)

const emptyForm = () => ({
  id: null, name: '', role: 'analysis', model_type: 'deepseek', model_name: '',
  base_url: '', api_key: '', model_family: '', max_tokens: 4096,
  temperature: 0.3, is_active: true, notes: ''
})
const form = reactive(emptyForm())

const currentRoleDesc = computed(() => {
  const r = roles.value.find(x => x.value === form.role)
  return r ? r.description : ''
})

const configsByRole = (role) => configs.value.filter(c => c.role === role)

function unwrap(res, key = 'results') {
  const d = res?.data
  if (d?.[key] !== undefined) return d[key]
  if (d?.data?.[key] !== undefined) return d.data[key]
  return Array.isArray(d) ? d : []
}

async function loadAll() {
  loading.value = true
  try {
    const [metaRes, listRes, activeRes] = await Promise.all([
      getRoleMeta(), listConfigs(), getActiveConfigs()
    ])
    const meta = metaRes?.data || {}
    roles.value = meta.roles || []
    modelTypes.value = meta.model_types || []
    defaultBaseUrls.value = meta.default_base_urls || {}
    configs.value = unwrap(listRes).map(c => ({ ...c, _testing: false, _toggling: false }))
    activeMap.value = activeRes?.data || {}
  } catch (e) {
    ElMessage.error('加载模型配置失败')
  } finally {
    loading.value = false
  }
}

function resetForm() {
  Object.assign(form, emptyForm())
  testResult.value = null
}

function openCreate(role) {
  resetForm()
  form.role = role
  // 视觉角色默认给个多模态模型，避免用户直接选了纯文本模型
  if (role === 'vision') {
    form.model_type = 'qwen'
    form.model_name = 'qwen3.7-plus'
  }
  dialogVisible.value = true
}

function openEdit(c) {
  resetForm()
  Object.assign(form, {
    id: c.id, name: c.name, role: c.role, model_type: c.model_type,
    model_name: c.model_name, base_url: c.base_url || '', api_key: '',
    model_family: c.model_family || '', max_tokens: c.max_tokens,
    temperature: c.temperature, is_active: c.is_active, notes: c.notes || ''
  })
  dialogVisible.value = true
}

function onProviderChange(v) {
  if (!form.base_url) form.base_url = defaultBaseUrls.value[v] || ''
}

async function testForm() {
  if (!form.model_name) {
    ElMessage.warning('请先填写模型名称')
    return
  }
  testing.value = true
  testResult.value = null
  try {
    const res = await testUnsaved({ ...form })
    const d = res?.data || {}
    testResult.value = { success: !!d.success, message: d.message || (d.error || '未知结果') }
  } catch (e) {
    testResult.value = { success: false, message: '测试请求失败' }
  } finally {
    testing.value = false
  }
}

async function save() {
  if (!form.name || !form.model_name) {
    ElMessage.warning('配置名称与模型名称必填')
    return
  }
  if (!form.id && !form.api_key) {
    ElMessage.warning('新建时 API Key 必填')
    return
  }
  saving.value = true
  try {
    const payload = { ...form }
    delete payload.id
    if (form.id) {
      if (!payload.api_key) delete payload.api_key
      await updateConfig(form.id, payload)
    } else {
      await createConfig(payload)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.error || '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggleActive(c, val) {
  c._toggling = true
  try {
    await updateConfig(c.id, { is_active: val })
    await loadAll()
    ElMessage.success(val ? `已启用「${c.name}」` : '已禁用')
  } catch (e) {
    ElMessage.error('切换失败')
  } finally {
    c._toggling = false
  }
}

async function testExisting(c) {
  c._testing = true
  try {
    const res = await testSaved(c.id)
    const d = res?.data || {}
    if (d.success) ElMessage.success(d.message || '连接成功')
    else ElMessage.error(d.message || d.error || '连接失败')
  } catch (e) {
    ElMessage.error('测试失败')
  } finally {
    c._testing = false
  }
}

async function remove(c) {
  try {
    await ElMessageBox.confirm(`确定删除配置「${c.name}」？`, '删除确认', { type: 'warning' })
  } catch (e) {
    return
  }
  try {
    await deleteConfig(c.id)
    ElMessage.success('已删除')
    await loadAll()
  } catch (e) {
    ElMessage.error('删除失败')
  }
}

onMounted(loadAll)
</script>

<style scoped lang="scss">
.aimode-model-config {
  padding: 20px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 14px;

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

.tip {
  margin-bottom: 16px;
}

.role-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 16px;
}

.role-card {
  &.missing {
    border-color: #f5c2c0;
  }
}

.role-head {
  display: flex;
  align-items: center;
  justify-content: space-between;

  .role-title {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .role-name {
    font-weight: 600;
  }
}

.role-desc {
  margin: 0 0 12px;
  color: #5c6370;
  font-size: 13px;
  line-height: 1.6;
}

.active-box {
  background: #f2fbf5;
  border: 1px solid #d6f0e0;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;

  .active-line {
    display: flex;
    font-size: 13px;
    padding: 2px 0;

    .k {
      color: #8a9099;
      width: 76px;
      flex: 0 0 76px;
    }

    .v {
      color: #1f2329;
      word-break: break-all;

      &.small {
        font-size: 12px;
      }
    }
  }
}

.mini-alert {
  margin-bottom: 12px;
}

.config-list {
  border-top: 1px dashed #eef0f3;
  padding-top: 10px;

  .config-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 0;
    gap: 8px;

    .row-main {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }

    .row-name {
      font-size: 13px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;

      &.dim {
        color: #a0a6ad;
      }
    }

    .row-model {
      font-size: 12px;
      color: #8a9099;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .row-actions {
      flex: 0 0 auto;
    }
  }

  .empty-row {
    color: #a0a6ad;
    font-size: 13px;
    padding: 6px 0;
  }
}

.add-btn {
  margin-top: 12px;
  width: 100%;
}

.hint {
  color: #8a9099;
  font-size: 12px;
  line-height: 1.6;
  margin-top: 2px;

  &.inline {
    margin-left: 10px;
  }
}

.mono {
  font-family: Consolas, Monaco, 'Courier New', monospace;
}

.test-result {
  margin-top: 4px;
}
</style>
