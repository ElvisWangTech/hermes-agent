import { describe, expect, it } from 'vitest'
import { ageFace, characters } from './characters'
import { cleanName, memoryLine, stepFor, takeSentences } from './flow'

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
