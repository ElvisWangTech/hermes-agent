import { useState } from 'react'
import { exchangeCode } from '../api'

interface ConnectProps {
  onToken: (token: string) => void
}

export function Connect({ onToken }: ConnectProps) {
  const [code, setCode] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const go = async () => {
    if (!code.trim() || busy) return
    setBusy(true)
    let token: string
    try {
      token = await exchangeCode(code.trim())
    } catch (error) {
      setNote(error instanceof Error ? error.message : '连接失败')
      setBusy(false)
      return
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      onToken(token)
      return
    }
    void navigator.mediaDevices
      ?.getUserMedia({ audio: true })
      .then(stream => {
        stream.getTracks().forEach(track => track.stop())
      })
      .catch(() => {
        setNote('麦克风还没用上，可以先打字。')
      })
      .finally(() => onToken(token))
  }

  return (
    <main className="sheet connect">
      <p className="kicker">接到这台手机</p>
      <h1>填一次连接码</h1>
      <label>
        连接码
        <input value={code} autoComplete="off" onChange={event => setCode(event.target.value)} />
      </label>
      <button type="button" className="primary" onClick={() => void go()} disabled={busy || !code.trim()}>
        继续
      </button>
      {note ? <p className="note">{note}</p> : null}
    </main>
  )
}
