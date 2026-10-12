import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
        table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
        a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
      }}>{text}</ReactMarkdown>
    </div>
  )
}