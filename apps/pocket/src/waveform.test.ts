import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { Waveform } from './Waveform'

it('reflects real amplitude and collapses all segments at silence', () => {
  const silent = renderToStaticMarkup(createElement(Waveform, { label: '你', level: 0 }))
  const loud = renderToStaticMarkup(createElement(Waveform, { label: '你', level: 1 }))
  expect(silent).toContain('aria-valuenow="0"')
  expect(silent).not.toContain('height:40px')
  expect(loud).toContain('aria-valuenow="100"')
  expect(loud).toContain('height:40px')
  expect(loud).not.toEqual(silent)
})

it('clamps invalid or out-of-range amplitudes', () => {
  for (const [level, expected] of [[NaN, 0], [-1, 0], [2, 100]]) {
    expect(renderToStaticMarkup(createElement(Waveform, { label: '角色', level }))).toContain(`aria-valuenow="${expected}"`)
  }
})