import { useEffect, useState } from 'react'
import { speak } from '../api'
import { findCharacter } from '../characters'
import { Sprite } from '../Sprite'
import { cleanName } from '../flow'

interface NameProps {
  token: string
  characterId: string
  onNamed: (name: string) => Promise<void>
}

export function Name({ token, characterId, onNamed }: NameProps) {
  const character = findCharacter(characterId)
  const [draft, setDraft] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const name = cleanName(draft)
  const line = confirming && name ? `${name}，就这么叫我吗？` : '你想怎么叫我？'

  useEffect(() => {
    let audio: HTMLAudioElement | null = null
    let dropped = false

    void speak(token, line)
      .then(url => {
        if (dropped) return
        audio = new Audio(url)
        return audio.play()
      })
      .catch(() => undefined)

    return () => {
      dropped = true
      audio?.pause()
    }
  }, [line, token])

  const accept = () => {
    if (!name || busy) return
    setBusy(true)
    setError('')
    void onNamed(name).catch((exc: unknown) => {
      setError(exc instanceof Error ? exc.message : '名字没有记下')
      setBusy(false)
    })
  }

  return (
    <main className="sheet name">
      <div className="frame">{character ? <Sprite character={character} className="portrait ask" /> : null}</div>
      <h1>{confirming ? `${name}，就这么叫我吗？` : '你想怎么叫我？'}</h1>
      {confirming ? null : (
        <input
          value={draft}
          placeholder="小满"
          maxLength={24}
          onChange={event => setDraft(event.target.value)}
        />
      )}
      <div className="row">
        <button
          type="button"
          onClick={() => {
            if (confirming) setConfirming(false)
            else setDraft('')
            setError('')
          }}
        >
          再想想
        </button>
        <button
          type="button"
          className="primary"
          disabled={!name || busy}
          onClick={() => (confirming ? accept() : setConfirming(true))}
        >
          就这个
        </button>
      </div>
      {error ? <p className="note">{error}</p> : null}
    </main>
  )
}
