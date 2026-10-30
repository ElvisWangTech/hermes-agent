import { expect, it } from 'vitest'
import { dueOutlet } from './dueNotice'

it('speaks while the page is open and only notifies from the background', () => {
  expect(dueOutlet(false, 'granted')).toBe('speak')
  expect(dueOutlet(false, 'denied')).toBe('speak')
  expect(dueOutlet(true, 'granted')).toBe('notify')
  expect(dueOutlet(true, 'default')).toBe('hold')
  expect(dueOutlet(true, 'denied')).toBe('hold')
  expect(dueOutlet(true, 'missing')).toBe('hold')
})
