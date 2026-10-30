import { expect, it, vi } from 'vitest'
import { loadProfile, rememberSession, savedRoles, saveProfile, setSpeakerEnabled, setVoiceEnabled, speakerEnabled, storedSession, switchRole, voiceEnabled } from './storage'

it('keeps the speaker and the microphone independent', () => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key)
  })
  try {
    expect(speakerEnabled()).toBe(true)
    expect(voiceEnabled()).toBe(true)
    setVoiceEnabled(false)
    expect(speakerEnabled()).toBe(true)
    setSpeakerEnabled(false)
    setVoiceEnabled(true)
    expect(speakerEnabled()).toBe(false)
    expect(voiceEnabled()).toBe(true)
  } finally {
    vi.unstubAllGlobals()
  }
})

it('keeps each saved role conversation through A to B to A switches', () => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key)
  })
  try {
    const first = { localId: 'a', token: 'first', characterId: 'adult-0', name: 'A' }
    const second = { localId: 'b', token: 'second', characterId: 'adult-1', name: 'B' }
    saveProfile(first)
    rememberSession('conversation-a')
    switchRole(second)
    expect(storedSession()).toBe('')
    rememberSession('conversation-b')
    switchRole(first)
    expect(storedSession()).toBe('conversation-a')
    expect(loadProfile()).toEqual(first)
    expect(savedRoles()).toHaveLength(2)
    switchRole(second)
    expect(storedSession()).toBe('conversation-b')
  } finally {
    vi.unstubAllGlobals()
  }
})