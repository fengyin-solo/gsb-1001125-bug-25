<template>
  <section class="page fire-worksheet" data-module="tunnel-fire">
    <header class="page-head">
      <div>
        <h2>隧道消防工作表</h2>
        <p class="page-desc">弱网下先把通风、消防记录和附件保存在本地；重连后按现场确认版本合并并幂等回写。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn ghost" :to="backLink">返回隧道详情</RouterLink>
        <RouterLink class="btn ghost" to="/tunnel">返回隧道列表</RouterLink>
      </div>
    </header>

    <div class="worksheet-layout">
      <form class="data-card form-panel" @submit.prevent="saveLocally">
        <div class="form-inline">
          <label class="form-item compact">
            <span>隧道编号</span>
            <input v-model="form.tunnelCode" @change="reloadRemote" />
          </label>
          <label class="form-item compact small">
            <span>本地序列号</span>
            <input v-model.number="form.localSeq" type="number" min="1" @change="reloadRemote" />
          </label>
          <label class="check-item">
            <input v-model="offline" type="checkbox" />
            模拟弱网离线
          </label>
          <button class="btn" type="button" @click="reloadRemote">读取服务端版本</button>
        </div>

        <label class="form-item">
          <span>检查员</span>
          <input v-model="form.operator" placeholder="现场检查员" />
        </label>
        <label class="form-item">
          <span>1. 核对通风笔记</span>
          <textarea v-model="form.ventilationNote" rows="5" placeholder="从隧道详情页带入的通风笔记"></textarea>
        </label>
        <label class="form-item">
          <span>2. 消防检查记录</span>
          <textarea v-model="form.fireNote" rows="5" placeholder="灭火器、消火栓、泡沫箱、联动报警等"></textarea>
        </label>
        <div class="form-inline">
          <label class="form-item compact">
            <span>消防结论</span>
            <select v-model="form.fireResult">
              <option value="">待确认</option>
              <option>正常</option>
              <option>限期整改</option>
              <option>停用整改</option>
            </select>
          </label>
          <label class="form-item compact">
            <span>附件</span>
            <input type="file" multiple @change="addAttachments" />
          </label>
        </div>

        <div class="attachment-list">
          <span v-for="item in form.attachments" :key="item.sha256" class="badge neutral">
            {{ item.name }} · {{ item.sha256.slice(0, 8) }}
          </span>
          <span v-if="!form.attachments.length" class="hint">暂无附件；附件摘要会与草稿、工单号在同一事务提交。</span>
        </div>

        <div class="button-row">
          <button class="btn" type="submit">保存本地草稿</button>
          <button class="btn" type="button" :disabled="offline" @click="syncDraft(false)">同步草稿</button>
          <button class="btn primary" type="button" @click="confirmAndMerge">合并现场草稿</button>
          <button class="btn primary" type="button" @click="writeBack">回写检修单</button>
          <button class="btn" type="button" :disabled="!canRetry" @click="retryAfterReconnect">
            重连后重传
          </button>
        </div>
        <p class="hint">幂等键：{{ form.canonicalKey }}；服务端修订号：{{ form.revision ?? '未同步' }}；状态：{{ form.status ?? '本地草稿' }}</p>
      </form>

      <aside class="data-card side-panel">
        <h3>同步与留痕</h3>
        <label class="form-item">
          <span>旧草稿来源</span>
          <input v-model="legacySource" placeholder="例如：2026-09 旧版消防 App" />
        </label>
        <button class="btn" type="button" @click="migrateLegacy">迁移旧草稿并保留来源</button>

        <h3>并发补写现场证据</h3>
        <input v-model="evidenceOperator" placeholder="补证人" />
        <textarea v-model="evidenceNote" rows="3" placeholder="只追加，不覆盖他人的现场笔记和附件"></textarea>
        <input type="file" multiple @change="addEvidenceFiles" />
        <button class="btn" type="button" :disabled="offline || !remoteDraft?.id" @click="appendEvidence">
          追加现场证据
        </button>

        <h3>来源</h3>
        <ul class="trace-list">
          <li v-for="item in sourceTrace" :key="item">{{ item }}</li>
        </ul>
      </aside>
    </div>

    <div v-if="message" :class="['inline-message', messageError ? 'error' : 'ok']">{{ message }}</div>

    <div class="detail-grid wide">
      <article class="data-card">
        <h3>当前草稿 / 检修单状态</h3>
        <dl class="status-list">
          <div><dt>草稿状态</dt><dd>{{ remoteDraft?.status ?? form.status ?? '仅本地' }}</dd></div>
          <div><dt>工单号</dt><dd>{{ remoteDraft?.work_order_no ?? form.workOrderNo ?? '—' }}</dd></div>
          <div><dt>待办状态</dt><dd>{{ remoteDraft?.todo_status ?? '—' }}</dd></div>
          <div><dt>三方一致性</dt>
            <dd><span :class="['badge', remoteDraft?.consistent ? 'ok' : 'warn']">{{ remoteDraft?.consistent ? '一致' : '待确认' }}</span></dd>
          </div>
        </dl>
      </article>

      <article class="data-card">
        <h3>历史工单附件（原附件留痕，不可覆盖）</h3>
        <table class="data-table">
          <thead><tr><th>工单号</th><th>状态</th><th>附件</th><th>补证次数</th></tr></thead>
          <tbody>
            <tr v-for="order in workOrders" :key="order.work_order_no">
              <td>{{ order.work_order_no }}</td>
              <td>{{ order.status }}</td>
              <td>
                <span v-for="item in order.attachments" :key="item.sha256" class="badge neutral">
                  {{ item.name }}
                </span>
              </td>
              <td>{{ order.supplemental_evidence?.length ?? 0 }}</td>
            </tr>
            <tr v-if="!workOrders.length"><td colspan="4" class="empty-state">暂无历史检修单</td></tr>
          </tbody>
        </table>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'
import {
  canonicalKey,
  digestFile,
  fromApiDraft,
  loadDraft,
  saveDraft,
  toApiPayload,
  type FireAttachment,
  type FireDraft,
} from '@/views/tunnel/fireStore'

type ApiRow = Record<string, unknown>
interface ApiOrderRow {
  work_order_no: string
  status: string
  attachments: FireAttachment[]
  supplemental_evidence?: unknown[]
}

const route = useRoute()

const routeTunnelId = Number(route.params.id ?? Number.NaN)
const initialCode = String(route.query.tunnel_code ?? (Number.isFinite(routeTunnelId) ? '' : 'TUNN-0001'))
const initialSeq = Number(route.query.seq ?? 1)
const offline = ref(false)
const message = ref('')
const messageError = ref(false)
const remoteDraft = ref<ApiRow | null>(null)
const workOrders = ref<ApiOrderRow[]>([])
const evidenceOperator = ref('')
const evidenceNote = ref('')
const evidenceFiles = ref<File[]>([])
const legacySource = ref('2026-09 旧版消防 App')

const stored = loadDraft(initialCode, initialSeq)
const form = reactive<FireDraft>(stored ?? {
  canonicalKey: canonicalKey(initialCode, initialSeq),
  tunnelCode: initialCode,
  localSeq: initialSeq,
  operator: '张检查',
  ventilationNote: '',
  fireNote: '',
  fireResult: '',
  confirmed: false,
  attachments: [],
  sources: ['消防工作表'],
  updatedAt: new Date().toISOString(),
})

const backLink = computed(() => {
  const match = remoteDraft.value?.tunnel_id
  return match ? `/tunnel/${match}` : '/tunnel'
})
const canRetry = computed(() => !offline.value && Boolean(form.pendingWriteback || remoteDraft.value?.status === '已确认'))
const sourceTrace = computed(() => form.sources)

function setMessage(text: string, isError = false) {
  message.value = text
  messageError.value = isError
}

function persist() {
  form.canonicalKey = canonicalKey(form.tunnelCode, form.localSeq)
  const saved = saveDraft({ ...form })
  Object.assign(form, saved)
}

function saveLocally() {
  if (!form.ventilationNote.trim()) {
    setMessage('请先核对并填写通风笔记', true)
    return
  }
  form.sources = Array.from(new Set([...form.sources, '消防工作表']))
  form.confirmed = false
  persist()
  setMessage('本地草稿已保存；页面切换或弱网断连不会覆盖通风笔记。')
}

async function addFiles(files: FileList | null): Promise<FireAttachment[]> {
  if (!files) return []
  const result: FireAttachment[] = []
  for (const file of Array.from(files)) {
    result.push({ name: file.name, sha256: await digestFile(file), uploaded_by: form.operator })
  }
  return result
}

async function addAttachments(event: Event) {
  const input = event.target as HTMLInputElement
  const incoming = await addFiles(input.files)
  const existing = new Map(form.attachments.map((item) => [item.sha256, item]))
  incoming.forEach((item) => existing.set(item.sha256, item))
  form.attachments = Array.from(existing.values())
  persist()
  input.value = ''
}

async function addEvidenceFiles(event: Event) {
  const input = event.target as HTMLInputElement
  evidenceFiles.value = Array.from(input.files ?? [])
}

async function readError(response: Response) {
  try {
    const payload = await response.json()
    return String(payload.detail ?? payload.message ?? '操作失败')
  } catch {
    return `接口返回 ${response.status}`
  }
}

async function postJson(path: string, payload: unknown) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(payload) })
  if (!response.ok) throw new Error(await readError(response))
  return response.json()
}

function applyRemoteDraft(data: ApiRow, options: { preserveLocal?: boolean } = {}) {
  const before = { ...form }
  remoteDraft.value = data
  const next = fromApiDraft(data, { ...form, pendingWriteback: form.pendingWriteback })
  const shouldPreserve = options.preserveLocal
    && Boolean(before.ventilationNote.trim() || before.fireNote.trim() || before.attachments.length)
  Object.assign(form, next)
  if (shouldPreserve) {
    form.ventilationNote = before.ventilationNote
    form.fireNote = before.fireNote
    form.attachments = next.attachments
    form.fireResult = before.fireResult
  }
  persist()
}

async function reloadRemote() {
  persist()
  if (offline.value) {
    setMessage('当前处于离线状态，仅展示本地草稿。')
    return
  }
  try {
    const params = new URLSearchParams({ tunnel_code: form.tunnelCode })
    const response = await request(`/api/tunnel-worksheets?${params}`)
    if (!response.ok) throw new Error(await readError(response))
    const payload = await response.json()
    const item = (payload.items as ApiRow[]).find((row) => Number(row.local_seq) === form.localSeq) ?? null
    if (item) applyRemoteDraft(item, { preserveLocal: true })
    else remoteDraft.value = null

    const orderResponse = await request(`/api/tunnel-worksheets/work-orders?${params}`)
    if (orderResponse.ok) {
      const orderPayload = await orderResponse.json()
      workOrders.value = (orderPayload.items as ApiOrderRow[]) ?? []
    }
    setMessage(item ? '已读取服务端现场版本。' : '服务端尚无该幂等键草稿，可先离线保存。')
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '读取失败', true)
  }
}

async function syncDraft(confirmed: boolean) {
  form.confirmed = confirmed
  if (confirmed) form.sources = Array.from(new Set([...form.sources, '检查员最后确认现场版本']))
  persist()
  if (offline.value) {
    form.pendingWriteback = confirmed ? form.pendingWriteback : false
    setMessage('离线中：草稿已保存在本机，重连后将按相同幂等键重传。')
    return
  }
  try {
    const payload = toApiPayload(form)
    const path = confirmed ? '/api/tunnel-worksheets/merge' : '/api/tunnel-worksheets'
    const result = await postJson(path, { values: payload })
    applyRemoteDraft(result.entry as ApiRow)
    setMessage(confirmed ? '草稿已合并，最后确认的现场版本生效。' : '草稿已同步，未生成重复记录。')
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '同步失败', true)
  }
}

async function confirmAndMerge() {
  if (!form.ventilationNote.trim() || !form.fireNote.trim() || !form.fireResult || !form.operator.trim()) {
    setMessage('合并前需核对通风笔记，并填写检查员、消防记录和消防结论。', true)
    return
  }
  await syncDraft(true)
}

async function writeBack() {
  persist()
  if (offline.value || !remoteDraft.value?.id) {
    form.pendingWriteback = true
    persist()
    setMessage('离线回写已排队；重连后先合并草稿，再用同一幂等键回写，不会重复建单。')
    return
  }
  if (remoteDraft.value.status !== '已确认') {
    setMessage('请先完成“合并现场草稿”，确认后的版本才能回写。', true)
    return
  }
  try {
    const result = await postJson(`/api/tunnel-worksheets/${remoteDraft.value.id}/write-back`, {
      values: { expected_revision: form.revision },
    })
    applyRemoteDraft(result.draft as ApiRow)
    form.pendingWriteback = false
    persist()
    const created = result.work_order as ApiOrderRow
    workOrders.value = [created, ...workOrders.value.filter((item) => item.work_order_no !== created.work_order_no)]
    setMessage(result.idempotent ? result.message : result.message)
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '回写失败', true)
  }
}

async function retryAfterReconnect() {
  offline.value = false
  await syncDraft(true)
  await reloadRemote()
  if (remoteDraft.value?.status === '已确认' || remoteDraft.value?.status === '已回写') {
    await writeBack()
  } else {
    setMessage('重连已完成，请补齐消防记录和结论后再合并、回写。')
  }
}

async function migrateLegacy() {
  persist()
  if (offline.value) {
    form.sources = Array.from(new Set([...form.sources, `旧草稿迁移：${legacySource.value}`]))
    persist()
    setMessage('离线迁移请求已记录来源，联网后提交。')
    return
  }
  try {
    const result = await postJson('/api/tunnel-worksheets/migrate', {
      values: { ...toApiPayload(form), source: legacySource.value, legacy: { ...form } },
    })
    applyRemoteDraft(result.entry as ApiRow)
    setMessage('旧草稿已迁移，原始载荷和来源均已保留。')
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '迁移失败', true)
  }
}

async function appendEvidence() {
  if (!remoteDraft.value?.id) {
    setMessage('请先同步草稿，再补写证据。', true)
    return
  }
  const attachments: FireAttachment[] = []
  for (const file of evidenceFiles.value) {
    attachments.push({ name: file.name, sha256: await digestFile(file), uploaded_by: evidenceOperator.value || form.operator })
  }
  try {
    const result = await postJson(`/api/tunnel-worksheets/${remoteDraft.value.id}/evidence`, {
      values: {
        note: evidenceNote.value,
        operator: evidenceOperator.value || form.operator,
        attachments,
        expected_revision: form.revision,
      },
    })
    applyRemoteDraft(result.entry as ApiRow)
    evidenceNote.value = ''
    evidenceFiles.value = []
    setMessage('现场证据已追加，原通风笔记、原附件和历史工单附件未被覆盖。')
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '补写失败', true)
  }
}

async function initializeTunnelCode() {
  if (!Number.isFinite(routeTunnelId)) return
  try {
    const response = await request(`/api/tunnel/${routeTunnelId}`)
    if (!response.ok) return
    const tunnel = await response.json()
    form.tunnelCode = String(tunnel['隧道编号'] ?? '')
    const local = loadDraft(form.tunnelCode, form.localSeq)
    if (local) Object.assign(form, local)
  } catch {
    setMessage('隧道编号读取失败，请手工填写后继续', true)
  }
}

onMounted(async () => {
  await initializeTunnelCode()
  await reloadRemote()
})
</script>
