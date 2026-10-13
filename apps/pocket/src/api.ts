import { voiceInstruction, voiceReply, visibleUserText } from './speech'
import { buildHermesWebSocketUrl, JsonRpcGatewayClient, type ServerRequest } from '@hermes/shared'

interface ListedSession {
  id: string
  title?: string
  preview?: string
  started_at?: number
  source?: string
}

interface StoredMessage {
  role?: string
  text?: string | null
}

export interface TurnLine {
  role: 'user' | 'assistant'
  text: string
}

export interface PastSession {
  id: string
  title: string
  preview: string
  started_at: number
}

function authHeaders(token: string): HeadersInit {
  return { 'X-Hermes-Session-Token': token, 'Content-Type': 'application/json' }
}

export async function exchangeCode(code: string): Promise<string> {
  const response = await fetch('/api/pocket/exchange', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code })
  })
  let body: { token?: string; detail?: string }
  try {
    body = await response.json() as { token?: string; detail?: string }
  } catch {
    throw new Error('连接服务没有返回有效响应，请确认 Hermes 后端正在运行后重试')
  }
  if (!response.ok || !body.token?.startsWith('pd_')) throw new Error(body.detail || '连接码没有兑换成功')
  return body.token
}

export async function openGateway(token: string, onRequest: (request: ServerRequest) => void): Promise<JsonRpcGatewayClient> {
  const client = new JsonRpcGatewayClient()

  client.onRequest(onRequest)
  await client.connect(buildHermesWebSocketUrl({ path: '/api/ws', authParam: ['token', token] }))

  return client
}

export async function disconnectDevice(token: string): Promise<void> {
  const response = await fetch('/api/pocket/device', {
    method: 'DELETE',
    headers: authHeaders(token)
  })

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(body.detail || '设备没有断开')
  }
}

export async function rememberName(token: string, name: string): Promise<void> {
  const response = await fetch('/api/pocket/name', {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ name })
  })

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string }

    throw new Error(body.detail || '名字没有记下')
  }
}

export async function ensureSession(client: JsonRpcGatewayClient, storedId: string, title: string): Promise<string> {
  if (storedId) {
    try {
      const resumed = await client.request<{ session_id: string }>('session.resume', {
        session_id: storedId,
        source: 'pocket',
        lazy: true,
        inline_images: false,
        omit_messages: true
      })

      if (resumed.session_id) return resumed.session_id
    } catch {
      // The stored conversation is gone. Open a new one.
    }
  }

  const created = await client.request<{ session_id: string }>('session.create', {
    source: 'pocket',
    title
  })

  return created.session_id
}

export async function submitText(client: JsonRpcGatewayClient, sessionId: string, text: string, voice = false): Promise<void> {
  await client.request('prompt.submit', { session_id: sessionId, text: voice ? text + voiceInstruction : text })
}

export async function interruptSession(client: JsonRpcGatewayClient, sessionId: string): Promise<void> {
  await client.request('session.interrupt', { session_id: sessionId }).catch(() => undefined)
}

export async function listSessions(client: JsonRpcGatewayClient): Promise<PastSession[]> {
  const result = await client.request<{ sessions: ListedSession[] }>('session.list', { limit: 40 })
  const rows = result.sessions ?? []
  const pocket = rows.filter(row => row.source === 'pocket')

  return pocket.map(row => ({
    id: row.id,
    title: row.title || '对话',
    preview: row.preview || '',
    started_at: row.started_at || 0
  }))
}

export async function readSession(client: JsonRpcGatewayClient, storedId: string): Promise<TurnLine[]> {
  const result = await client.request<{ messages?: StoredMessage[] }>('session.resume', {
    session_id: storedId,
    source: 'pocket',
    lazy: true,
    inline_images: false
  })
  const lines: TurnLine[] = []

  for (const message of result.messages ?? []) {
    if (message.role !== 'user' && message.role !== 'assistant') continue
    const text = typeof message.text === 'string' ? message.text : ''

    const visible = message.role === 'assistant' ? voiceReply(text).text : visibleUserText(text).trim()
    if (visible) lines.push({ role: message.role, text: visible })
  }

  return lines
}

export function watchTurn(
  client: JsonRpcGatewayClient,
  sessionId: string,
  handlers: {
    onDelta: (chunk: string) => void
    onComplete: (text: string) => void
    onTool: () => void
  }
): () => void {
  return client.onEvent(event => {
    if (event.session_id && event.session_id !== sessionId) return
    const payload = event.payload as { text?: string } | undefined

    if (event.type === 'message.delta' && payload?.text) handlers.onDelta(payload.text)
    if (event.type === 'tool.start') handlers.onTool()
    if (event.type === 'message.complete') handlers.onComplete(typeof payload?.text === 'string' ? payload.text : '')
  })
}

export async function transcribe(token: string, blob: Blob): Promise<string> {
  const dataUrl = await blobToDataUrl(blob)
  const response = await fetch('/api/audio/transcribe', {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ data_url: dataUrl, mime_type: blob.type || 'audio/webm' })
  })

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(body.detail || `语音识别失败（${response.status}）`)
  }

  const body = (await response.json()) as { transcript?: string }

  return (body.transcript ?? '').trim()
}

export async function speak(token: string, text: string): Promise<string> {
  const response = await fetch('/api/audio/speak', {
    method: 'POST',
    headers: authHeaders(token),
    body: JSON.stringify({ text })
  })

  if (!response.ok) throw new Error('语音还没准备好')
  const body = (await response.json()) as { data_url?: string }

  if (!body.data_url) throw new Error('语音还没准备好')

  return body.data_url
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()

    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(new Error('录音读不出来'))
    reader.readAsDataURL(blob)
  })
}
