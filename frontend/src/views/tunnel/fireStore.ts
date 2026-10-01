/** 隧道消防现场草稿的本地持久化与弱网重传队列。 */

export interface FireAttachment {
  name: string
  sha256: string
  uploaded_by: string
}

export interface FireDraft {
  serverId?: number
  canonicalKey: string
  tunnelCode: string
  localSeq: number
  operator: string
  ventilationNote: string
  fireNote: string
  fireResult: '' | '正常' | '限期整改' | '停用整改'
  confirmed: boolean
  attachments: FireAttachment[]
  sources: string[]
  revision?: number
  status?: string
  workOrderNo?: string
  consistent?: boolean
  pendingWriteback?: boolean
  updatedAt: string
}

const STORAGE_PREFIX = 'tunnel-fire-draft:'
const legacyDraft = {
  canonicalKey: '',
  tunnelCode: '',
  localSeq: 1,
  operator: '',
  ventilationNote: '',
  fireNote: '',
  fireResult: '',
  confirmed: false,
  attachments: [],
  sources: [],
  updatedAt: '',
} satisfies FireDraft

export function canonicalKey(tunnelCode: string, localSeq: number): string {
  return `${tunnelCode.trim()}#${String(localSeq).padStart(6, '0')}`
}

function storageKey(key: string): string {
  return `${STORAGE_PREFIX}${key}`
}

export function loadDraft(tunnelCode: string, localSeq = 1): FireDraft | null {
  const raw = window.localStorage.getItem(storageKey(canonicalKey(tunnelCode, localSeq)))
  if (!raw) return null
  try {
    return { ...legacyDraft, ...(JSON.parse(raw) as Partial<FireDraft>), canonicalKey: canonicalKey(tunnelCode, localSeq) }
  } catch {
    return null
  }
}

export function saveDraft(draft: Omit<FireDraft, 'canonicalKey' | 'updatedAt'> & { updatedAt?: string }): FireDraft {
  const key = canonicalKey(draft.tunnelCode, draft.localSeq)
  const stored: FireDraft = {
    ...draft,
    canonicalKey: key,
    sources: Array.from(new Set(draft.sources.filter(Boolean))),
    updatedAt: draft.updatedAt ?? new Date().toISOString(),
  }
  window.localStorage.setItem(storageKey(key), JSON.stringify(stored))
  return stored
}

export function listLocalDrafts(tunnelCode?: string): FireDraft[] {
  const result: FireDraft[] = []
  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index)
    if (!key?.startsWith(STORAGE_PREFIX)) continue
    try {
      const draft = JSON.parse(window.localStorage.getItem(key) ?? '{}') as FireDraft
      if (!tunnelCode || draft.tunnelCode === tunnelCode) result.push(draft)
    } catch {
      // 损坏的本地缓存不应阻塞列表读取。
    }
  }
  return result.sort((a, b) => a.canonicalKey.localeCompare(b.canonicalKey))
}

export function toApiPayload(draft: FireDraft) {
  return {
    tunnel_code: draft.tunnelCode,
    local_seq: draft.localSeq,
    operator: draft.operator,
    ventilation_note: draft.ventilationNote,
    fire_note: draft.fireNote,
    fire_result: draft.fireResult || null,
    confirmed: draft.confirmed,
    attachments: draft.attachments.map((item) => ({
      name: item.name,
      sha256: item.sha256,
      uploaded_by: item.uploaded_by || draft.operator,
    })),
    sources: draft.sources,
    expected_revision: draft.revision,
  }
}

export function fromApiDraft(data: Record<string, unknown>, previous?: FireDraft | null): FireDraft {
  const tunnelCode = String(data.tunnel_code ?? previous?.tunnelCode ?? '')
  const localSeq = Number(data.local_seq ?? previous?.localSeq ?? 1)
  const apiAttachments = Array.isArray(data.attachments) ? (data.attachments as FireAttachment[]) : []
  const sources = Array.isArray(data.source_trace)
    ? (data.source_trace as string[])
    : previous?.sources ?? []
  const attachments = apiAttachments.length
    ? mergeAttachments(apiAttachments, previous?.attachments ?? [])
    : previous?.attachments ?? []
  return {
    serverId: Number(data.id ?? previous?.serverId),
    canonicalKey: String(data.canonical_key ?? canonicalKey(tunnelCode, localSeq)),
    tunnelCode,
    localSeq,
    operator: String(data.operator ?? previous?.operator ?? ''),
    ventilationNote: String(data.ventilation_note ?? previous?.ventilationNote ?? ''),
    fireNote: String(data.fire_note ?? previous?.fireNote ?? ''),
    fireResult: (data.fire_result as FireDraft['fireResult']) ?? previous?.fireResult ?? '',
    confirmed: Boolean(data.confirmed ?? previous?.confirmed),
    attachments,
    sources: Array.from(new Set([...sources, ...(previous?.sources ?? [])])),
    revision: typeof data.revision === 'number' ? data.revision : previous?.revision,
    status: String(data.status ?? previous?.status ?? ''),
    workOrderNo: data.work_order_no ? String(data.work_order_no) : previous?.workOrderNo,
    consistent: Boolean(data.consistent ?? previous?.consistent),
    pendingWriteback: Boolean(previous?.pendingWriteback),
    updatedAt: String(data.updated_at ?? new Date().toISOString()),
  }
}

function mergeAttachments(left: FireAttachment[], right: FireAttachment[]): FireAttachment[] {
  const map = new Map<string, FireAttachment>()
  ;[...left, ...right].forEach((item) => map.set(item.sha256, item))
  return Array.from(map.values())
}

export async function digestFile(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  if (window.crypto?.subtle) {
    const digest = await window.crypto.subtle.digest('SHA-256', buffer)
    return Array.from(new Uint8Array(digest))
      .map((value) => value.toString(16).padStart(2, '0'))
      .join('')
  }
  const bytes = new Uint8Array(buffer)
  let hash = 0
  bytes.forEach((value) => {
    hash = (hash * 31 + value) >>> 0
  })
  return `local-${hash.toString(16).padStart(8, '0')}-${file.size}`
}
