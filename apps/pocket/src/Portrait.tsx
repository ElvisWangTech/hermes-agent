import type { Ref } from 'react'
import type { Character } from './characters'
import { Live2DStage, type FigureHandle } from './live2d/Stage'
import type { FigureInput, LookPoint } from './live2d/pose'
import { Sprite } from './Sprite'

interface PortraitProps {
  character: Character
  figure?: Ref<FigureHandle>
  input?: Omit<FigureInput, 'reducedMotion'>
  look?: LookPoint
}

export function Portrait({ character, figure, input, look }: PortraitProps) {
  return (
    <div className="stage">
      {character.model && input && look ? (
        <Live2DStage ref={figure} modelUrl={character.model} fallback={character.image} input={input} look={look} />
      ) : (
        <Sprite character={character} className="portrait" />
      )}
    </div>
  )
}
