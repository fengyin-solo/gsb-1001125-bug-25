<template>
  <section class="page" data-module="tunnel-fire">
    <header class="page-head">
      <div>
        <h2>消防工作表 · {{ tunnelCode || '—' }}</h2>
        <p class="page-desc">弱网下从详情页转入时，通风笔记仍保留在同一草稿；先合并草稿，再回写检修单，重连重传不重复建单。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn" :to="detailLink">返回隧道详情</RouterLink>
        <RouterLink class="btn ghost" :to="`/tunnel`">隧道列表</RouterLink>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">待重传命令</span>
        <strong class="stat-value">{{ queued.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">本地草稿号</span>
        <strong class="stat-value">#{{ work.draftId ?? '未分配' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">通风笔记（核对过的现场版）</span>
        <strong class="stat-value vent">{{ ventContent || '未填写' }}</strong>
      </article>
    </div>

    <article class="draft-card">
      <h3>消防检查项</h3>
      <label class="filter-item">
        <span>检查员</span>
        <input v-model="inspector" placeholder="检查员姓名" />
      </label>
      <label class="filter-item note-field">
        <span>消防检查记录</span>
        <textarea v-model="fireNote" rows="4" placeholder="如：消火栓箱封条破损，灭火器压力正常"></textarea>
      </label>
      <label class="filter-item">
        <span>附件名（现场拍照/录像）</span>
        <input v-model="attachmentName" placeholder="fire-seal-01.jpg" />
      </label>
      <div class="row-actions" style="margin: 8px 0">
        <button class="btn" type="button" :disabled="busy" @click="saveFire">暂存消防检查项</button>
        <button class="btn" type="button" :disabled="busy" @click="mergeDrafts">合并通风/消防草稿</button>
        <button class="btn primary" type="button" :disabled="busy" @click="submitOrder">回写检修单</button>
        <button class="btn ghost" type="button" @click="flushNow">立即重传排队命令</button>
      </div>
      <p v-if="message" :class="lastOk ? 'ok-text' : 'error-text'">{{ message }}</p>
    </article>

    <article v-if="conflict" class="draft-card conflict">
      <h3>并发补写冲突（未覆盖他人证据）</h3>
      <p>服务端现场证据已到 v{{ conflict.version }}，他人刚写入：</p>
      <ul>
        <li v-for="(body, key) in conflict.sections" :key="key">
          <strong>{{ key }}</strong>：{{ body.content }}（{{ body.inspector }}）
        </li>
      </ul>
      <button class="btn" type="button" @click="conflict = null">知道了，我去合并后再补写</button>
    </article>

    <article class="draft-card">
      <h3>合并后将回写的内容预览</h3>
      <ul class="preview">
        <li><b>通风：</b>{{ ventContent || '（空）' }}<em v-if="ventConfirmed"> · 已现场确认</em></li>
        <li><b>消防：</b>{{ fireNote || work.sections['消防']?.content || '（空）' }}</li>
        <li><b>附件：</b>{{ attachmentNames || '无' }}</li>
      </ul>
    </article>

    <article class="draft-card">
      <h3>并发补写现场证据（乐观锁）</h3>
      <p class="page-desc">基于证据 v{{ evidenceVersion }} 补写；他人已抢先写入时返回 409，本端不会覆盖。</p>
      <div class="filter-bar">
        <label class="filter-item">
          <span>补写专项</span>
          <select v-model="patchSection">
            <option value="通风">通风</option>
            <option value="消防">消防</option>
          </select>
        </label>
        <label class="filter-item note-field">
          <span>补写内容</span>
          <input v-model="patchContent" placeholder="基于最新版本的补充证据" />
        </label>
        <button class="btn" type="button" @click="patchEvidence">提交补写</button>
      </div>
    </article>

    <article class="draft-card">
      <h3>旧系统草稿迁移（保留来源）</h3>
      <div class="filter-bar">
        <label class="filter-item">
          <span>来源（必填）</span>
          <input v-model="legacyOrigin" placeholder="如：2025旧版PDA#TD-88" />
        </label>
        <label class="filter-item note-field">
          <span>旧草稿内容</span>
          <input v-model="legacyContent" placeholder="旧系统里记录的现场文字" />
        </label>
        <button class="btn" type="button" @click="migrateLegacy">迁移为消防草稿</button>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { request } from '@/api/client'
import { useInspectionStore } from '@/stores/inspection'

const route = useRoute()
const inspection = useInspectionStore()

const tunnelCode = String(route.query.code ?? '')
const inspector = ref('张三')
const fireNote = ref('')
const attachmentName = ref('')
const busy = ref(false)
const message = ref('')
const lastOk = ref(false)
const conflict = ref<{ version: number; sections: Record<string, { content: string; inspector: string }> } | null>(null)
const patchSection = ref<'通风' | '消防'>('消防')
const patchContent = ref('')
const evidenceVersion = ref(0)
const legacyOrigin = ref('')
const legacyContent = ref('')

const work = computed(() => inspection.ensureWorking(tunnelCode, inspector.value))
const queued = computed(() => inspection.pendingForTunnel(tunnelCode))
const ventContent = computed(() => work.value.sections['通风']?.content ?? '')
const ventConfirmed = computed(() => Boolean(work.value.sections['通风']?.confirmed))
const attachmentNames = computed(() => work.value.attachments.map((a) => a.name).join('、'))
const detailLink = computed(() => ({ path: '/tunnel/detail', query: { code: tunnelCode } }))

async function saveFire() {
  busy.value = true
  const attachments = attachmentName.value.trim()
    ? [{ name: attachmentName.value.trim(), sha256: `local-${Date.now()}` }]
    : []
  const result = await inspection.saveSection({
    tunnelCode,
    section: '消防',
    inspector: inspector.value,
    content: fireNote.value,
    attachments,
  })
  fireNote.value = work.value.sections['消防']?.content ?? fireNote.value
  message.value = result.message
  lastOk.value = result.ok || result.enqueued
  busy.value = false
}

/**
 * 多草稿合并：通风页与消防页若各自产生了草稿（例如分设备离线作业），
 * 以检查员最后确认的现场版本为准；被并草稿保留来源。
 */
async function mergeDrafts() {
  busy.value = true
  try {
    const drafts = await (await request(
      `/api/tunnel/inspection/drafts?tunnel_code=${encodeURIComponent(tunnelCode)}`,
    )).json() as { items: { id: number; sections: Record<string, unknown> }[] }
    const open = drafts.items
    if (open.length <= 1) {
      message.value = open.length === 1 ? '当前只有一份草稿，无需合并' : '还没有可合并的草稿，请先暂存'
      lastOk.value = true
    } else {
      // 含已确认通风内容的草稿作主草稿；否则取最新一份
      const survivor = open.find((d) => d.sections['通风'] && work.value.sections['通风']?.confirmed) ?? open[0]
      const response = await request(`/api/tunnel/inspection/tunnels/${encodeURIComponent(tunnelCode)}/merge`, {
        method: 'POST',
        body: JSON.stringify({
          surviving_draft_id: survivor.id,
          absorbed_draft_ids: open.filter((d) => d.id !== survivor.id).map((d) => d.id),
          operator: inspector.value,
        }),
      })
      if (!response.ok) throw new Error(`合并失败（${response.status}）`)
      inspection.working[tunnelCode].draftId = survivor.id
      message.value = '草稿已合并：现场最后确认版为准，旧草稿来源已留痕'
      lastOk.value = true
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '草稿合并失败，已保留各草稿未动'
    lastOk.value = false
  }
  busy.value = false
}

async function submitOrder() {
  busy.value = true
  // 确保消防内容先落草稿，再以同一份草稿回写
  const saved = await inspection.saveSection({
    tunnelCode,
    section: '消防',
    inspector: inspector.value,
    content: fireNote.value || work.value.sections['消防']?.content || '',
  })
  const result = await inspection.submitWorkOrder({
    tunnelCode,
    inspector: inspector.value,
    draftId: saved.draftId ?? work.value.draftId,
    title: `${tunnelCode} 通风/消防现场检修单`,
  })
  message.value = result.message
  lastOk.value = result.ok || result.enqueued
  busy.value = false
  if (result.ok) {
    inspection.clearAcked()
  }
}

async function flushNow() {
  busy.value = true
  await inspection.flush()
  message.value = '重传队列去重并按序列号有序重放，重复请求按原序列号幂等'
  lastOk.value = true
  busy.value = false
}

async function patchEvidence() {
  if (!patchContent.value.trim()) {
    message.value = '补写内容不能为空'
    lastOk.value = false
    return
  }
  const result = await inspection.patchEvidence({
    tunnelCode,
    section: patchSection.value,
    inspector: inspector.value,
    content: patchContent.value,
    baseVersion: evidenceVersion.value,
  })
  if (result.conflict) {
    conflict.value = result.conflict as typeof conflict.value
    evidenceVersion.value = (result.conflict as { version: number }).version
  } else if (result.ok) {
    evidenceVersion.value += 1
    patchContent.value = ''
  }
  message.value = result.message
  lastOk.value = result.ok
}

async function migrateLegacy() {
  const result = await inspection.migrateLegacy({
    tunnelCode,
    section: '消防',
    inspector: inspector.value,
    content: legacyContent.value,
    origin: legacyOrigin.value,
  })
  message.value = result.message
  lastOk.value = result.ok
  if (result.ok) {
    legacyContent.value = ''
    legacyOrigin.value = ''
  }
}

async function loadEvidenceVersion() {
  try {
    const response = await request(`/api/tunnel/inspection/tunnels/${encodeURIComponent(tunnelCode)}/evidence`)
    if (response.ok) {
      const payload = await response.json() as { version: number }
      evidenceVersion.value = payload.version
    }
  } catch {
    // 离线时保留上次版本，补写会失败并提示，不会盲目覆盖
  }
}

onMounted(() => {
  inspection.bindNetwork()
  const local = inspection.working[tunnelCode]
  if (local?.sections['消防']?.content) fireNote.value = local.sections['消防']!.content
  void loadEvidenceVersion()
  void inspection.flush()
})
</script>

<style scoped>
.draft-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.draft-card h3 { margin: 0 0 8px; font-size: 15px; }
.note-field { display: block; margin: 8px 0; }
.note-field textarea { width: 100%; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font: inherit; }
.ok-text { color: #067647; }
.vent { font-size: 14px; font-weight: 500; }
.conflict { border-color: #b54708; background: #fffaeb; }
.preview { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.9; }
.preview em { color: var(--muted); font-style: normal; }
</style>
