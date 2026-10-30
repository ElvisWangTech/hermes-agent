export interface TodoLine {
  name: string
  when: string
  next: string
  paused: boolean
  expired?: boolean
  created_at?: string
}

const openList = /^(?:请|帮我|我想|我要)?(?:打开|看看|看一下|查看|给我看|播报)?(?:一下)?(?:我的|最近的|最近)?待办(?:列表|事项)?(?:有哪些|有什么|是什么|呢|吗)?[。！!？?]?$/
const askList = /^(?:我有|有)(?:哪些|什么)待办(?:呢|吗)?[。！!？?]?$/

/** A request to look at the list, not to create or change one. */
export function wantsTodoList(text: string): boolean {
  const clean = text.replace(/\s+/g, '')
  if (!clean || /提醒|闹钟|改到|取消|停掉|暂停|继续|记下|定一个|定个/.test(clean)) return false
  return openList.test(clean) || askList.test(clean)
}

function byNewest(left: TodoLine, right: TodoLine): number {
  return (right.created_at || '').localeCompare(left.created_at || '')
}

/** Current todos, newest first. Expired ones stay out of this list. */
export function currentTodos<T extends TodoLine>(items: T[]): T[] {
  return items.filter(item => !item.expired).sort(byNewest)
}

export function expiredTodos<T extends TodoLine>(items: T[]): T[] {
  return items.filter(item => item.expired).sort(byNewest)
}

const friendCues = ['嘿，有件事想跟你说。', '对了，我帮你记着一件事。', '先缓一下，我提醒你。']
const friendLines = [
  (name: string) => `记得${name}哦。忙的时候也要照顾好自己。`,
  (name: string) => `${name}的时间到了。不急，我陪你记着。`,
  (name: string) => `该${name}啦。做完也好好歇一歇。`
]
const friendTails = [
  (name: string) => `${name}。我帮你记着了，不急。`,
  (name: string) => `${name}。慢慢来，我在这儿。`,
  (name: string) => `${name}。做完也好好歇一歇。`
]

function friendPick(name: string): number {
  return [...name].reduce((sum, char) => sum + char.charCodeAt(0), 0)
}

/** A short cue, then the task said the way a friend would. The cue does not contain the task. */
export function remindAsFriend(text: string): { cue: string; line: string } {
  const name = text.trim()
  const pick = friendPick(name || '待办')
  const bare = name.length > 0 && name.length <= 6 && !/[提醒。！？，、]/.test(name)
  const line = (bare ? friendLines : friendTails)[pick % 3](name || '这件事')
  return { cue: friendCues[pick % friendCues.length], line }
}

/** The newest current todos, for her to say aloud. Expired ones are not spoken. */
export function nearestTodoSpeech(items: TodoLine[]): string {
  const current = currentTodos(items)
  if (!current.length) return items.length ? '待办都过期了。' : '还没有待办。'
  const active = current.filter(item => !item.paused)
  const upcoming = (active.length ? active : current).slice(0, 3)
  const bits = upcoming.map(item => {
    const when = item.next || item.when
    return item.paused ? `${when} ${item.name}，暂停中` : `${when} ${item.name}`
  })
  const more = current.length > upcoming.length ? `。还有 ${current.length - upcoming.length} 条` : ''
  return `你有 ${current.length} 条。${bits.join('。')}${more}。`
}
