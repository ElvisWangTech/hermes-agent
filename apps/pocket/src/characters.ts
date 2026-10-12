export type BandId = 'young' | 'teen' | 'adult'
export type Mood = 'idle' | 'listen' | 'think' | 'speak' | 'wait'

export interface Character {
  id: string
  band: BandId
  image: string
}

export interface Band {
  id: BandId
  label: string
}

const base = `${import.meta.env.BASE_URL}characters/`

function portrait(id: string, band: BandId, file: string): Character {
  return { id, band, image: `${base}${file}` }
}

export const bands: Band[] = [
  { id: 'young', label: '低龄' },
  { id: 'teen', label: '少年' },
  { id: 'adult', label: '青年' }
]

export const characters: Character[] = [
  portrait('young-0', 'young', 'young-0.png'),
  portrait('young-1', 'young', 'young-1.png'),
  portrait('young-2', 'young', 'young-2.png'),
  portrait('teen-0', 'teen', 'teen-0.png'),
  portrait('teen-1', 'teen', 'teen-1.png'),
  portrait('teen-2', 'teen', 'teen-2.png'),
  portrait('adult-0', 'adult', 'adult-0.png'),
  portrait('adult-1', 'adult', 'adult-1.png'),
  portrait('adult-2', 'adult', 'adult-2.png')
]

export function charactersIn(band: BandId): Character[] {
  return characters.filter(character => character.band === band)
}

export function ageFace(band: BandId): Character {
  if (band === 'young') return charactersIn('young')[0]
  if (band === 'adult') return charactersIn('adult')[1]

  return charactersIn('teen')[1]
}

export function findCharacter(id: string): Character | undefined {
  return characters.find(character => character.id === id)
}
