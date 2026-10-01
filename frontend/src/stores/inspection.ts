/**
 * 隧道现场检修：本地草稿与离线重传队列。
 *
 * 关键不变量：
 * - 通风笔记与消防检查项都先落本地草稿，弱网下在两个页面间切换不丢内容；
 * - 每次提交分配「隧道编号 + 本地序列号」幂等键，重连重传不会重复生成检修单；
 * - 队列按序列号顺序重放，已确认（acked）的请求直接丢弃；
 * - 并发补写遇到 409 时不自动重试覆盖，挂起并把服务端最新版本交回页面提示。
 */
import { defineStore } from 'pinia'

import { request } from '@/api/client'

export type Section = '通风' | '消防'
export type QueueKind = 'draft' | 'work_order' | 'evidence'

export interface QueuedCommand {
  id: string
  kind: QueueKind
  tunnelCode: string
  /** 本地序列号：与隧道编号共同构成幂等键，草稿内单调递增。 */
  clientSeq: number
  path: string
  body: Record<string, unknown>
  createdAt: string
  status: 'pending' | 'sending' | 'acked' | 'conflict'
  conflictDetail?: unknown
}

interface InspectionState {
  online: boolean
  /** 隧道编号 -> 序列号游标。 */
  seqCounters: Record<string, number>
  queue: QueuedCommand[]
  /** 隧道编号 -> 本地正在编辑的草稿（通风/消防同一份，切换页面不丢）。 */
  working: Record<string, {
    draftId: number | null
    inspector: string
    sections: Partial<Record<Section, { content: string; confirmed: boolean }>>
    attachments: { name: string; sha256: string }[]
    savedAt: string
  }>
}

const STORAGE_KEY = 'tunnel-inspection-queue-v1'

function loadPersisted(): { queue: QueuedCommand[]; seqCounters: Record<string, number> } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return { queue: [], seqCounters: {} }
    const parsed = JSON.parse(raw) as Partial<InspectionState>
    return {
      queue: Array.isArray(parsed.queue) ? parsed.queue : [],
      seqCounters: parsed.seqCounters ?? {},
    }
  } catch {
    return { queue: [], seqCounters: {} }
  }
}

export const useInspectionStore = defineStore('inspection', {
  state: (): InspectionState => {
    const persisted = loadPersisted()
    return {
      online: navigator.onLine,
      seqCounters: persisted.seqCounters,
      queue: persisted.queue,
      working: {},
    }
  },
  getters: {
    pendingCount: (state) => state.queue.filter((c) => c.status === 'pending' || c.status === 'conflict').length,
    pendingForTunnel: (state) => (code: string) =>
      state.queue.filter((c) => c.tunnelCode === code && (c.status === 'pending' || c.status === 'conflict')),
  },
  actions: {
    bindNetwork() {
      window.addEventListener('online', () => {
        this.online = true
        void this.flush()
      })
      window.addEventListener('offline', () => {
        this.online = false
      })
    },
    persist() {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        queue: this.queue,
        seqCounters: this.seqCounters,
      }))
    },
    nextSeq(tunnelCode: string): number {
      const next = (this.seqCounters[tunnelCode] ?? 0) + 1
      this.seqCounters[tunnelCode] = next
      return next
    },
    ensureWorking(tunnelCode: string, inspector: string) {
      if (!this.working[tunnelCode]) {
        this.working[tunnelCode] = {
          draftId: null,
          inspector,
          sections: {},
          attachments: [],
          savedAt: '',
        }
      }
      return this.working[tunnelCode]
    },
    /** 保存一个专项（通风/消防）到同一份本地草稿，切换页面互不覆盖。 */
    async saveSection(input: {
      tunnelCode: string
      section: Section
      inspector: string
      content: string
      attachments?: { name: string; sha256: string }[]
      draftId?: number | null
    }): Promise<{ ok: boolean; draftId?: number | null; enqueued: boolean; message: string }> {
      const work = this.ensureWorking(input.tunnelCode, input.inspector)
      const prev = work.sections[input.section]
      work.sections[input.section] = {
        content: input.content,
        // 保留既有现场确认标记；空内容不改变确认态
        confirmed: prev?.confirmed ?? false,
      }
      for (const file of input.attachments ?? []) {
        if (!work.attachments.some((a) => a.sha256 === file.sha256 && a.name === file.name)) {
          work.attachments.push(file)
        }
      }
      work.savedAt = new Date().toISOString()

      const clientSeq = this.nextSeq(input.tunnelCode)
      const body = {
        tunnel_code: input.tunnelCode,
        section: input.section,
        inspector: input.inspector,
        content: input.content,
        client_seq: clientSeq,
        draft_id: input.draftId ?? work.draftId ?? null,
        attachments: input.attachments ?? [],
      }
      const command = this.enqueue('draft', '/api/tunnel/inspection/drafts', input.tunnelCode, clientSeq, body)

      if (!this.online) {
        return { ok: false, enqueued: true, draftId: work.draftId, message: '当前离线，通风笔记已存本机，恢复网络后自动续传' }
      }
      try {
        const response = await request(command.path, { method: 'POST', body: JSON.stringify(body) })
        if (response.ok) {
          const payload = await response.json() as { id: number }
          work.draftId = payload.id
          this.markAcked(command.id)
          return { ok: true, draftId: payload.id, enqueued: false, message: '现场草稿已保存' }
        }
        return { ok: false, enqueued: true, draftId: work.draftId, message: `保存未成功（${response.status}），已加入重传队列` }
      } catch {
        command.status = 'pending'
        this.persist()
        return { ok: false, enqueued: true, draftId: work.draftId, message: '网络不可达，已暂存并将在重连后续传' }
      }
    },
    /** 回写检修单：同一体 (隧道编号, 序列号) 重放只会得到同一张单。 */
    async submitWorkOrder(input: {
      tunnelCode: string
      inspector: string
      draftId: number | null
      title?: string
    }): Promise<{ ok: boolean; created?: boolean; orderNo?: string; enqueued: boolean; message: string }> {
      // 命令自包含：完全离线时尚未拿到服务端 draft_id，重放也能凭内嵌内容成单
      const work = this.ensureWorking(input.tunnelCode, input.inspector)
      const sections: Record<string, { content: string; confirmed: boolean }> = {}
      for (const name of ['通风', '消防'] as const) {
        const section = work.sections[name]
        if (section?.content) sections[name] = { content: section.content, confirmed: section.confirmed }
      }
      const clientSeq = this.nextSeq(input.tunnelCode)
      const body = {
        tunnel_code: input.tunnelCode,
        client_seq: clientSeq,
        inspector: input.inspector,
        draft_id: input.draftId ?? work.draftId ?? null,
        title: input.title ?? '',
        sections,
        attachments: work.attachments,
      }
      const command = this.enqueue('work_order', '/api/tunnel/inspection/work-orders', input.tunnelCode, clientSeq, body)
      return this.sendWorkOrder(command, body)
    },
    async sendWorkOrder(command: QueuedCommand, body: Record<string, unknown>) {
      try {
        const response = await request(command.path, { method: 'POST', body: JSON.stringify(body) })
        if (response.ok) {
          const payload = await response.json() as { created: boolean; order: { order_no: string } }
          this.markAcked(command.id)
          return {
            ok: true,
            created: payload.created,
            orderNo: payload.order.order_no,
            enqueued: false,
            message: payload.created ? `检修单 ${payload.order.order_no} 已回写` : `重连重传已识别原单 ${payload.order.order_no}，未重复建单`,
          }
        }
        return { ok: false, enqueued: true, message: `回写未成功（${response.status}），检修单请求已排队` }
      } catch {
        command.status = 'pending'
        this.persist()
        return { ok: false, enqueued: true, message: '网络不可达，检修单将在重连后按原序列号幂等重传' }
      }
    },
    enqueue(kind: QueueKind, path: string, tunnelCode: string, clientSeq: number, body: Record<string, unknown>): QueuedCommand {
      // 幂等去重：同一隧道同一序列号只保留一条（重连重传不重复）
      const existing = this.queue.find(
        (c) => c.tunnelCode === tunnelCode && c.clientSeq === clientSeq && c.kind === kind && c.status !== 'acked',
      )
      if (existing) return existing
      const command: QueuedCommand = {
        id: `${kind}-${tunnelCode}-${clientSeq}`,
        kind,
        tunnelCode,
        clientSeq,
        path,
        body,
        createdAt: new Date().toISOString(),
        status: 'pending',
      }
      this.queue.push(command)
      this.persist()
      return command
    },
    markAcked(id: string) {
      const command = this.queue.find((c) => c.id === id)
      if (command) {
        command.status = 'acked'
        this.persist()
      }
    },
    /** 旧系统草稿迁移：必须带来源 origin，服务端会长期留痕。 */
    async migrateLegacy(input: {
      tunnelCode: string
      section: Section
      inspector: string
      content: string
      origin: string
    }): Promise<{ ok: boolean; message: string }> {
      const body = {
        tunnel_code: input.tunnelCode,
        section: input.section,
        inspector: input.inspector,
        content: input.content,
        origin: input.origin,
      }
      try {
        const response = await request('/api/tunnel/inspection/migrations/legacy-draft', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        if (!response.ok) {
          const detail = await response.json().catch(() => null) as { detail?: string } | null
          return { ok: false, message: detail?.detail ?? `迁移被拒（${response.status}）` }
        }
        return { ok: true, message: `旧草稿已迁移，来源「${input.origin}」已留痕` }
      } catch {
        return { ok: false, message: '迁移请求未送达，请联网后重试' }
      }
    },
    /** 并发补写现场证据：携带 base_version，过期写入收到 409，不覆盖他人内容。 */
    async patchEvidence(input: {
      tunnelCode: string
      section: Section
      inspector: string
      content: string
      baseVersion: number
    }): Promise<{ ok: boolean; conflict?: unknown; message: string }> {
      const body = {
        tunnel_code: input.tunnelCode,
        section: input.section,
        inspector: input.inspector,
        content: input.content,
        base_version: input.baseVersion,
      }
      try {
        const response = await request('/api/tunnel/inspection/evidence', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        if (response.status === 409) {
          const payload = await response.json() as { detail: { current: unknown } }
          return { ok: false, conflict: payload.detail.current, message: '现场证据已被他人更新，未覆盖' }
        }
        if (!response.ok) return { ok: false, message: `补写失败（${response.status}）` }
        return { ok: true, message: '现场证据已补写' }
      } catch {
        return { ok: false, message: '补写请求未送达' }
      }
    },
    /** 恢复网络后按隧道、序列号顺序重放；409 的证据补写挂起等待人工处理。 */
    async flush() {
      const pending = this.queue
        .filter((c) => c.status === 'pending')
        .sort((a, b) => a.tunnelCode.localeCompare(b.tunnelCode) || a.clientSeq - b.clientSeq)
      for (const command of pending) {
        command.status = 'sending'
        try {
          const response = await request(command.path, { method: 'POST', body: JSON.stringify(command.body) })
          if (response.ok) {
            command.status = 'acked'
            if (command.kind === 'draft') {
              const payload = await response.json() as { id: number }
              const work = this.working[command.tunnelCode]
              if (work && work.draftId === null) work.draftId = payload.id
            }
          } else if (response.status === 409 && command.kind === 'evidence') {
            command.status = 'conflict'
            command.conflictDetail = await response.json().catch(() => null)
          } else {
            command.status = 'pending'
            return
          }
        } catch {
          command.status = 'pending'
          return
        }
      }
      this.persist()
    },
    clearAcked() {
      this.queue = this.queue.filter((c) => c.status !== 'acked')
      this.persist()
    },
  },
})
