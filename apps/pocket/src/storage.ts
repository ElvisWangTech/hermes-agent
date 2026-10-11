import type { PocketProfile } from './flow'

const KEY = 'pocket.profile'

const empty: PocketProfile = { token: '', characterId: '', name: '' }

export function adoptToken(profile: PocketProfile): PocketProfile {
  const params = new URLSearchParams(window.location.search)
  const fromUrl = params.get('token')?.trim() ?? ''
  const injected = (window.__POCKET_SESSION_TOKEN__ ?? '').trim()
  // A loopback Hermes server creates a fresh token on every restart. Prefer the
  // current page/bootstrap token so a stale localStorage token cannot trigger
  // an avoidable reconnect prompt or WebSocket 403.
  const token = fromUrl || injected || profile.token

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
}

export function voiceEnabled(): boolean {
  return localStorage.getItem('pocket.voice') !== 'off'
}

export function setVoiceEnabled(on: boolean): void {
  localStorage.setItem('pocket.voice', on ? 'on' : 'off')
}

export function storedSession(): string {
  return localStorage.getItem('pocket.session') ?? ''
}

export function rememberSession(id: string): void {
  if (id) localStorage.setItem('pocket.session', id)
}


export function clearPocketStorage(): void {
  localStorage.removeItem(KEY)
  localStorage.removeItem('pocket.session')
  localStorage.removeItem('pocket.voice')
}
