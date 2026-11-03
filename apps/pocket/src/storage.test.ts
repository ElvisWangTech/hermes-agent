import { expect, it, vi } from 'vitest'
import { newId, loadProfile, rememberSession, savedRoles, saveProfile, setSpeakerEnabled, setVoiceEnabled, speakerEnabled, storedSession, switchRole, voiceEnabled } from './storage'

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
it('mints ids without crypto.randomUUID on insecure origins', () => {
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
  expect(newId()).toMatch(uuidPattern)  // secure context: the native path

  // browser served over plain http: no randomUUID, only getRandomValues
  const cryptoStub = {
    getRandomValues: (target: Uint8Array) => { target.set(new Uint8Array(target.length).fill(0x5a)); return target }
  }
  vi.stubGlobal('crypto', cryptoStub)
  try {
    expect(newId()).toMatch(uuidPattern)
  } finally {
    vi.unstubAllGlobals()
  }
})
