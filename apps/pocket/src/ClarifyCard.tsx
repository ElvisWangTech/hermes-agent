import { useEffect, useRef, useState, type MutableRefObject } from 'react'
import type { ServerRequest } from '@hermes/shared'
import { Markdown } from './Markdown'

interface Question {
  qid: string
  question: string
  choices?: string[] | null
  multi_select?: boolean
}

export interface ClarifyParams {
  question?: string | null
  choices?: string[] | null
  multi_select?: boolean
  questions?: Question[] | null
  answers?: Record<string, string> | null
}

const DISMISS = new Set(['不用', '不用了', '算了', '取消'])
const SUBMIT = new Set(['告诉她', '确认', '好', '可以'])
const ORDINALS = ['第一', '第二', '第三', '第四', '第五', '第六', '第七', '第八']

export function clarifyQuestions(params: ClarifyParams): Question[] {
  return params.questions?.length
    ? params.questions
    : [{ qid: 'single', question: params.question || '请补充说明', choices: params.choices, multi_select: params.multi_select }]
}

export function clarifyResult(params: ClarifyParams, answers: Record<string, string>) {
  return params.questions?.length ? { answers } : { answer: answers.single || '' }
}

export function choiceLabel(choice: string): { text: string; suggested: boolean } {
  const suggested = /\(recommended\)|（推荐）|（建议）/i.test(choice)
  const text = choice.replace(/\s*[（(]\s*(?:recommended|推荐|建议)\s*[）)]\s*/gi, '').trim()

  return { text: text || choice, suggested }
}

function spoken(text: string): string {
  return text.trim().replace(/[。！？!?，,\s]+$/g, '')
}

function ordinalIndex(text: string, count: number): number | null {
  const numbered = text.match(/^第?\s*(\d+)\s*个?$/)

  if (numbered) {
    const index = Number(numbered[1]) - 1

    return index >= 0 && index < count ? index : null
  }
  const index = ORDINALS.findIndex(label => text === label || text === `${label}个` || text === `${label}条`)

  return index >= 0 && index < count ? index : null
}

function choiceScore(choice: string, utterance: string): number {
  const bare = choiceLabel(choice).text

  if (!bare || !utterance) return 0
  if (bare === utterance) return 100 + bare.length
  if (utterance.length >= 2 && bare.includes(utterance)) return 50 + utterance.length
  if (bare.length >= 2 && utterance.includes(bare)) return 40 + bare.length

  return 0
}

function pickChoice(choices: string[], utterance: string): string | null {
  const index = ordinalIndex(utterance, choices.length)

  if (index !== null) return choices[index]
  if (/^(推荐|建议|就这个|就按建议|按建议)$/.test(utterance)) {
    return choices.find(choice => choiceLabel(choice).suggested) || choices[0]
  }
  const ranked = choices
    .map(choice => ({ choice, score: choiceScore(choice, utterance) }))
    .filter(item => item.score > 0)
    .sort((left, right) => right.score - left.score)

  if (!ranked.length || (ranked.length > 1 && ranked[0].score === ranked[1].score)) return null

  return ranked[0].choice
}

export type ClarifyVoice =
  | { type: 'dismiss' }
  | { type: 'submit' }
  | { type: 'answer'; qid: string; value: string; submit: boolean }

export function matchClarifyUtterance(
  questions: Question[],
  answers: Record<string, string>,
  text: string
): ClarifyVoice | null {
  const utterance = spoken(text)

  if (!utterance) return null
  if (DISMISS.has(utterance)) return { type: 'dismiss' }
  if (SUBMIT.has(utterance)) {
    return questions.every(question => (answers[question.qid] || '').trim()) ? { type: 'submit' } : null
  }
  const hits: { qid: string; value: string; exclusive: boolean }[] = []

  for (const question of questions) {
    if (!question.choices?.length) continue
    const choice = pickChoice(question.choices, utterance)

    if (choice) hits.push({ qid: question.qid, value: choice, exclusive: !question.multi_select })
  }
  if (hits.length > 1) return null
  if (hits.length === 1) {
    const hit = hits[0]
    const previous = (answers[hit.qid] || '').split(',').map(part => part.trim()).filter(Boolean)
    const value = hit.exclusive
      ? hit.value
      : (previous.includes(hit.value) ? previous : [...previous, hit.value]).join(', ')

    return { type: 'answer', qid: hit.qid, value, submit: questions.length === 1 && hit.exclusive }
  }
  const overlaps = questions.reduce((count, question) => (
    count + (question.choices || []).filter(choice => choiceScore(choice, utterance) > 0).length
  ), 0)

  if (overlaps > 1) return null
  const target = questions.find(question => !(answers[question.qid] || '').trim()) || (questions.length === 1 ? questions[0] : null)

  if (!target || utterance.length < 2) return null

  return { type: 'answer', qid: target.qid, value: text.trim(), submit: questions.length === 1 }
}

export function ClarifyCard({
  request,
  onClose,
  voiceRef
}: {
  request: ServerRequest
  onClose: () => void
  voiceRef?: MutableRefObject<(text: string) => boolean>
}) {
  const params = request.params as ClarifyParams
  const questions = clarifyQuestions(params)
  const [answers, setAnswers] = useState<Record<string, string>>(params.answers || {})
  const [selections, setSelections] = useState<Record<string, string[]>>({})
  const answersRef = useRef(answers)

  answersRef.current = answers

  const respond = (next: Record<string, string>) => {
    request.respond(clarifyResult(params, Object.fromEntries(
      questions.map(question => [question.qid, (next[question.qid] || '').trim()])
    )))
    onClose()
  }

  const dismiss = () => {
    request.respond(params.questions?.length ? {} : { answer: '' })
    onClose()
  }

  const apply = (action: ClarifyVoice) => {
    if (action.type === 'dismiss') {
      dismiss()
      return
    }
    if (action.type === 'submit') {
      respond(answersRef.current)
      return
    }
    const next = { ...answersRef.current, [action.qid]: action.value }
    const question = questions.find(item => item.qid === action.qid)
    const chosen = question?.choices?.includes(action.value) ? [action.value] : action.value.split(',').map(part => part.trim())

    answersRef.current = next
    setAnswers(next)
    setSelections(current => ({ ...current, [action.qid]: question?.choices ? chosen.filter(part => question.choices?.includes(part)) : [] }))
    if (action.submit) respond(next)
  }

  if (voiceRef) {
    voiceRef.current = (text: string) => {
      const action = matchClarifyUtterance(questions, answersRef.current, text)

      if (!action) return false
      apply(action)
      return true
    }
  }

  useEffect(() => {
    return () => {
      if (voiceRef) voiceRef.current = () => false
    }
  }, [voiceRef])

  return (
    <section className="overlay clarify-card" role="dialog" aria-modal="true" aria-label="确认问题"
      onPointerDown={event => event.stopPropagation()} onPointerUp={event => event.stopPropagation()}>
      <div className="clarify-sheet">
        <h1>她想确认一件事</h1>
        <div className="clarify-questions">
          {questions.map(question => (
            <div className="clarify-block" key={question.qid}>
              <div className="clarify-ask"><Markdown text={question.question} /></div>
              {question.choices?.length ? (
                <div className="clarify-options">
                  {question.choices.map(choice => {
                    const label = choiceLabel(choice)
                    const selected = (selections[question.qid] || []).includes(choice)

                    return (
                      <button
                        key={choice}
                        type="button"
                        className={selected ? 'clarify-option on' : 'clarify-option'}
                        aria-pressed={selected}
                        onClick={() => {
                          const chosen = question.multi_select
                            ? selected
                              ? (selections[question.qid] || []).filter(value => value !== choice)
                              : [...(selections[question.qid] || []), choice]
                            : [choice]
                          const next = { ...answers, [question.qid]: chosen.join(', ') }

                          setSelections(current => ({ ...current, [question.qid]: chosen }))
                          setAnswers(next)
                        }}
                      >
                        <span>{label.text}</span>
                        {label.suggested ? <em>建议</em> : null}
                      </button>
                    )
                  })}
                </div>
              ) : null}
              <textarea
                aria-label={question.question}
                rows={2}
                placeholder={question.choices?.length ? '或者用自己的话说' : '直接说，或写在这里'}
                value={question.choices?.some(choice => (selections[question.qid] || []).includes(choice)) ? '' : (answers[question.qid] || '')}
                onChange={event => {
                  setSelections(current => ({ ...current, [question.qid]: [] }))
                  setAnswers(current => ({ ...current, [question.qid]: event.target.value }))
                }}
              />
            </div>
          ))}
        </div>
        <p className="hint">点一条，或者说出来</p>
        <div className="row">
          <button type="button" onClick={dismiss}>不用</button>
          <button type="button" className="primary" onClick={() => respond(answers)}>告诉她</button>
        </div>
      </div>
    </section>
  )
}
