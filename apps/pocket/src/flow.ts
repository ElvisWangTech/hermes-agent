export interface PocketProfile {
  localId?: string
  token: string
  characterId: string
  name: string
}

export type Step = 'connect' | 'age' | 'name' | 'home'

export function cleanName(raw: string): string {
  return raw.replace(/\s+/g, ' ').trim().slice(0, 24)
}

export function stepFor(profile: PocketProfile): Step {
  if (!profile.token) return 'connect'
  if (!profile.characterId) return 'age'
  if (!profile.name) return 'name'

  return 'home'
}

export function memoryLine(name: string): string {
  return `我的名字是${cleanName(name)}，这是用户给我起的。`
}

export function takeSentences(buffer: string): { ready: string[]; rest: string } {
  const ready: string[] = []
  let rest = buffer
  const end = /[。！？\n]/

  while (end.test(rest)) {
    const index = rest.search(end)
    const sentence = rest.slice(0, index + 1).trim()

    rest = rest.slice(index + 1)
    if (sentence) ready.push(sentence)
  }

  return { ready, rest }
}
