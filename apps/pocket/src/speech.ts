export const voiceInstruction = '\n\n[POCKET_VOICE_REQUEST]\n请先正常完整回答，再在回复末尾使用 <pocket-voice-summary> 和 </pocket-voice-summary> 包住口语摘要。摘要使用回复原语言，2到4个短句，中文不超过180字；保留主要结论、关键数字、提醒和下一步。表格只概括结论和比较，不逐行朗读；不要朗读代码、网址或Markdown标记，不添加原文没有的事实。\n[/POCKET_VOICE_REQUEST]'

export function voiceReply(text: string): { text: string; summary: string } {
  const start = text.indexOf('<pocket-voice-summary>')
  if (start < 0) return { text: text.trim(), summary: '' }
  const end = text.indexOf('</pocket-voice-summary>', start)
  return {
    text: text.slice(0, start).trim(),
    summary: end < 0 ? '' : text.slice(start + '<pocket-voice-summary>'.length, end).trim()
  }
}

export function visibleUserText(text: string): string {
  return text.endsWith(voiceInstruction) ? text.slice(0, -voiceInstruction.length) : text
}

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