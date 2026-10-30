import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { ServerRequest } from '@hermes/shared'
import { History, ListTodo, Menu, Mic, MicOff, Unplug, Users, Volume2, VolumeX, type LucideIcon } from 'lucide-react'
import {
  claimSchedules,
  confirmSchedule,
  declineSchedule,
  ensureSession,
  interruptSession,
  listSessions,
  loadSchedules,
  openGateway,
  readSession,
  settleSchedule,
  speak,
  submitText,
  transcribe,
  watchTurn,
  type PastSession,
  type ScheduleBoard,
  type ScheduleProposal,
  type ScheduleRead,
  type TurnLine
} from '../api'
import { BackButton } from '../BackButton'
import { ScheduleSheet } from '../ScheduleSheet'
import { findCharacter, type Mood } from '../characters'
import type { FigureHandle } from '../live2d/Stage'
import { heardLevel, lookAt, type LookPoint } from '../live2d/pose'
import { type PocketProfile } from '../flow'
import { speechText, voiceReply } from '../speech'
import { nearestTodoSpeech, remindAsFriend, wantsTodoList } from '../todos'
import { openEar, type Ear } from '../listen'
import { measurePlayback } from '../audio'
import { Portrait } from '../Portrait'
import { Markdown } from '../Markdown'
import { Waveform } from '../Waveform'
import { ClarifyCard } from '../ClarifyCard'
import { ConfirmDialog } from '../ConfirmDialog'
import { rememberSession, setSpeakerEnabled, setVoiceEnabled, speakerEnabled, storedSession, voiceEnabled } from '../storage'
import { closeDueNotice, dueOutlet, readNoticePermission, requestNoticePermission, showDueNotice } from '../dueNotice'

interface HomeProps {
  profile: PocketProfile
  onSwitch: () => void
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

export function Home({ profile, onReset, onDestroy, onSwitch }: HomeProps) {
  const character = findCharacter(profile.characterId)
  const [mood, setMood] = useState<Mood>('idle')
  const [muted, setMuted] = useState(!voiceEnabled())
  const [speakerOn, setSpeakerOn] = useState(speakerEnabled())
  const [caption, setCaption] = useState('')
  const [status, setStatus] = useState('正在过来…')
  const [sheet, setSheet] = useState(false)
  const [draft, setDraft] = useState('')
  const [turns, setTurns] = useState<TurnLine[]>([])
  const [past, setPast] = useState<PastSession[] | null>(null)
  const [reading, setReading] = useState<{ title: string; lines: TurnLine[] } | null>(null)
  const [ask, setAsk] = useState<Ask | null>(null)
  const [proposal, setProposal] = useState<ScheduleProposal | null>(null)
  const [board, setBoard] = useState<ScheduleBoard | null>(null)
  const [todos, setTodos] = useState(false)
  const [clarify, setClarify] = useState<ServerRequest | null>(null)
  const [voiceNote, setVoiceNote] = useState('')
  const [inputLevel, setInputLevel] = useState(0)
  const [outputLevel, setOutputLevel] = useState(0)
  const [dueToast, setDueToast] = useState<{ id: string; text: string } | null>(null)
  const [menu, setMenu] = useState(false)
  const [confirmDisconnect, setConfirmDisconnect] = useState(false)
  const sessionId = useRef('')
  const clientRef = useRef<Awaited<ReturnType<typeof openGateway>> | null>(null)
  const earRef = useRef<Ear | null>(null)
  const speaking = useRef(false)
  const queue = useRef<string[]>([])
  const pending = useRef('')
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const generation = useRef(0)
  const sayRef = useRef<(text: string) => Promise<void>>(async () => {})
  const openTodosRef = useRef<(announce: boolean) => Promise<void>>(async () => {})
  const proposalRef = useRef<ScheduleProposal | null>(null)
  const clarifyRef = useRef<string | null>(null)
  const noticeAsked = useRef(false)
  const clarifyVoiceRef = useRef<(text: string) => boolean>(() => false)
  const cardRef = useRef(false)
  const pressTimer = useRef(0)
  const holding = useRef(false)
  const origin = useRef({ x: 0, y: 0 })
  const mutedRef = useRef(muted)
  const speakerRef = useRef(speakerOn)
  const busyRef = useRef(false)
  const figureRef = useRef<FigureHandle>(null)
  const look = useRef<LookPoint>({ x: 0, y: 0 })

  mutedRef.current = muted
  speakerRef.current = speakerOn
  cardRef.current = Boolean(ask || proposal || clarify)

  const stopSpeech = () => {
    generation.current += 1
    queue.current = []
    pending.current = ''
    audioRef.current?.pause()
    speaking.current = false
    setOutputLevel(0)
  }

  const interruptReply = () => {
    if (!speaking.current && !busyRef.current && !queue.current.length) return
    stopSpeech()
    busyRef.current = false
    setMood(mutedRef.current ? 'wait' : 'idle')
    if (sessionId.current && clientRef.current) {
      void interruptSession(clientRef.current, sessionId.current).catch(() => setVoiceNote('停止生成失败，请稍后重试'))
    }
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
    setMood('think')
    try {
      const url = await speak(token, sentence)

      if (ticket !== generation.current) return
      await play(url, ticket, generation, audioRef, () => {
        setMood('speak')
      }, level => {
        if (ticket === generation.current) setOutputLevel(level)
      })
    } catch {
      if (ticket !== generation.current) return
      setVoiceNote('语音还没准备好，可以先打字')
    } finally {
      if (ticket === generation.current) {
        speaking.current = false
        setOutputLevel(0)
        void pump(token)
      }
    }
  }

  const enqueue = (sentence: string) => {
    if (!speakerRef.current) {
      return
    }

    queue.current.push(sentence)
    void pump(profile.token)
  }

  const showRead = (read: ScheduleRead) => {
    if (read.type === 'list') {
      clarifyRef.current = null
      void openTodosRef.current(true)
      return
    }
    if (read.type === 'clarify') {
      clarifyRef.current = read.pending
      setCaption(read.question)
      if (speakerRef.current) enqueue(read.question)
      else setMood('wait')
      return
    }
    if (read.type === 'saved') {
      clarifyRef.current = null
      setCaption(read.speech)
      if (speakerRef.current) enqueue(read.speech)
      void loadSchedules(profile.token).then(setBoard).catch(() => undefined)
      return
    }
    if (read.type === 'proposal') {
      clarifyRef.current = null
      const next = { id: read.id, title: read.title, body: read.body, allow: read.allow, deny: read.deny }

      proposalRef.current = next
      cardRef.current = true
      setProposal(next)
      setMood('wait')
    }
  }

  const finishProposal = async (accepted: boolean) => {
    const current = proposalRef.current

    if (!current) return
    proposalRef.current = null
    setProposal(null)
    try {
      const result = accepted
        ? await confirmSchedule(profile.token, current.id)
        : await declineSchedule(profile.token, current.id)

      setCaption(result.speech)
      if (speakerRef.current) enqueue(result.speech)
      setMood(mutedRef.current ? 'wait' : 'idle')
      if (board) setBoard(await loadSchedules(profile.token))
    } catch (error) {
      setVoiceNote(error instanceof Error ? error.message : '没记下')
    }
  }

  const say = async (text: string) => {
    const clean = text.trim()

    if (!clean) return
    const open = proposalRef.current

    if (open && (clean === '确认' || clean === open.allow)) {
      await finishProposal(true)
      return
    }
    if (open && (clean === '取消' || clean === '不用' || clean === open.deny)) {
      await finishProposal(false)
      return
    }
    if (clarify) {
      if (!clarifyVoiceRef.current(clean)) setVoiceNote('没对上。点一条，或者说得再具体一点')
      return
    }
    if (wantsTodoList(clean)) {
      await openTodosRef.current(true)
      return
    }
    if (!sessionId.current || !clientRef.current) return
    stopSpeech()
    busyRef.current = false
    setMood('think')
    setTurns(current => [...current, { role: 'user', text: clean }])
    setCaption('')
    pending.current = ''
    const ticket = generation.current
    try {
      if (sessionId.current) await interruptSession(clientRef.current!, sessionId.current)
      if (ticket !== generation.current) return
      busyRef.current = true
      await submitText(clientRef.current!, sessionId.current, clean, speakerRef.current)
    } catch {
      if (ticket !== generation.current) return
      busyRef.current = false
      setStatus('这句话没送出去')
      setMood('idle')
    }
  }

  sayRef.current = say

  const heard = (blob: Blob) => {
    void transcribe(profile.token, blob)
      .then(text => {
        if (text) void sayRef.current(text)
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
        setClarify(request)
        setMood('wait')

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
            if (!busyRef.current) return
            pending.current += chunk
            setCaption(voiceReply(pending.current).text)
          },
          onTool: () => { if (busyRef.current) setMood('think') },
          onSchedule: read => { if (read.type !== 'chat') showRead(read) },
          onComplete: text => {
            if (!busyRef.current) return
            busyRef.current = false
            const reply = voiceReply(text || pending.current)
            const full = reply.text

            pending.current = ''
            if (full) setTurns(current => [...current, { role: 'assistant', text: full }])
            setCaption(full)
            if (reply.summary) enqueue(speechText(reply.summary))
            else if (speakerRef.current && full) setVoiceNote('这次回复没有语音摘要')
            if (!queue.current.length && !speaking.current) setMood(mutedRef.current ? 'wait' : 'idle')
          }
        })
        setStatus('')
        const ear = await openEar(
          heard,
          () => !mutedRef.current && !holding.current && !busyRef.current && !speaking.current,
          level => {
            if (!cancelled) setInputLevel(level)
          },
          interruptReply
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

  useEffect(() => {
    let stop = false
    let mode: 'backlog' | 'live' = 'backlog'
    const skipped = new Set<string>()

    const speakLine = async (text: string) => {
      const ticket = generation.current

      setCaption(text)
      if (!speakerRef.current) {
        setVoiceNote('文字回复')
        return ticket === generation.current
      }
      speaking.current = true
      setMood('think')
      try {
        const url = await speak(profile.token, text)

        if (ticket !== generation.current) return false
        await play(url, ticket, generation, audioRef, () => setMood('speak'), level => {
          if (ticket === generation.current) setOutputLevel(level)
        })
        return ticket === generation.current
      } catch {
        setVoiceNote('语音还没准备好，可以先看字幕')
        return false
      } finally {
        if (ticket === generation.current) {
          speaking.current = false
          setOutputLevel(0)
          if (!busyRef.current) setMood(mutedRef.current ? 'wait' : 'idle')
        }
      }
    }

    let prefer: string | null = null

    const tick = async () => {
      if (stop || busyRef.current || speaking.current || cardRef.current || clarifyRef.current) return
      let loaded: ScheduleBoard
      try {
        loaded = await loadSchedules(profile.token)
      } catch {
        return
      }
      if (stop || busyRef.current || speaking.current || cardRef.current || clarifyRef.current) return
      const pending = loaded.unheard.filter(item => !skipped.has(item.id))
      if (!pending.length) return
      const chosen = prefer
      prefer = null
      if (!chosen && dueOutlet(document.hidden, readNoticePermission()) !== 'speak') {
        const next = pending[0]
        if (dueOutlet(true, readNoticePermission()) === 'notify') {
          showDueNotice(next.id, profile.name, next.text, id => {
            prefer = id
            void tick()
          })
        }
        return
      }
      const thisMode = mode

      mode = 'live'
      const opened = chosen ? pending.filter(item => item.id === chosen).slice(0, 1) : []
      const batch = opened.length ? opened : thisMode === 'backlog' ? pending.slice(0, 3) : pending.slice(0, 1)

      if (!chosen && thisMode === 'backlog') pending.slice(3).forEach(item => skipped.add(item.id))
      if (!batch.length) return
      batch.forEach(item => closeDueNotice(item.id))
      const claimed = await claimSchedules(profile.token, batch.map(item => item.id)).catch(() => ({ items: [] }))

      for (const item of claimed.items) {
        if (stop || cardRef.current || clarifyRef.current) {
          await settleSchedule(profile.token, item.id, false).catch(() => undefined)
          continue
        }
        const spoken = remindAsFriend(item.text)
        setDueToast({ id: item.id, text: item.text })
        const cued = await speakLine(spoken.cue)
        if (!cued) {
          await settleSchedule(profile.token, item.id, false).catch(() => undefined)
          break
        }
        const delivered = await speakLine(spoken.line)

        await settleSchedule(profile.token, item.id, delivered).catch(() => undefined)
        if (!delivered) break
      }
    }

    void tick()
    const timer = window.setInterval(() => { void tick() }, 4000)

    return () => {
      stop = true
      window.clearInterval(timer)
    }
  }, [profile.token])

  const covered = Boolean(sheet || past || todos || ask || proposal || clarify || confirmDisconnect)

  useEffect(() => {
    if (covered) setMenu(false)
  }, [covered])

  if (!character) return null

  const toggleMute = () => {
    const next = !muted

    setMuted(next)
    setVoiceEnabled(!next)
    if (!speaking.current && !busyRef.current) setMood(next ? 'wait' : 'idle')
  }

  const toggleSpeaker = () => {
    const next = !speakerOn

    setSpeakerOn(next)
    setSpeakerEnabled(next)
    if (!next) {
      stopSpeech()
      if (!busyRef.current) setMood(mutedRef.current ? 'wait' : 'idle')
    }
  }

  const onPointerDown = (event: ReactPointerEvent) => {
    if (!noticeAsked.current && readNoticePermission() === 'default') {
      noticeAsked.current = true
      void requestNoticePermission()
    }
    origin.current = { x: event.clientX, y: event.clientY }
    window.clearTimeout(pressTimer.current)
    pressTimer.current = window.setTimeout(() => {
      interruptReply()
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
      setMenu(false)
      setSheet(true)

      return
    }

    if (dy > 48 && Math.abs(dy) > Math.abs(dx)) {
      setSheet(false)

      return
    }

    const onStage = event.target instanceof Element && event.target.closest('.stage')

    if (Math.abs(dx) < 16 && Math.abs(dy) < 16) {
      if (speaking.current || busyRef.current) interruptReply()
      else if (onStage) figureRef.current?.nod()
    }

    look.current = { x: 0, y: 0 }
  }

  const openTodos = async (announce = false) => {
    setSheet(false)
    try {
      const loaded = await loadSchedules(profile.token)
      setBoard(loaded)
      setTodos(true)
      if (!announce) return
      const speech = nearestTodoSpeech(loaded.items)
      setCaption(speech)
      if (speakerRef.current) enqueue(speech)
    } catch {
      setVoiceNote('待办没有打开')
    }
  }
  openTodosRef.current = openTodos

  const quietPointer = (event: ReactPointerEvent) => {
    event.stopPropagation()
  }

  const openPast = () => {
    setMenu(false)
    setReading(null)
    const client = clientRef.current

    if (!client) return
    void listSessions(client).then(setPast).catch(() => setPast([]))
  }

  const disconnect = () => {
    setConfirmDisconnect(false)
    void onDestroy().catch(() => setVoiceNote('设备还没有断开，请稍后再试'))
  }

  const menuEntries: Array<{ id: string; label: string; icon: LucideIcon; danger?: boolean; run: () => void }> = [
    { id: 'past', label: '以前的对话', icon: History, run: openPast },
    { id: 'todos', label: '待办', icon: ListTodo, run: () => { setMenu(false); void openTodos() } },
    { id: 'switch', label: '切换角色', icon: Users, run: () => { setMenu(false); onSwitch() } },
    { id: 'disconnect', label: '断开此设备', icon: Unplug, danger: true, run: () => { setMenu(false); setConfirmDisconnect(true) } }
  ]

  const onPointerMove = (event: ReactPointerEvent) => {
    const stage = event.target instanceof Element ? event.target.closest('.stage') : null

    if (!stage) return
    look.current = lookAt(event.clientX, event.clientY, stage.getBoundingClientRect())
  }

  return (
    <main
      className="home"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={() => {
        look.current = { x: 0, y: 0 }
      }}
      onContextMenu={event => event.preventDefault()}
    >
      {menu && !covered ? (
        <button
          type="button"
          className="menu-backdrop"
          aria-label="关闭菜单"
          onPointerDown={quietPointer}
          onPointerUp={quietPointer}
          onClick={() => setMenu(false)}
        />
      ) : null}
      {covered ? null : <p className="who top-name">{profile.name}</p>}
      {covered ? null : (
        <div className="corner-menu" onPointerDown={quietPointer} onPointerUp={quietPointer}>
          <button
            type="button"
            className="ghost icon-entry"
            aria-label="菜单"
            aria-expanded={menu}
            title="菜单"
            onClick={() => setMenu(open => !open)}
          >
            <Menu size={22} aria-hidden="true" />
          </button>
          {menu ? (
            <div className="menu-panel" role="menu">
              {menuEntries.map(entry => {
                const Icon = entry.icon

                return (
                  <button
                    key={entry.id}
                    type="button"
                    role="menuitem"
                    className={entry.danger ? 'menu-item danger' : 'menu-item'}
                    onClick={entry.run}
                  >
                    <Icon size={20} aria-hidden="true" />
                    {entry.label}
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>
      )}
      {covered ? null : (
        <div className="corner-speaker" onPointerDown={quietPointer} onPointerUp={quietPointer}>
          <button
            type="button"
            className={speakerOn ? 'ghost icon-entry' : 'ghost icon-entry off'}
            aria-label={speakerOn ? '关闭声音' : '开启声音'}
            title={speakerOn ? '关闭声音' : '开启声音'}
            onClick={toggleSpeaker}
          >
            {speakerOn ? <Volume2 size={22} aria-hidden="true" /> : <VolumeX size={22} aria-hidden="true" />}
          </button>
        </div>
      )}
      {dueToast && (
        <div className="due-toast" role="status">
          <div className="due-toast-copy">
            <p className="due-toast-cue">想提醒你</p>
            <p className="due-toast-body">{dueToast.text}</p>
          </div>
          <button
            type="button"
            className="due-toast-dismiss"
            onPointerDown={event => event.stopPropagation()}
            onClick={() => setDueToast(null)}
          >
            知道了
          </button>
        </div>
      )}
      <Portrait
        character={character}
        figure={figureRef}
        look={look.current}
        input={{ mood, muted, confirming: Boolean(ask || clarify || proposal), inputLevel, outputLevel }}
      />
      <section className="lower">
        <p className="note" role="status">{status || (ask || proposal || clarify ? '等你确认' : mood === 'speak' ? '正在说话' : mood === 'think' ? '正在思考' : inputLevel > heardLevel ? '正在听' : muted ? '麦克风已关闭' : voiceNote ? '文字回复' : '等你开口')}</p>
        <div className="audio-levels">
          <Waveform label="你" level={inputLevel} />
          <Waveform label={profile.name} level={outputLevel} />
        </div>
        {mood === 'speak' || mood === 'think' ? <button type="button" className="ghost" onPointerDown={event => event.stopPropagation()} onPointerUp={event => event.stopPropagation()} onClick={interruptReply}>停止回复</button> : null}
        <Caption text={status || caption} fallback={muted ? '她听不见了。点麦克风，或者上滑打字。' : '点一下，或者按住跟她说'} live={speaking.current} />
        {voiceNote ? <p className="note">{voiceNote}</p> : null}
        {status.startsWith('接不上') ? (
          <button type="button" onPointerDown={event => event.stopPropagation()} onClick={onReset}>
            重新填写连接码
          </button>
        ) : null}
        <p className="hint">上滑看对话</p>
        <button type="button" className={muted ? 'mic off' : 'mic'}
          aria-label={muted ? '开启麦克风' : '关闭麦克风'} title={muted ? '开启麦克风' : '关闭麦克风'}
          onPointerDown={event => event.stopPropagation()} onClick={toggleMute}>
          {muted ? <MicOff size={28} aria-hidden="true" /> : <Mic size={28} aria-hidden="true" />}
        </button>
      </section>
      {sheet ? (
        <section className="overlay" onPointerDown={event => event.stopPropagation()}>
          <BackButton onClick={() => setSheet(false)} />
          <div className="log">
            {turns.map((turn, index) => (
              <div className="message" key={`${turn.role}-${index}`}>
                <span className="speaker">{turn.role === 'user' ? '你' : profile.name}</span>
                <Markdown text={turn.text} />
              </div>
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
          <BackButton onClick={() => (reading ? setReading(null) : setPast(null))} />
          {reading ? (
            <>
              <h1>{reading.title}</h1>
              <div className="log">
                {reading.lines.map((line, index) => (
                  <div className="message" key={`${line.role}-${index}`}>
                    <span className="speaker">{line.role === 'user' ? '你' : profile.name}</span>
                    <Markdown text={line.text} />
                  </div>
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
      {todos ? (
        <ScheduleSheet
          board={board}
          onClose={() => setTodos(false)}
          onUtterance={text => { void sayRef.current(text) }}
        />
      ) : null}
      {confirmDisconnect ? (
        <ConfirmDialog
          title="断开此设备？"
          body="会清除本地缓存。角色、记忆、历史对话和其他设备不受影响。"
          confirmLabel="断开"
          danger
          onConfirm={disconnect}
          onCancel={() => setConfirmDisconnect(false)}
        />
      ) : null}
      {ask ? <AskCard ask={ask} onClose={() => setAsk(null)} /> : null}
      {proposal ? (
        <AskCard
          ask={{
            title: proposal.title,
            body: proposal.body,
            needsText: false,
            allow: proposal.allow,
            deny: proposal.deny,
            onAllow: () => { void finishProposal(true) },
            onDeny: () => { void finishProposal(false) }
          }}
          onClose={() => {}}
        />
      ) : null}
      {clarify ? <ClarifyCard key={clarify.id} request={clarify} voiceRef={clarifyVoiceRef} onClose={() => setClarify(null)} /> : null}
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
  const viewport = useRef<HTMLDivElement>(null)
  const follow = useRef(true)

  useEffect(() => {
    if (follow.current && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight
  }, [text, fallback])

  return (
    <div className={`caption caption-scroll${text ? '' : ' quiet'}`} ref={viewport}
      tabIndex={0} role="region" aria-label="对话字幕" aria-live={live ? 'off' : 'polite'}
      onPointerDown={event => event.stopPropagation()} onPointerUp={event => event.stopPropagation()}
      onScroll={event => {
        const element = event.currentTarget
        follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 32
      }}>
      <Markdown text={text || fallback} />
    </div>
  )
}

function play(
  url: string,
  ticket: number,
  generation: { current: number },
  slot: { current: HTMLAudioElement | null },
  onPlaying: () => void,
  onLevel: (level: number) => void
): Promise<void> {
  return new Promise((resolve, reject) => {
    const audio = new Audio(url)
    let stopMeter = () => {}
    const finish = () => {
      stopMeter()
      resolve()
    }

    slot.current = audio
    audio.onplaying = () => {
      if (generation.current !== ticket) {
        audio.pause()
        return
      }
      onPlaying()
      try {
        stopMeter()
        stopMeter = measurePlayback(audio, onLevel)
      } catch {
        onLevel(0)
      }
    }
    audio.onended = finish
    audio.onpause = finish
    audio.onerror = () => {
      stopMeter()
      reject(new Error('播放失败'))
    }
    void audio.play().then(() => {
      if (generation.current !== ticket) {
        audio.pause()
        resolve()
      }
    }).catch(error => {
      stopMeter()
      reject(error)
    })
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
