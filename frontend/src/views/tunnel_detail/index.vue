<template>
  <section class="page" data-module="tunnel-detail">
    <header class="page-head">
      <div>
        <h2>隧道现场详情 · {{ tunnelCode || '—' }}</h2>
        <p class="page-desc">先核对通风笔记：弱网下切换到消防工作表不会清空本页内容，重连按隧道编号+本地序列号续传。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" :to="`/tunnel`">返回隧道列表</RouterLink>
        <RouterLink class="btn primary" :to="fireLink">前往消防工作表</RouterLink>
      </div>
    </header>

    <div v-if="notFound" class="error-text">隧道不存在或已归档</div>
    <template v-else>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">隧道名称</span>
          <strong class="stat-value">{{ archive['隧道名称'] ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">通风方式</span>
          <strong class="stat-value">{{ archive['通风方式'] ?? '—' }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">证据版本</span>
          <strong class="stat-value">v{{ evidence.version }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待重传</span>
          <strong class="stat-value">{{ queued.length }}</strong>
        </article>
      </div>

      <article class="draft-card">
        <h3>通风笔记（现场核对）</h3>
        <label class="filter-item">
          <span>检查员</span>
          <input v-model="inspector" placeholder="检查员姓名" />
        </label>
        <label class="filter-item note-field">
          <span>现场通风记录</span>
          <textarea v-model="ventNote" rows="4" placeholder="如：射流风机2号异响，已复测风速"></textarea>
        </label>
        <div class="row-actions" style="margin: 8px 0">
          <button class="btn" type="button" :disabled="saving" @click="saveVent">暂存通风笔记（离线可用）</button>
          <button class="btn" type="button" :disabled="saving" @click="saveThenConfirm">核对无误并现场确认</button>
          <button class="btn ghost" type="button" @click="goFire">切到消防表（笔记已自动保留）</button>
        </div>
        <p v-if="message" :class="lastOk ? 'ok-text' : 'error-text'">{{ message }}</p>
        <p class="page-desc">本地草稿 #{{ draftId ?? '未分配' }} · 最近保存 {{ work.savedAt || '尚未保存' }}</p>
      </article>

      <article class="draft-card">
        <h3>消防检查项（来自消防工作表的同一份草稿）</h3>
        <p v-if="!fireContent" class="page-desc">消防表尚未填写，可从上方按钮前往；切换不会清掉通风笔记。</p>
        <p v-else>{{ fireContent }}</p>
      </article>

      <article class="draft-card">
        <h3>检修单与待办</h3>
        <table class="data-table">
          <thead>
            <tr><th>工单号</th><th>状态</th><th>通风</th><th>消防</th><th>附件（原快照）</th></tr>
          </thead>
          <tbody>
            <tr v-for="order in orders" :key="order.id">
              <td>{{ order.order_no }}</td>
              <td>{{ order.status }}</td>
              <td>{{ order.sections['通风']?.content || '—' }}</td>
              <td>{{ order.sections['消防']?.content || '—' }}</td>
              <td>{{ (order.attachments_snapshot || []).map((a: Attachment) => a.name).join('、') || '—' }}</td>
            </tr>
            <tr v-if="!orders.length"><td colspan="5" class="empty-state">暂无检修单</td></tr>
          </tbody>
        </table>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { request, fetchJson } from '@/api/client'
import { useInspectionStore } from '@/stores/inspection'

interface ArchiveRow {
  '隧道编号'?: string
  [key: string]: string | number | null | undefined
}
interface Attachment { name: string; sha256: string }
interface WorkOrder {
  id: number
  order_no: string
  status: string
  sections: Record<string, { content: string }>
  attachments_snapshot: Attachment[]
}
interface Evidence { version: number; sections: Record<string, { content: string }> }

const route = useRoute()
const router = useRouter()
const inspection = useInspectionStore()

const tunnelCode = String(route.query.code ?? '')
const archive = ref<ArchiveRow>({})
const evidence = ref<Evidence>({ version: 0, sections: {} })
const orders = ref<WorkOrder[]>([])
const notFound = ref(false)
const inspector = ref('张三')
const ventNote = ref('')
const saving = ref(false)
const message = ref('')
const lastOk = ref(false)

const work = computed(() => inspection.ensureWorking(tunnelCode, inspector.value))
const draftId = computed(() => work.value.draftId)
const fireContent = computed(() => work.value.sections['消防']?.content ?? '')
const queued = computed(() => inspection.pendingForTunnel(tunnelCode))
const fireLink = computed(() => ({ path: '/tunnel/fire', query: { code: tunnelCode } }))

watch(inspector, (value) => {
  inspection.ensureWorking(tunnelCode, value).inspector = value
})

async function reload() {
  try {
    const list = await fetchJson<{ items: ArchiveRow[] }>('/api/tunnel')
    archive.value = list.items.find((row) => String(row['隧道编号']) === tunnelCode) ?? {}
    if (!archive.value['隧道编号']) {
      notFound.value = true
      return
    }
    evidence.value = await fetchJson<Evidence>(`/api/tunnel/inspection/tunnels/${tunnelCode}/evidence`)
    orders.value = (await fetchJson<{ items: WorkOrder[] }>(
      `/api/tunnel/inspection/work-orders?tunnel_code=${encodeURIComponent(tunnelCode)}`,
    )).items
    const local = inspection.working[tunnelCode]
    if (local) ventNote.value = local.sections['通风']?.content ?? ''
  } catch {
    // 弱网读不到远端时仍展示本地草稿，不阻塞现场记录
    message.value = '远端暂不可达，已展示本机草稿'
  }
}

async function saveVent() {
  saving.value = true
  const result = await inspection.saveSection({
    tunnelCode,
    section: '通风',
    inspector: inspector.value,
    content: ventNote.value,
  })
  message.value = result.message
  lastOk.value = result.ok || result.enqueued
  saving.value = false
}

async function saveThenConfirm() {
  const saved = await inspection.saveSection({
    tunnelCode,
    section: '通风',
    inspector: inspector.value,
    content: ventNote.value,
  })
  if (saved.draftId) {
    // 现场确认：合并草稿时此版本优先
    try {
      const response = await request(`/api/tunnel/inspection/drafts/${saved.draftId}/confirm`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      if (!response.ok) throw new Error(`确认失败（${response.status}）`)
      const local = inspection.ensureWorking(tunnelCode, inspector.value)
      local.sections['通风'] = { content: ventNote.value, confirmed: true }
      message.value = '通风笔记已现场确认，合并时以本版为准'
      lastOk.value = true
    } catch {
      message.value = `${saved.message}；确认请求将在联网后补做`
      lastOk.value = true
    }
  } else {
    const local = inspection.ensureWorking(tunnelCode, inspector.value)
    local.sections['通风'] = { content: ventNote.value, confirmed: true }
    message.value = '已离线确认本机版本，联网续传后以该版参与合并'
    lastOk.value = true
  }
}

function goFire() {
  void router.push(fireLink.value)
}

onMounted(async () => {
  inspection.bindNetwork()
  await reload()
})
</script>

<style scoped>
.draft-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.draft-card h3 { margin: 0 0 8px; font-size: 15px; }
.note-field { display: block; margin: 8px 0; }
.note-field textarea { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font: inherit; }
.ok-text { color: #067647; }
</style>
