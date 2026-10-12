import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'

interface TextNode {
  type: string
  value?: string
  alt?: string | null
  children?: TextNode[]
}

const parser = unified().use(remarkParse).use(remarkGfm)

export function speechText(markdown: string): string {
  const read = (node: TextNode): string => {
    if (['code', 'html', 'definition', 'thematicBreak'].includes(node.type)) return ''
    if (node.type === 'text' || node.type === 'inlineCode') return node.value ?? ''
    if (node.type === 'image' || node.type === 'imageReference') return node.alt ?? ''
    if (node.type === 'break') return '\n'
    const separator = node.type === 'tableRow' ? '，' : ['root', 'list', 'blockquote', 'table'].includes(node.type) ? '\n' : ''
    const text = (node.children ?? []).map(read).filter(Boolean).join(separator)
    return ['paragraph', 'heading', 'listItem', 'tableRow'].includes(node.type) ? `${text}\n` : text
  }
  return read(parser.parse(markdown)).replace(/[ \t]+/g, ' ').replace(/\n\s*\n/g, '\n').trim()
}