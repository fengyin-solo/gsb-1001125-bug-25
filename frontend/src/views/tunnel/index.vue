<template>
  <section class="page" data-module="tunnel">
    <header class="page-head">
      <div>
        <h2>隧道管养管理</h2>
        <p class="page-desc">维护隧道，围绕隧道编号、隧道名称、隧道长度、通风方式做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记隧道</button>
        <button class="btn" type="button" @click="exportRows">导出隧道管养清单</button>
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
          <th>消防草稿 / 待办 / 工单</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <div class="draft-summary">
              <span v-for="item in worksheetMap[String(row['隧道编号'])] ?? []" :key="String(item.id)" :class="['badge', item.consistent ? 'ok' : 'warn']">
                {{ item.status }} · {{ item.todo_status ?? '无待办' }} · {{ item.work_order_no ?? '无工单' }}
              </span>
              <RouterLink v-if="!worksheetMap[String(row['隧道编号'])]?.length" class="link" :to="`/tunnel/${row.id}/fire`">
                新建消防作业
              </RouterLink>
            </div>
          </td>
          <td class="row-actions">
            <RouterLink class="link" :to="`/tunnel/${row.id}`">详情</RouterLink>
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
          <td :colspan="columns.length + 2" class="empty-state">暂无隧道管养数据，可先登记隧道</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条隧道管养记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { listLocalDrafts } from '@/views/tunnel/fireStore'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/tunnel'
const columns = ["隧道编号", "隧道名称", "隧道长度", "通风方式", "照明方式", "消防设施", "消防状态", "最近检修单号", "最近定检", "隧道状态"]
const actions = ["设置限速", "安排维修", "封闭交通"]
const statuses = ["正常", "限速", "维修", "封闭"]
const stats = [{"label": "正常隧道", "value": 0}, {"label": "限速隧道", "value": 0}, {"label": "维修隧道", "value": 0}]

const rows = ref<Row[]>([])
const worksheetDrafts = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const worksheetMap = computed<Record<string, Row[]>>(() => {
  const result: Record<string, Row[]> = {}
  worksheetDrafts.value.forEach((draft) => {
    const code = String(draft['隧道编号'] ?? draft.tunnel_code ?? '')
    if (!result[code]) result[code] = []
    result[code].push(draft)
  })
  return result
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '隧道登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('隧道管养动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '隧道管养操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('隧道列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await reloadWorksheets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '隧道管养列表读取失败'
  }
}

async function reloadWorksheets() {
  const localItems = listLocalDrafts().map((draft) => ({
    id: draft.serverId ?? draft.canonicalKey,
    canonical_key: draft.canonicalKey,
    tunnel_code: draft.tunnelCode,
    status: draft.workOrderNo ? '已回写' : draft.confirmed ? '已确认' : '本地草稿',
    consistent: Boolean(draft.workOrderNo),
    todo_status: draft.workOrderNo ? '待处理' : '未生成',
    work_order_no: draft.workOrderNo ?? null,
  }))
  try {
    const response = await request('/api/tunnel-worksheets?size=200')
    if (!response.ok) throw new Error('worksheet unavailable')
    const payload = await response.json()
    const serverKeys = new Set((payload.items ?? []).map((item: Row) => item.canonical_key))
    worksheetDrafts.value = [...(payload.items ?? []), ...localItems.filter((item) => !serverKeys.has(item.canonical_key))]
  } catch {
    worksheetDrafts.value = localItems
  }
}

onMounted(reload)
</script>
