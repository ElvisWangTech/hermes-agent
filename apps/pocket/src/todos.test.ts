import { expect, it } from 'vitest'
import { nearestTodoSpeech, remindAsFriend, wantsTodoList } from './todos'

it('opens the list when she is asked to show todos', () => {
  expect(wantsTodoList('打开待办')).toBe(true)
  expect(wantsTodoList('我的待办')).toBe(true)
  expect(wantsTodoList('看看我的待办')).toBe(true)
  expect(wantsTodoList('最近的待办')).toBe(true)
  expect(wantsTodoList('我有哪些待办')).toBe(true)
  expect(wantsTodoList('明天早上八点提醒我带钥匙')).toBe(false)
  expect(wantsTodoList('取消喝水')).toBe(false)
})

it('reminds like a friend, with the task only in the second line', () => {
  const water = remindAsFriend('喝水')
  expect(water.cue.endsWith('。')).toBe(true)
  expect(water.cue.includes('喝水')).toBe(false)
  expect(water.line.includes('喝水')).toBe(true)
  expect(water.line.length).toBeGreaterThan('喝水'.length + 4)
  expect(remindAsFriend('喝水')).toEqual(water)

  const longer = remindAsFriend('五分钟后提醒我出门')
  expect(longer.line.startsWith('五分钟后提醒我出门')).toBe(true)
  expect(longer.line.includes('该五分钟')).toBe(false)
})

it('speaks newer current todos and skips expired ones', () => {
  expect(nearestTodoSpeech([])).toBe('还没有待办。')
  expect(nearestTodoSpeech([
    { name: '带钥匙', when: '明天 8:00', next: '明天 8:00', paused: false, created_at: '2026-10-01' },
    { name: '喝水', when: '每天 21:00', next: '今天 21:00', paused: false, created_at: '2026-10-30' },
    { name: '买菜', when: '周五', next: '周五', paused: true, created_at: '2026-10-20' },
    { name: '睡觉', when: '昨天', next: '昨天', paused: false, created_at: '2026-09-01', expired: true }
  ])).toBe('你有 3 条。今天 21:00 喝水。明天 8:00 带钥匙。还有 1 条。')
  expect(nearestTodoSpeech([
    { name: '睡觉', when: '昨天', next: '昨天', paused: false, expired: true }
  ])).toBe('待办都过期了。')
})
