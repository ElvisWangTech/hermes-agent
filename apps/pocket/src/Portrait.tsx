import type { Character, Mood } from './characters'
import { Sprite } from './Sprite'

interface PortraitProps {
  character: Character
  mood: Mood
  muted: boolean
  hop: boolean
}

export function Portrait({ character, mood, muted, hop }: PortraitProps) {
  return (
    <div className={`stage mood-${mood}${hop ? ' hop' : ''}${muted ? ' muted' : ''}`}>
      <Sprite character={character} className="portrait" />
      {muted ? (
        <>
          <span className="hand left" />
          <span className="hand right" />
        </>
      ) : null}
    </div>
  )
}
