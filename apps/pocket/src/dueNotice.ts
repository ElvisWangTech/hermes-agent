export type NoticePermission = 'granted' | 'denied' | 'default' | 'missing'

/** A hidden page must not speak: that would mark the line heard without anyone listening. */
export function dueOutlet(hidden: boolean, permission: NoticePermission): 'speak' | 'notify' | 'hold' {
  if (!hidden) return 'speak'
  return permission === 'granted' ? 'notify' : 'hold'
}

export function readNoticePermission(): NoticePermission {
  if (typeof Notification === 'undefined') return 'missing'
  if (Notification.permission === 'granted' || Notification.permission === 'denied') return Notification.permission
  return 'default'
}

export function requestNoticePermission(): Promise<NoticePermission> {
  if (typeof Notification === 'undefined' || !Notification.requestPermission) return Promise.resolve('missing')
  if (Notification.permission !== 'default') return Promise.resolve(readNoticePermission())
  return Notification.requestPermission()
    .then(() => readNoticePermission())
    .catch(() => 'denied' as const)
}

const open = new Map<string, Notification>()

export function showDueNotice(id: string, title: string, body: string, onOpen: (id: string) => void): boolean {
  if (readNoticePermission() !== 'granted') return false
  const existing = open.get(id)
  if (existing) return true
  const note = new Notification(title, { body, tag: id, lang: 'zh-CN' })
  open.set(id, note)
  note.onclick = () => {
    note.close()
    open.delete(id)
    window.focus()
    onOpen(id)
  }
  note.onclose = () => {
    if (open.get(id) === note) open.delete(id)
  }
  return true
}

export function closeDueNotice(id: string): void {
  const note = open.get(id)
  if (!note) return
  open.delete(id)
  note.close()
}
