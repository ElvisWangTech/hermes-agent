import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { Markdown } from './Markdown'

describe('Markdown', () => {
  it('renders emphasis, lists, code and GFM tables', () => {
    const html = renderToStaticMarkup(createElement(Markdown, { text: '**天气**\n\n- 多云\n\n```js\nconst temperature = 23\n```\n\n|日期|天气|\n|---|---|\n|今天|多云|' }))
    expect(html).toContain('<strong>天气</strong>')
    expect(html).toContain('<li>多云</li>')
    expect(html).toContain('<pre>')
    expect(html).toContain('<table>')
    expect(html).toContain('<td>多云</td>')
  })

  it('does not render raw HTML or executable links', () => {
    const html = renderToStaticMarkup(createElement(Markdown, { text: '<script>alert(1)</script>\n\n[链接](javascript:alert)' }))
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('href="javascript:')
  })
})