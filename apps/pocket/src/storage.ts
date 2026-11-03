import type { PocketProfile } from './flow'

const KEY = 'pocket.profile'

const empty: PocketProfile = { token: '', characterId: '', name: '' }

export function newId(): string {
  // crypto.randomUUID only exists in secure contexts (https, localhost); the
  // cluster entry is plain http on an IP, so fall back to manual UUID v4 bytes.
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex: string[] = []
  for (let i = 0; i < bytes.length; i += 1) hex.push(bytes[i].toString(16).padStart(2, '0'))
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`
}

export function adoptToken(profile: PocketProfile): PocketProfile {
  const params = new URLSearchParams(window.location.search)
  const fromUrl = params.get('token')?.trim() ?? ''
  const candidate = fromUrl || profile.token
  const token = candidate.startsWith('pd_') ? candidate : ''

  if (fromUrl) {
    params.delete('token')
    const query = params.toString()

    window.history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`)
  }

  const next = { ...profile, token }

  if (next.token !== profile.token) saveProfile(next)

  return next
}

export function loadProfile(): PocketProfile {
  try {
    const raw = localStorage.getItem(KEY)

    if (!raw) return { ...empty }
    const parsed = JSON.parse(raw) as Partial<PocketProfile>

    return {
      localId: typeof parsed.localId === 'string' ? parsed.localId : undefined,
      token: typeof parsed.token === 'string' ? parsed.token : '',
      characterId: typeof parsed.characterId === 'string' ? parsed.characterId : '',
      name: typeof parsed.name === 'string' ? parsed.name : ''
    }
  } catch {
    return { ...empty }
  }
}

export function saveProfile(profile: PocketProfile): void {
  localStorage.setItem(KEY, JSON.stringify(profile))
  if (profile.localId) {
    const roles = savedRoles().filter(role => role.localId !== profile.localId)
    localStorage.setItem('pocket.roles', JSON.stringify([...roles, profile]))
  }
}

export function savedRoles(): PocketProfile[] {
  try {
    const roles: unknown = JSON.parse(localStorage.getItem('pocket.roles') || '[]')
    return Array.isArray(roles) ? roles.filter((role): role is PocketProfile =>
      !!role && typeof role.localId === 'string' && typeof role.token === 'string' &&
      typeof role.characterId === 'string' && typeof role.name === 'string') : []
  } catch {
    return []
  }
}

export function switchRole(next: PocketProfile): void {
  const previous = loadProfile()
  const session = storedSession()
  if (previous.localId) {
    if (session) localStorage.setItem(`pocket.session.${previous.localId}`, session)
    else localStorage.removeItem(`pocket.session.${previous.localId}`)
  }
  const restored = next.localId ? localStorage.getItem(`pocket.session.${next.localId}`) : null
  localStorage.removeItem('pocket.session')
  if (restored) rememberSession(restored)
  saveProfile(next)
}

export function voiceEnabled(): boolean {
  return localStorage.getItem('pocket.voice') !== 'off'
}

export function setVoiceEnabled(on: boolean): void {
  localStorage.setItem('pocket.voice', on ? 'on' : 'off')
}

export function speakerEnabled(): boolean {
  return localStorage.getItem('pocket.speaker') !== 'off'
}

export function setSpeakerEnabled(on: boolean): void {
  localStorage.setItem('pocket.speaker', on ? 'on' : 'off')
}

export function storedSession(): string {
  return localStorage.getItem('pocket.session') ?? ''
}

export function rememberSession(id: string): void {
  if (id) localStorage.setItem('pocket.session', id)
}


export function clearPocketStorage(): void {
  const current = loadProfile()
  if (current.localId) {
    localStorage.setItem('pocket.roles', JSON.stringify(savedRoles().filter(role => role.localId !== current.localId)))
    localStorage.removeItem(`pocket.session.${current.localId}`)
  }
  localStorage.removeItem(KEY)
  localStorage.removeItem('pocket.session')
  localStorage.removeItem('pocket.voice')
  localStorage.removeItem('pocket.speaker')
}
