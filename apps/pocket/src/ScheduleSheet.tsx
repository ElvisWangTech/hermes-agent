import type { ScheduleBoard, ScheduleItem } from './api'
import { BackButton } from './BackButton'
import { currentTodos, expiredTodos } from './todos'

export function ScheduleSheet({
  board,
  onClose,
  onUtterance
}: {
  board: ScheduleBoard | null
  onClose: () => void
  onUtterance: (text: string) => void
}) {
  const items = board?.items ?? []
  const current = currentTodos(items)
  const expired = expiredTodos(items)
  const missed = board?.unheard ?? []
  const said = board?.said ?? []

  return (
    <section className="overlay" onPointerDown={event => event.stopPropagation()}>
      <BackButton onClick={onClose} />
      <h1>待办</h1>
      <div className="log">
        {current.length === 0 && expired.length === 0 ? <p className="quiet">还没有待办</p> : null}
        {current.map(item => <TodoRow key={item.id} item={item} onUtterance={onUtterance} />)}
        {expired.length > 0 ? (
          <details className="folded">
            <summary>已过期 {expired.length}</summary>
            {expired.map(item => <TodoRow key={item.id} item={item} onUtterance={onUtterance} />)}
          </details>
        ) : null}
        {missed.length > 0 ? <h2>你不在时</h2> : null}
        {missed.map(item => <p key={item.id}>{item.text}</p>)}
        {said.length > 0 ? <h2>说过</h2> : null}
        {said.map((item, index) => <p key={`${item.text}-${index}`}>{item.ok ? item.text : `没做成：${item.text}`}</p>)}
      </div>
    </section>
  )
}

function TodoRow({ item, onUtterance }: { item: ScheduleItem; onUtterance: (text: string) => void }) {
  return (
    <article className="session">
      <strong>{item.name}</strong>
      <span>{item.when}{item.paused ? '，暂停中' : item.next ? `，下次 ${item.next}` : ''}</span>
      {item.last_text ? <span>{item.last_ok === false ? '上次没做成' : '上次'}：{item.last_text}</span> : null}
      <div className="row">
        {item.paused
          ? <button type="button" onClick={() => onUtterance(`继续${item.name}提醒`)}>继续</button>
          : <button type="button" onClick={() => onUtterance(`停掉${item.name}`)}>停掉</button>}
        <button type="button" onClick={() => onUtterance(`取消${item.name}`)}>取消这条</button>
      </div>
      <form onSubmit={event => {
        event.preventDefault()
        const field = event.currentTarget.elements.namedItem('when') as HTMLInputElement
        const when = field.value.trim()

        if (!when) return
        field.value = ''
        onUtterance(`把${item.name}改到${when}`)
      }}>
        <input name="when" placeholder="改到什么时候" />
      </form>
    </article>
  )
}
