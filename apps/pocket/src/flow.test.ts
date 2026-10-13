import { afterEach, describe, expect, it, vi } from 'vitest'
import { ageFace, characters } from './characters'
import { cleanName, memoryLine, stepFor, takeSentences } from './flow'
import { audioLevel } from './audio'
import { exchangeCode, listSessions } from './api'
import type { JsonRpcGatewayClient } from '@hermes/shared'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('listSessions', () => {
  it('never substitutes non-Pocket history when Pocket history is empty', async () => {
    const client = { request: async () => ({ sessions: [{ id: 'cli-session', source: 'cli' }] }) }
    expect(await listSessions(client as unknown as JsonRpcGatewayClient)).toEqual([])
  })
})

describe('exchangeCode', () => {
  it('reports a useful error when the backend returns a non-JSON response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 502 })))

    await expect(exchangeCode('test-code')).rejects.toThrow('连接服务没有返回有效响应')
  })
})

describe('audioLevel', () => {
  it('measures silence and actual sample energy without inventing motion', () => {
    expect(audioLevel(new Uint8Array())).toBe(0)
    expect(audioLevel(new Uint8Array([128, 128]))).toBe(0)
    expect(audioLevel(new Uint8Array([140, 116]))).toBeGreaterThan(0)
    expect(audioLevel(new Uint8Array([255, 0]))).toBe(1)
  })
})

describe('stepFor', () => {
  it('walks connect, then a character, then a name', () => {
    expect(stepFor({ token: '', characterId: '', name: '' })).toBe('connect')
    expect(stepFor({ token: 't', characterId: '', name: '' })).toBe('age')
    expect(stepFor({ token: 't', characterId: 'teen-1', name: '' })).toBe('name')
    expect(stepFor({ token: 't', characterId: 'teen-1', name: '小满' })).toBe('home')
  })
})

describe('memoryLine', () => {
  it('stores the name the user chose', () => {
    expect(memoryLine('  小满  ')).toBe('我的名字是小满，这是用户给我起的。')
    expect(cleanName('  a   b  ')).toBe('a b')
  })
})

describe('characters', () => {
  it('gives every person her own picture', () => {
    const images = characters.map(character => character.image)

    expect(new Set(images).size).toBe(characters.length)
    expect(ageFace('teen').id).toBe('teen-1')
    expect(ageFace('adult').id).toBe('adult-1')
    expect(ageFace('young').id).toBe('young-0')
  })
})

describe('takeSentences', () => {
  it('releases text only at a sentence end', () => {
    expect(takeSentences('今晚多云，大概二十度。要不要')).toEqual({
      ready: ['今晚多云，大概二十度。'],
      rest: '要不要'
    })
  })
})
