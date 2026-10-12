import { useState } from 'react'

interface ConnectProps {
  onToken: (token: string) => void
}

export function Connect({ onToken }: ConnectProps) {
  const [code, setCode] = useState('')
  const [note, setNote] = useState('')

  const go = () => {
    const token = code.trim()

    if (!token) return
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
      <button type="button" className="primary" onClick={go} disabled={!code.trim()}>
        继续
      </button>
      {note ? <p className="note">{note}</p> : null}
    </main>
  )
}
