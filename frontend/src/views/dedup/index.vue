<template>
  <section class="page" data-module="dedup">
    <header class="page-head">
      <div>
        <h2>采集去重比对管理</h2>
        <p class="page-desc">维护采集去重的业务键定义、相似度算法与保留策略，为数据去重管控提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记去重策略</button>
        <button class="btn" type="button" @click="exportRows">导出采集去重比对清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集去重比对数据，可先登记去重策略</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集去重比对记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="rehearsal-panel">
      <header class="page-head">
        <div>
          <h3>去重预演</h3>
          <p class="page-desc">
            针对指定时间窗口与比对字段计算重复组，预演拟保留记录与排除依据；高风险或相似度落在边界区间的组会进入采集数据标注待复核清单，人工复核裁定优先于策略结果。
          </p>
        </div>
      </header>

      <form class="filter-bar" @submit.prevent="runRehearsal">
        <label class="filter-item">
          <span>策略编号</span>
          <input v-model="rehearsalForm.策略编号" placeholder="如 DEDU-0002" />
        </label>
        <label class="filter-item">
          <span>窗口开始</span>
          <input v-model="rehearsalForm.窗口开始" placeholder="留空取策略时间窗口" />
        </label>
        <label class="filter-item">
          <span>窗口结束</span>
          <input v-model="rehearsalForm.窗口结束" placeholder="默认同窗口开始" />
        </label>
        <label class="filter-item">
          <span>比对字段</span>
          <input v-model="rehearsalForm.比对字段" placeholder="留空取策略比对字段" />
        </label>
        <button class="btn primary" type="submit">发起预演</button>
      </form>
      <p v-if="rehearsalMessage" class="rehearsal-message">{{ rehearsalMessage }}</p>

      <template v-if="batch">
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">预演批次</span>
            <strong class="stat-value">{{ batch.预演编号 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">拟保留记录</span>
            <strong class="stat-value">{{ summary ? summary.拟保留记录数 : batch.拟保留数 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">待复核组</span>
            <strong class="stat-value">{{ summary ? summary.待复核组数 : batch.待复核组数 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已排除数量</span>
            <strong class="stat-value">{{ summary ? summary.已排除数量 : batch.已排除数 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">标注待办核对</span>
            <strong class="stat-value">{{ summary ? (summary.一致性 ? '一致' : '不一致') : '—' }}</strong>
          </article>
        </div>
        <p v-if="summary" class="rehearsal-message">{{ summary.一致性说明 }}</p>
        <p v-if="batch.空窗口" class="empty-state">该时间窗口内没有可比记录，未生成重复组</p>

        <div v-for="group in groups" :key="group.组编号" class="group-card">
          <header class="group-head">
            <label class="group-select">
              <input
                type="checkbox"
                :checked="selectedGroups.has(group.组编号)"
                :disabled="group.回执状态 === '已回执'"
                @change="toggleGroup(group.组编号)"
              />
              批量选择
            </label>
            <strong>{{ group.组编号 }}</strong>
            <span>业务键：{{ group.业务键 }}</span>
            <span>相似度：{{ group.相似度 }}</span>
            <span class="risk-tag" :data-risk="group.风险标记">{{ group.风险标记 }}</span>
            <span>复核状态：{{ group.复核状态 }}</span>
            <span>回执状态：{{ group.回执状态 }}</span>
            <button
              class="btn"
              type="button"
              :disabled="group.回执状态 === '已回执'"
              @click="receiptGroup(group)"
            >
              整组回执
            </button>
          </header>
          <table class="data-table">
            <thead>
              <tr>
                <th>保留选择</th>
                <th>记录编号</th>
                <th>比对内容</th>
                <th>排序值</th>
                <th>风险等级</th>
                <th>角色</th>
                <th>裁决依据</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="member in group.成员" :key="member.记录编号">
                <td>
                  <input
                    type="radio"
                    :name="`keep-${group.组编号}`"
                    :checked="keepChoices[group.组编号] === member.记录编号"
                    :disabled="group.回执状态 === '已回执'"
                    @change="setKeep(group.组编号, member.记录编号)"
                  />
                </td>
                <td>{{ member.记录编号 }}</td>
                <td>{{ member.比对内容 }}</td>
                <td>{{ member.排序值 }}</td>
                <td>{{ member.风险等级 }}</td>
                <td>{{ member.角色 }}</td>
                <td>{{ member.裁决依据 }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-if="groups.length" class="submit-bar">
          <label class="filter-item">
            <span>裁决人</span>
            <input v-model="reviewer" placeholder="复核员" />
          </label>
          <button class="btn primary" type="button" :disabled="!selectedGroups.size" @click="submitSelected">
            一次提交选中裁决（{{ selectedGroups.size }} 组）
          </button>
        </div>
      </template>

      <h4 class="batch-title">历史预演批次</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>预演编号</th>
            <th>策略编号</th>
            <th>时间窗口</th>
            <th>状态</th>
            <th>拟保留</th>
            <th>待复核组</th>
            <th>已排除</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in batches" :key="item.预演编号">
            <td>{{ item.预演编号 }}</td>
            <td>{{ item.策略编号 }}</td>
            <td>{{ item.窗口开始 }} ~ {{ item.窗口结束 }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.拟保留数 }}</td>
            <td>{{ item.待复核组数 }}</td>
            <td>{{ item.已排除数 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="loadBatch(item.id)">查看</button>
            </td>
          </tr>
          <tr v-if="!batches.length">
            <td colspan="8" class="empty-state">暂无预演批次，可先在上方发起去重预演</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type AnyRow = Record<string, any>

const ENDPOINT = '/api/dedup'
const columns = ["策略编号", "业务键列", "排序字段", "保留策略", "时间窗口", "相似阈值", "比对字段", "是否启用"]
const actions = ["提交校验", "启用策略", "停用策略"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const stats = [{"label": "去重策略总数", "value": 0}, {"label": "待校验策略", "value": 0}, {"label": "已启用策略", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const rehearsalForm = ref<Record<string, string>>({ 策略编号: 'DEDU-0002', 窗口开始: '', 窗口结束: '', 比对字段: '' })
const rehearsalMessage = ref('')
const batch = ref<AnyRow | null>(null)
const groups = ref<AnyRow[]>([])
const summary = ref<AnyRow | null>(null)
const batches = ref<AnyRow[]>([])
const selectedGroups = ref<Set<string>>(new Set())
const keepChoices = ref<Record<string, string>>({})
const reviewer = ref('复核员')

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '去重策略登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集去重比对动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集去重比对操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('去重策略列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集去重比对列表读取失败'
  }
}

async function runRehearsal() {
  rehearsalMessage.value = ''
  try {
    const values: Record<string, string> = {}
    for (const [key, value] of Object.entries(rehearsalForm.value)) {
      if (value.trim()) values[key] = value.trim()
    }
    const response = await request(`${ENDPOINT}/rehearsals`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    rehearsalMessage.value = payload.message ?? ''
    if (payload.ok && payload.entry?.id) {
      await loadBatch(payload.entry.id)
    }
    await loadBatches()
  } catch (error) {
    rehearsalMessage.value = error instanceof Error ? error.message : '去重预演发起失败'
  }
}

async function loadBatch(batchId: number) {
  rehearsalMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/rehearsals/${batchId}`)
    if (!response.ok) {
      throw new Error('预演批次读取失败')
    }
    const payload = await response.json()
    batch.value = payload.batch
    groups.value = payload.groups ?? []
    const choices: Record<string, string> = {}
    for (const group of groups.value) {
      choices[group.组编号] = group.拟保留记录
    }
    keepChoices.value = choices
    selectedGroups.value = new Set()
    await loadSummary(batchId)
  } catch (error) {
    rehearsalMessage.value = error instanceof Error ? error.message : '预演批次读取失败'
  }
}

async function loadSummary(batchId: number) {
  try {
    const response = await request(`${ENDPOINT}/rehearsals/${batchId}/summary`)
    if (response.ok) {
      summary.value = await response.json()
    }
  } catch {
    summary.value = null
  }
}

async function loadBatches() {
  try {
    const response = await request(`${ENDPOINT}/rehearsals?size=10`)
    if (response.ok) {
      const payload = await response.json()
      batches.value = payload.items ?? []
    }
  } catch {
    batches.value = []
  }
}

function toggleGroup(groupNo: string) {
  const next = new Set(selectedGroups.value)
  if (next.has(groupNo)) {
    next.delete(groupNo)
  } else {
    next.add(groupNo)
  }
  selectedGroups.value = next
}

function setKeep(groupNo: string, recordNo: string) {
  keepChoices.value = { ...keepChoices.value, [groupNo]: recordNo }
}

async function submitDecisions(decisions: AnyRow[]) {
  if (!batch.value) return
  rehearsalMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/rehearsals/${batch.value.id}/adjudicate`, {
      method: 'POST',
      body: JSON.stringify({ values: { decisions, 裁决人: reviewer.value || '复核员' } }),
    })
    const payload = await response.json()
    rehearsalMessage.value = payload.message ?? ''
    await loadBatch(batch.value.id)
    await loadBatches()
  } catch (error) {
    rehearsalMessage.value = error instanceof Error ? error.message : '裁决提交失败'
  }
}

function receiptGroup(group: AnyRow) {
  const keep = keepChoices.value[group.组编号] ?? group.拟保留记录
  void submitDecisions([{ 组编号: group.组编号, 整组确认: true, 保留记录: keep }])
}

function submitSelected() {
  const decisions = [...selectedGroups.value].map((groupNo) => ({
    组编号: groupNo,
    保留记录: keepChoices.value[groupNo],
  }))
  void submitDecisions(decisions)
}

onMounted(() => {
  void reload()
  void loadBatches()
})
</script>

<style scoped>
.rehearsal-panel {
  margin-top: 24px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.rehearsal-message {
  background: #eef4ff;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
}
.group-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
}
.group-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  flex-wrap: wrap;
}
.group-select {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--muted);
}
.risk-tag {
  border-radius: 4px;
  padding: 2px 6px;
  font-size: 12px;
  background: #eef2f7;
}
.risk-tag[data-risk='高风险'] {
  background: #fee4e2;
  color: #b42318;
}
.risk-tag[data-risk='边界相似'] {
  background: #fef0c7;
  color: #b54708;
}
.submit-bar {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin: 12px 0;
}
.batch-title {
  margin: 16px 0 8px;
}
</style>
