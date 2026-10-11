import type { Character } from './characters'

interface SpriteProps {
  character: Character
  className?: string
}

export function Sprite({ character, className }: SpriteProps) {
  return (
    <span className={className ? `sprite ${className}` : 'sprite'}>
      <img alt="" draggable={false} src={character.image} />
    </span>
  )
}
