import { ChevronLeft } from 'lucide-react'

export function BackButton({
  children = '返回',
  onClick
}: {
  children?: string
  onClick: () => void
}) {
  return (
    <button type="button" className="back" onClick={onClick}>
      <ChevronLeft size={22} aria-hidden="true" />
      {children}
    </button>
  )
}
