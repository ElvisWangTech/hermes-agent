import type { Character } from './characters'
import { Sprite } from './Sprite'

interface PortraitProps {
  character: Character
}

export function Portrait({ character }: PortraitProps) {
  return (
    <div className="stage">
      <Sprite character={character} className="portrait" />
    </div>
  )
}
