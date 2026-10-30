import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { ServerRequest } from '@hermes/shared'
import { expect, it } from 'vitest'
import { ClarifyCard, choiceLabel, clarifyQuestions, clarifyResult, matchClarifyUtterance } from './ClarifyCard'

it('renders batch question content and choices and returns answers by qid', () => {
  const params = { questions: [{ qid: 'book', question: '你指的是**哪本书**？', choices: ['图集', '小说'] }, { qid: 'task', question: '需要做什么？' }] }
  const request = { id: 'test', params } as unknown as ServerRequest
  const html = renderToStaticMarkup(createElement(ClarifyCard, { request, onClose: () => {} }))
  expect(html).toContain('<strong>哪本书</strong>')
  expect(html).toContain('图集')
  expect(html).toContain('需要做什么？')
  expect(clarifyResult(params, { book: '图集', task: '整理' })).toEqual({ answers: { book: '图集', task: '整理' } })
})

it('speaks a choice, an ordinal, or a dismissal without a second tap', () => {
  const questions = [{ qid: 'single', question: '指什么？', choices: ['照片里的黑色纹理 (Recommended)', '衣服上的纹路', '插画风格'] }]

  expect(choiceLabel(questions[0].choices[0])).toEqual({ text: '照片里的黑色纹理', suggested: true })
  expect(matchClarifyUtterance(questions, {}, '衣服')).toEqual({ type: 'answer', qid: 'single', value: '衣服上的纹路', submit: true })
  expect(matchClarifyUtterance(questions, {}, '第二个')).toEqual({ type: 'answer', qid: 'single', value: '衣服上的纹路', submit: true })
  expect(matchClarifyUtterance(questions, {}, '推荐')).toEqual({ type: 'answer', qid: 'single', value: questions[0].choices[0], submit: true })
  expect(matchClarifyUtterance(
    [{ qid: 'single', question: '指什么？', choices: ['照片里的黑色纹理', '衣服上的黑色纹路'] }],
    {},
    '黑色'
  )).toBeNull()
  expect(matchClarifyUtterance(questions, {}, '不用')).toEqual({ type: 'dismiss' })
  expect(matchClarifyUtterance(questions, { single: '衣服上的纹路' }, '告诉她')).toEqual({ type: 'submit' })
  expect(matchClarifyUtterance(
    [{ qid: 'book', question: '哪本？', choices: ['图集'] }, { qid: 'task', question: '做什么？' }],
    {},
    '图集'
  )).toEqual({ type: 'answer', qid: 'book', value: '图集', submit: false })
})

it('preserves the single-question response contract', () => {
  const params = { question: '需要继续吗？' }
  expect(clarifyQuestions(params)[0].question).toBe(params.question)
  expect(clarifyResult(params, { single: '继续' })).toEqual({ answer: '继续' })
})