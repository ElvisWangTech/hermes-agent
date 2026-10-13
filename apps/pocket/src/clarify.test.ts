import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ServerRequest } from '@hermes/shared'
import { expect, it } from 'vitest'
import { ClarifyCard, clarifyQuestions, clarifyResult } from './ClarifyCard'

it('renders batch question content and choices and returns answers by qid', () => {
  const params = { questions: [{ qid: 'book', question: '你指的是**哪本书**？', choices: ['图集', '小说'] }, { qid: 'task', question: '需要做什么？' }] }
  const request = { id: 'test', params } as unknown as ServerRequest
  const html = renderToStaticMarkup(createElement(ClarifyCard, { request, onClose: () => {} }))
  expect(html).toContain('<strong>哪本书</strong>')
  expect(html).toContain('图集')
  expect(html).toContain('需要做什么？')
  expect(clarifyResult(params, { book: '图集', task: '整理' })).toEqual({ answers: { book: '图集', task: '整理' } })
})

it('preserves the single-question response contract', () => {
  const params = { question: '需要继续吗？' }
  expect(clarifyQuestions(params)[0].question).toBe(params.question)
  expect(clarifyResult(params, { single: '继续' })).toEqual({ answer: '继续' })
})