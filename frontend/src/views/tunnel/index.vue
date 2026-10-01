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
      <article class="stat-card offline-card" :class="{ active: pendingCount > 0 }">
        <span class="stat-label">{{ online ? '在线' : '离线' }} · 待重传命令</span>
        <strong class="stat-value">{{ pendingCount }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      <button class="btn ghost" type="button" @click="reload">刷新现场状态</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>未收口草稿</th>
          <th>设施待办</th>
          <th>检修单</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="digestFor(row)?.open_draft" class="draft-badge">
              草稿 #{{ digestFor(row)?.open_draft_ids.join('、#') }}
            </span>
            <span v-else class="muted-text">无（已收口）</span>
          </td>
          <td>{{ digestFor(row)?.todo_count ?? 0 }}</td>
          <td>{{ digestFor(row)?.work_order_count ?? 0 }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">现场详情/通风</button>
            <button class="link" type="button" @click="openFire(row)">消防工作表</button>
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
          <td :colspan="columns.length + 4" class="empty-state">暂无隧道管养数据，可先登记隧道</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条隧道管养记录（档案、待办、工单按同一 digest 口径核对）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { request } from '@/api/client'
import { useInspectionStore } from '@/stores/inspection'

type Row = Record<string, string | number | null>
interface Digest {
  open_draft: boolean
  open_draft_ids: number[]
  draft_count: number
  todo_count: number
  work_order_count: number
  evidence_version: number
}

const ENDPOINT = '/api/tunnel'
const columns = ["隧道编号", "隧道名称", "隧道长度", "通风方式", "照明方式", "消防设施", "最近定检", "隧道状态"]
const actions = ["设置限速", "安排维修", "封闭交通"]
const stats = [{"label": "正常隧道", "value": 0}, {"label": "限速隧道", "value": 0}, {"label": "维修隧道", "value": 0}]

const router = useRouter()
const inspection = useInspectionStore()

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const digestMap = ref<Record<string, Digest>>({})
const filterFields = columns.slice(0, 3)

const online = ref(navigator.onLine)
const pendingCount = ref(0)

function digestFor(row: Row): Digest | undefined {
  return digestMap.value[String(row['隧道编号'])]
}

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

function openDetail(row: Row) {
  void router.push({ path: '/tunnel/detail', query: { code: String(row['隧道编号']) } })
}

function openFire(row: Row) {
  void router.push({ path: '/tunnel/fire', query: { code: String(row['隧道编号']) } })
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
    await reloadDigest()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '隧道管养列表读取失败'
  }
}

async function reloadDigest() {
  // 与详情页、消防表共用同一后端口径：回写后未收口草稿立即从列表消失
  try {
    const response = await request('/api/tunnel/inspection/digest')
    if (response.ok) {
      const payload = await response.json() as { items: Record<string, Digest> }
      digestMap.value = payload.items
    }
  } catch {
    // digest 失败不阻塞列表主体，只保留上次口径
  }
  pendingCount.value = inspection.pendingCount
  online.value = inspection.online
}

async function onBackOnline() {
  online.value = true
  await inspection.flush()
  await reloadDigest()
}

onMounted(() => {
  inspection.bindNetwork()
  window.addEventListener('online', () => { void onBackOnline() })
  void reload()
})
// keep-alive 场景或从详情/消防表返回时重新拉取，确保不残留同一草稿
onActivated(() => { void reloadDigest() })
</script>

<style scoped>
.offline-card.active { border-color: #b54708; background: #fffaeb; }
.draft-badge { color: #b54708; font-size: 12px; white-space: nowrap; }
.muted-text { color: var(--muted); font-size: 12px; }
</style>
