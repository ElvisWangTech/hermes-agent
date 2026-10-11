import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { ServerRequest } from '@hermes/shared'
import {
  ensureSession,
  interruptSession,
  listSessions,
  openGateway,
  readSession,
  speak,
  submitText,
  transcribe,
  watchTurn,
  type PastSession,
  type TurnLine
} from '../api'
import { findCharacter, type Mood } from '../characters'
import { takeSentences, type PocketProfile } from '../flow'
import { openEar, type Ear } from '../listen'
import { Portrait } from '../Portrait'
import { rememberSession, setVoiceEnabled, storedSession, voiceEnabled } from '../storage'

interface HomeProps {
  profile: PocketProfile
  onReset: () => void
  onDestroy: () => Promise<void>
}

interface Ask {
  title: string
  body: string
  needsText: boolean
  allow: string
  deny: string
  onAllow: (text: string) => void
  onDeny: () => void
}

const valueMethods = new Set(['sudo', 'secret', 'vault.code', 'display.install.sudo', 'vault.unlock_prompt'])

export function Home({ profile, onReset, onDestroy }: HomeProps) {
  const character = findCharacter(profile.characterId)
  const [mood, setMood] = useState<Mood>('idle')
  const [muted, setMuted] = useState(!voiceEnabled())
  const [hop, setHop] = useState(false)
  const [caption, setCaption] = useState('')
  const [status, setStatus] = useState('正在过来…')
  const [sheet, setSheet] = useState(false)
  const [draft, setDraft] = useState('')
  const [turns, setTurns] = useState<TurnLine[]>([])
  const [past, setPast] = useState<PastSession[] | null>(null)
  const [reading, setReading] = useState<{ title: string; lines: TurnLine[] } | null>(null)
  const [ask, setAsk] = useState<Ask | null>(null)
  const [voiceNote, setVoiceNote] = useState('')
  const sessionId = useRef('')
  const clientRef = useRef<Awaited<ReturnType<typeof openGateway>> | null>(null)
  const earRef = useRef<Ear | null>(null)
  const speaking = useRef(false)
  const queue = useRef<string[]>([])
  const pending = useRef('')
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const generation = useRef(0)
  const pressTimer = useRef(0)
  const holding = useRef(false)
  const origin = useRef({ x: 0, y: 0 })
  const mutedRef = useRef(muted)
  const busyRef = useRef(false)

  mutedRef.current = muted

  const stopSpeech = () => {
    generation.current += 1
    queue.current = []
    pending.current = ''
    audioRef.current?.pause()
    speaking.current = false
  }

  const pump = async (token: string) => {
    if (speaking.current) return
    const sentence = queue.current.shift()

    if (!sentence) {
      if (!busyRef.current) setMood(mutedRef.current ? 'wait' : 'idle')

      return
    }

    const ticket = generation.current

    speaking.current = true
    setMood('speak')
    setCaption(current => (current ? `${current}${sentence}` : sentence))
    try {
      const url = await speak(token, sentence)

      if (ticket !== generation.current) return
      await play(url, ticket, generation, audioRef)
    } catch {
      setVoiceNote('语音还没准备好，可以先打字')
      setCaption(current => current || sentence)
    } finally {
      speaking.current = false
      if (ticket === generation.current) void pump(token)
    }
  }

  const enqueue = (sentence: string) => {
    if (mutedRef.current) {
      setCaption(current => (current ? `${current}${sentence}` : sentence))

      return
    }

    queue.current.push(sentence)
    void pump(profile.token)
  }

  const say = async (text: string) => {
    const clean = text.trim()

    if (!clean || busyRef.current || !sessionId.current || !clientRef.current) return
    stopSpeech()
    busyRef.current = true
    setMood('listen')
    setTurns(current => [...current, { role: 'user', text: clean }])
    setCaption('')
    pending.current = ''
    try {
      if (sessionId.current) await interruptSession(clientRef.current!, sessionId.current)
      await submitText(clientRef.current!, sessionId.current, clean)
    } catch {
      busyRef.current = false
      setStatus('这句话没送出去')
      setMood('idle')
    }
  }

  const heard = (blob: Blob) => {
    void transcribe(profile.token, blob)
      .then(text => {
        if (text) void say(text)
        else setVoiceNote('没听清，可以按住再说，或者上滑打字')
      })
      .catch((error: unknown) => {
        setVoiceNote(error instanceof Error ? error.message : '语音识别失败，可以先打字')
      })
  }

  useEffect(() => {
    const clientHolder: { current: Awaited<ReturnType<typeof openGateway>> | null } = { current: null }
    let unwatch = () => {}
    let cancelled = false

    const onRequest = (request: ServerRequest) => {
      if (request.method === 'approval') {
        const params = request.params as { command?: string; description?: string }

        setAsk({
          title: '要运行这条命令吗？',
          body: params.description || params.command || '',
          needsText: false,
          allow: '允许',
          deny: '不用',
          onAllow: () => request.respond({ choice: 'once' }),
          onDeny: () => request.respond({ choice: 'deny' })
        })
        setMood('wait')

        return true
      }

      if (request.method === 'clarify') {
        const params = request.params as { question?: string | null }

        setAsk({
          title: params.question || '她想确认一件事',
          body: '',
          needsText: true,
          allow: '告诉她',
          deny: '不用',
          onAllow: text => request.respond({ answer: text }),
          onDeny: () => request.respond({ answer: '' })
        })

        return true
      }

      if (valueMethods.has(request.method)) {
        setAsk({
          title: '需要你填一下',
          body: '',
          needsText: true,
          allow: '好',
          deny: '不用',
          onAllow: text => request.respond({ value: text }),
          onDeny: () => request.respond({ value: '' })
        })

        return true
      }

      request.fail(-32601, 'unsupported')

      return true
    }

    void openGateway(profile.token, onRequest)
      .then(async client => {
        if (cancelled) {
          client.close?.()

          return
        }

        clientHolder.current = client
        clientRef.current = client
        const id = await ensureSession(client, storedSession(), profile.name)

        if (cancelled) return
        sessionId.current = id
        rememberSession(id)
        unwatch = watchTurn(client, id, {
          onDelta: chunk => {
            busyRef.current = true
            pending.current += chunk
            if (mutedRef.current) {
              setCaption(current => current + chunk)

              return
            }

            const split = takeSentences(pending.current)

            pending.current = split.rest
            split.ready.forEach(enqueue)
          },
          onTool: () => setMood('think'),
          onComplete: text => {
            busyRef.current = false
            const rest = pending.current.trim()
            const full = text.trim()

            pending.current = ''
            if (full) setTurns(current => [...current, { role: 'assistant', text: full }])
            if (rest) enqueue(rest)
            else if (mutedRef.current && full) setCaption(full)
            if (!rest && !queue.current.length) setMood(mutedRef.current ? 'wait' : 'idle')
          }
        })
        setStatus('')
        const ear = await openEar(
          heard,
          () => !mutedRef.current && !busyRef.current && !holding.current && !speaking.current
        ).catch(() => {
          setVoiceNote('语音还没准备好，可以先打字')

          return null
        })

        if (cancelled) {
          ear?.stop()

          return
        }

        earRef.current = ear
      })
      .catch(() => setStatus('接不上。连接码可能已经换过了。'))

    return () => {
      cancelled = true
      unwatch()
      earRef.current?.stop()
      stopSpeech()
      clientHolder.current?.close?.()
    }
    // The voice loop belongs to this profile. Reconnecting mid-conversation would drop the turn.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile.name, profile.token])

  if (!character) return null

  const toggleMute = () => {
    const next = !muted

    setMuted(next)
    setVoiceEnabled(!next)
    setMood(next ? 'wait' : 'idle')
    if (next) stopSpeech()
  }

  const onPointerDown = (event: ReactPointerEvent) => {
    origin.current = { x: event.clientX, y: event.clientY }
    window.clearTimeout(pressTimer.current)
    pressTimer.current = window.setTimeout(() => {
      holding.current = true
      setMood('listen')
      earRef.current?.holdStart()
    }, 450)
  }

  const onPointerUp = (event: ReactPointerEvent) => {
    window.clearTimeout(pressTimer.current)
    if (holding.current) {
      holding.current = false
      void earRef.current?.holdEnd().then(blob => {
        if (blob) heard(blob)
        else setMood(muted ? 'wait' : 'idle')
      })

      return
    }

    const dx = event.clientX - origin.current.x
    const dy = event.clientY - origin.current.y

    if (dy < -48 && Math.abs(dy) > Math.abs(dx)) {
      setSheet(true)

      return
    }

    if (dy > 48 && Math.abs(dy) > Math.abs(dx)) {
      setSheet(false)

      return
    }

    if (Math.abs(dx) < 16 && Math.abs(dy) < 16) {
      if (speaking.current || busyRef.current) {
        stopSpeech()
        if (sessionId.current && clientRef.current) void interruptSession(clientRef.current, sessionId.current)
        busyRef.current = false
        setMood('idle')
      } else {
        setHop(true)
        window.setTimeout(() => setHop(false), 280)
      }
    }
  }

  return (
    <main className="home" onPointerDown={onPointerDown} onPointerUp={onPointerUp} onContextMenu={event => event.preventDefault()}>
      <button
        type="button"
        className="ghost destroy-entry"
        onPointerDown={event => event.stopPropagation()}
        onClick={() => {
          if (!window.confirm('销毁这个角色？只会清除名字记忆和本地缓存，历史对话不会删除。')) return
          void onDestroy().catch(() => setVoiceNote('角色还没有销毁，请稍后再试'))
        }}
      >
        销毁角色
      </button>
      <button
        type="button"
        className="ghost past-entry"
        onPointerDown={event => event.stopPropagation()}
        onClick={() => {
          setReading(null)
          const client = clientRef.current

          if (!client) return
          void listSessions(client).then(setPast).catch(() => setPast([]))
        }}
      >
        以前
      </button>
      <Portrait character={character} mood={mood} muted={muted} hop={hop} />
      <section className="lower">
        <p className="who">{profile.name}</p>
        <Caption text={status || caption} fallback={muted ? '她听不见了。点麦克风，或者上滑打字。' : '点一下，或者按住跟她说'} live={speaking.current} />
        {voiceNote ? <p className="note">{voiceNote}</p> : null}
        {status.startsWith('接不上') ? (
          <button type="button" onPointerDown={event => event.stopPropagation()} onClick={onReset}>
            重新填写连接码
          </button>
        ) : null}
        <p className="hint">上滑看对话</p>
        <button type="button" className={muted ? 'mic off' : 'mic'} onPointerDown={event => event.stopPropagation()} onClick={toggleMute}>
          {muted ? '听不见' : '在听'}
        </button>
      </section>
      {sheet ? (
        <section className="overlay" onPointerDown={event => event.stopPropagation()}>
          <button type="button" className="back" onClick={() => setSheet(false)}>
            返回
          </button>
          <div className="log">
            {turns.map((turn, index) => (
              <p key={`${turn.role}-${index}`}>
                <span>{turn.role === 'user' ? '你' : profile.name}</span>
                {turn.text}
              </p>
            ))}
          </div>
          <form
            onSubmit={event => {
              event.preventDefault()
              const text = draft.trim()

              setDraft('')
              if (text) void say(text)
            }}
          >
            <input value={draft} placeholder="输入文字" onChange={event => setDraft(event.target.value)} />
          </form>
        </section>
      ) : null}
      {past ? (
        <section className="overlay" onPointerDown={event => event.stopPropagation()}>
          <button type="button" className="back" onClick={() => (reading ? setReading(null) : setPast(null))}>
            返回
          </button>
          {reading ? (
            <>
              <h1>{reading.title}</h1>
              <div className="log">
                {reading.lines.map((line, index) => (
                  <p key={`${line.role}-${index}`}>
                    <span>{line.role === 'user' ? '你' : profile.name}</span>
                    {line.text}
                  </p>
                ))}
              </div>
            </>
          ) : (
            <>
              <h1>以前的对话</h1>
              <div className="log">
                {past.map(row => (
                  <button
                    key={row.id}
                    type="button"
                    className="session"
                    onClick={() => {
                      const client = clientRef.current

                      if (!client) return
                      void readSession(client, row.id).then(lines => setReading({ title: row.title, lines }))
                    }}
                  >
                    <strong>{whenLabel(row.started_at)}</strong>
                    <span>{row.preview || row.title}</span>
                  </button>
                ))}
                {past.length === 0 ? <p className="quiet">还没有更早的对话</p> : null}
              </div>
            </>
          )}
        </section>
      ) : null}
      {ask ? <AskCard ask={ask} onClose={() => setAsk(null)} /> : null}
    </main>
  )
}

function AskCard({ ask, onClose }: { ask: Ask; onClose: () => void }) {
  const [text, setText] = useState('')

  return (
    <section className="overlay ask-card" onPointerDown={event => event.stopPropagation()}>
      <h1>{ask.title}</h1>
      {ask.body ? <p className="caption">{ask.body}</p> : null}
      {ask.needsText ? <input value={text} onChange={event => setText(event.target.value)} /> : null}
      <div className="row">
        <button
          type="button"
          onClick={() => {
            ask.onDeny()
            onClose()
          }}
        >
          {ask.deny}
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => {
            ask.onAllow(text.trim())
            onClose()
          }}
        >
          {ask.allow}
        </button>
      </div>
    </section>
  )
}

function Caption({ text, fallback, live }: { text: string; fallback: string; live: boolean }) {
  if (!text) return <p className="caption quiet">{fallback}</p>
  const pieces = text.split(/(?<=[。！？])/)
  const tail = pieces.pop() ?? ''

  return (
    <p className="caption">
      {pieces.join('')}
      <span className={live ? 'clause' : undefined}>{tail}</span>
    </p>
  )
}

function play(
  url: string,
  ticket: number,
  generation: { current: number },
  slot: { current: HTMLAudioElement | null }
): Promise<void> {
  return new Promise((resolve, reject) => {
    const audio = new Audio(url)

    slot.current = audio
    audio.onended = () => resolve()
    audio.onpause = () => resolve()
    audio.onerror = () => reject(new Error('播放失败'))
    void audio.play().then(() => {
      if (generation.current !== ticket) {
        audio.pause()
        resolve()
      }
    }).catch(reject)
  })
}

function whenLabel(stamp: number): string {
  if (!stamp) return '更早'
  const ms = stamp > 1e12 ? stamp : stamp * 1000
  const date = new Date(ms)
  const start = new Date()

  start.setHours(0, 0, 0, 0)
  const day = new Date(date)

  day.setHours(0, 0, 0, 0)
  const diff = Math.round((start.getTime() - day.getTime()) / 86400000)

  if (diff === 0) return '今天'
  if (diff === 1) return '昨天'
  if (diff > 1 && diff < 7) return '这周'

  return `${date.getMonth() + 1}月${date.getDate()}日`
}
