import { useState } from 'react'
import { ageFace, bands, charactersIn, type BandId, type Character } from '../characters'
import { Sprite } from '../Sprite'

interface PickProps {
  onChoose: (id: string) => void
}

export function Pick({ onChoose }: PickProps) {
  const [band, setBand] = useState<BandId | null>(null)
  const [index, setIndex] = useState(1)

  if (!band) {
    return (
      <main className="sheet age">
        <h1>选一个年龄段</h1>
        <div className="age-row">
          {bands.map(item => {
            const face = ageFace(item.id)

            return (
              <button key={item.id} type="button" className="age-card" onClick={() => setBand(item.id)}>
                <span className="frame">
                  <Sprite character={face} />
                </span>
                <span>{item.label}</span>
              </button>
            )
          })}
        </div>
      </main>
    )
  }

  const roster = charactersIn(band)
  const current = roster[index] ?? roster[0]

  return (
    <main className="sheet pick">
      <button type="button" className="back" onClick={() => setBand(null)}>
        返回
      </button>
      <h1>选一个陪你的人</h1>
      <CharacterSwipe roster={roster} index={index} onIndex={setIndex} />
      <button type="button" className="primary" onClick={() => onChoose(current.id)}>
        就是她
      </button>
    </main>
  )
}

function facePlace(itemIndex: number, index: number, length: number): 'left' | 'center' | 'right' {
  if (itemIndex === index) return 'center'
  const forward = (itemIndex - index + length) % length
  const backward = (index - itemIndex + length) % length

  return backward <= forward ? 'left' : 'right'
}

function CharacterSwipe({
  roster,
  index,
  onIndex
}: {
  roster: Character[]
  index: number
  onIndex: (index: number) => void
}) {
  const [startX, setStartX] = useState<number | null>(null)

  return (
    <div
      className="swipe"
      onPointerDown={event => setStartX(event.clientX)}
      onPointerUp={event => {
        if (startX == null) return
        const delta = event.clientX - startX

        setStartX(null)
        if (delta > 40) onIndex((index - 1 + roster.length) % roster.length)
        if (delta < -40) onIndex((index + 1) % roster.length)
      }}
    >
      {roster.map((character, itemIndex) => {
        const place = facePlace(itemIndex, index, roster.length)

        return (
          <button
            key={character.id}
            type="button"
            className={`face ${place}${itemIndex === index ? ' on' : ''}`}
            onClick={() => onIndex(itemIndex)}
          >
            <Sprite character={character} className="tall" />
          </button>
        )
      })}
    </div>
  )
}
