import { expect, it } from 'vitest'
import { speechText } from './speech'

it('reads Markdown prose and table cells without formatting syntax or URLs', () => {
  const text = speechText('# 天气\n\n**今天**看[预报](https://example.com)。\n\n- *多云*\n\n|日期|气温|\n|---|---|\n|今天|23度|')
  expect(text).toContain('今天看预报。')
  expect(text).toContain('多云')
  expect(text).toContain('今天，23度')
  expect(text).not.toMatch(/[*#|]|https:\/\//)
})

it('omits code blocks and HTML while preserving inline code and literal punctuation', () => {
  const text = speechText('请运行 `hello`，2 * 3 = 6。\n\n```js\nconsole.log("不要朗读")\n```\n\n<script>alert(1)</script>\n\n结束。')
  expect(text).toContain('请运行 hello，2 * 3 = 6。')
  expect(text).toContain('结束。')
  expect(text).not.toMatch(/console|不要朗读|script|alert/)
})