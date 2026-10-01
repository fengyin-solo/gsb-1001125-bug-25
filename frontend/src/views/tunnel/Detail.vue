<template>
  <section class="page tunnel-detail" data-module="tunnel-detail">
    <header class="page-head">
      <div>
        <h2>隧道档案 · {{ tunnel?.['隧道名称'] ?? route.params.id }}</h2>
        <p class="page-desc">先核对通风笔记，再进入消防工作表；笔记在弱网切换时保留在本地草稿中。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn ghost" to="/tunnel">返回隧道列表</RouterLink>
        <button class="btn primary" type="button" :disabled="!tunnelCode" @click="goToFire">
          转消防工作表
        </button>
      </div>
    </header>

    <div v-if="errorMessage" class="inline-message error">{{ errorMessage }}</div>
    <div v-else-if="!tunnel" class="data-card">正在读取隧道档案……</div>

    <template v-else>
      <div class="detail-grid">
        <article class="data-card">
          <h3>档案信息</h3>
          <dl>
            <div><dt>隧道编号</dt><dd>{{ tunnel['隧道编号'] }}</dd></div>
            <div><dt>隧道名称</dt><dd>{{ tunnel['隧道名称'] }}</dd></div>
            <div><dt>隧道长度</dt><dd>{{ tunnel['隧道长度'] }}</dd></div>
            <div><dt>通风方式</dt><dd>{{ tunnel['通风方式'] }}</dd></div>
            <div><dt>消防设施</dt><dd>{{ tunnel['消防设施'] }}</dd></div>
            <div><dt>消防状态</dt><dd>{{ tunnel['消防状态'] ?? tunnel['隧道状态'] ?? '—' }}</dd></div>
            <div><dt>最近检修单</dt><dd>{{ tunnel['最近检修单号'] ?? '—' }}</dd></div>
          </dl>
        </article>

        <article class="data-card">
          <h3>现场通风笔记</h3>
          <label class="form-item">
            <span>检查员</span>
            <input v-model="operator" placeholder="请输入检查员姓名" />
          </label>
          <label class="form-item">
            <span>通风笔记</span>
            <textarea v-model="ventilationNote" rows="6" placeholder="记录风机、排烟、风量等现场情况"></textarea>
          </label>
          <label class="form-item">
            <span>本地序列号</span>
            <input v-model.number="localSeq" type="number" min="1" />
          </label>
          <div class="button-row">
            <button class="btn" type="button" @click="keepVentilationNote">核对并保留笔记</button>
            <button class="btn primary" type="button" @click="goToFire">带入消防表</button>
          </div>
          <p class="hint">保存键：{{ draftKey }}。离线重传始终按“隧道编号 + 本地序列号”幂等。</p>
        </article>
      </div>

      <article class="data-card">
        <h3>同隧道消防草稿、待办与工单</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>本地序列号</th><th>状态</th><th>检查员</th><th>附件</th><th>设施待办</th><th>工单号</th><th>一致性</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="draft in drafts" :key="draft.id">
              <td>{{ draft.local_seq }}</td>
              <td>{{ draft.status }}</td>
              <td>{{ draft.operator }}</td>
              <td>{{ draft.attachments?.length ?? 0 }}</td>
              <td>{{ draft.todo_status ?? '未生成' }}</td>
              <td>{{ draft.work_order_no ?? '—' }}</td>
              <td>
                <span :class="draft.consistent ? 'badge ok' : 'badge warn'">
                  {{ draft.consistent ? '一致' : '待同步' }}
                </span>
              </td>
            </tr>
            <tr v-if="!drafts.length">
              <td colspan="7" class="empty-state">暂无消防草稿</td>
            </tr>
          </tbody>
        </table>
      </article>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { request } from '@/api/client'
import { canonicalKey, loadDraft, saveDraft, type FireDraft } from '@/views/tunnel/fireStore'

type TunnelRow = Record<string, string | number | boolean | null>
interface DraftRow {
  id: number
  local_seq: number
  status: string
  operator: string
  ventilation_note?: string
  attachments?: unknown[]
  todo_status?: string
  work_order_no?: string
  consistent: boolean
}

const route = useRoute()
const router = useRouter()
const tunnelId = Number(route.params.id)

const tunnel = ref<TunnelRow | null>(null)
const drafts = ref<DraftRow[]>([])
const operator = ref('张检查')
const ventilationNote = ref('')
const localSeq = ref(1)
const errorMessage = ref('')

const tunnelCode = computed(() => String(tunnel.value?.['隧道编号'] ?? ''))
const draftKey = computed(() => (tunnelCode.value ? canonicalKey(tunnelCode.value, localSeq.value) : ''))

function hydrateFromLocal(code: string) {
  const local = loadDraft(code, localSeq.value)
  if (local) {
    operator.value = local.operator || operator.value
    ventilationNote.value = local.ventilationNote
    localSeq.value = local.localSeq
  }
}

function keepVentilationNote() {
  if (!tunnelCode.value) {
    errorMessage.value = '隧道档案尚未加载，不能保存现场笔记'
    return
  }
  const previous = loadDraft(tunnelCode.value, localSeq.value)
  saveDraft({
    ...(previous ?? emptyDraft(tunnelCode.value, localSeq.value)),
    tunnelCode: tunnelCode.value,
    localSeq: localSeq.value,
    operator: operator.value,
    ventilationNote: ventilationNote.value,
    sources: Array.from(new Set([...(previous?.sources ?? []), '隧道详情页通风笔记'])),
  })
  errorMessage.value = ''
}

function goToFire() {
  keepVentilationNote()
  if (!tunnelCode.value || !ventilationNote.value.trim()) {
    errorMessage.value = '请先核对并填写通风笔记，再转入消防工作表'
    return
  }
  router.push({
    name: 'tunnel-fire',
    query: { tunnel_code: tunnelCode.value, seq: String(localSeq.value) },
  })
}

function emptyDraft(tunnelCode: string, localSeq: number): FireDraft {
  return {
    canonicalKey: canonicalKey(tunnelCode, localSeq),
    tunnelCode,
    localSeq,
    operator: operator.value,
    ventilationNote: '',
    fireNote: '',
    fireResult: '',
    confirmed: false,
    attachments: [],
    sources: [],
    updatedAt: new Date().toISOString(),
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`/api/tunnel/${tunnelId}`)
    if (!response.ok) throw new Error('隧道详情读取失败')
    tunnel.value = await response.json()
    hydrateFromLocal(tunnelCode.value)

    const draftResponse = await request(`/api/tunnel-worksheets?tunnel_code=${encodeURIComponent(tunnelCode.value)}`)
    if (draftResponse.ok) {
      const payload = await draftResponse.json()
      drafts.value = payload.items ?? []
      const remote = drafts.value.find((item) => Number(item.local_seq) === localSeq.value)
      if (remote && !loadDraft(tunnelCode.value, localSeq.value)) {
        ventilationNote.value = String(remote.ventilation_note ?? '')
        operator.value = String(remote.operator ?? operator.value)
      }
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '隧道详情读取失败'
  }
}

watch(localSeq, () => tunnelCode.value && hydrateFromLocal(tunnelCode.value))
onMounted(reload)
</script>
