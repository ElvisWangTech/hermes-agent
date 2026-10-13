import { useState } from 'react'
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

export function clarifyQuestions(params: ClarifyParams): Question[] {
  return params.questions?.length ? params.questions : [{ qid: 'single', question: params.question || '请补充说明', choices: params.choices, multi_select: params.multi_select }]
}

export function clarifyResult(params: ClarifyParams, answers: Record<string, string>) {
  return params.questions?.length ? { answers } : { answer: answers.single || '' }
}

export function ClarifyCard({ request, onClose }: { request: ServerRequest; onClose: () => void }) {
  const params = request.params as ClarifyParams
  const [answers, setAnswers] = useState<Record<string, string>>(params.answers || {})
  const [selections, setSelections] = useState<Record<string, string[]>>({})
  const questions = clarifyQuestions(params)
  return (
    <section className="overlay clarify-card" role="dialog" aria-modal="true" aria-label="确认问题"
      onPointerDown={event => event.stopPropagation()} onPointerUp={event => event.stopPropagation()}>
      <h1>她想确认一件事</h1>
      <div className="clarify-questions">
        {questions.map(question => (
          <fieldset key={question.qid}>
            <legend><Markdown text={question.question} /></legend>
            {question.choices?.map(choice => (
              <label className="clarify-choice" key={choice}>
                <input type={question.multi_select ? 'checkbox' : 'radio'} name={question.qid}
                  checked={(selections[question.qid] || []).includes(choice)}
                  onChange={event => {
                    const chosen = question.multi_select
                      ? event.target.checked ? [...(selections[question.qid] || []), choice] : (selections[question.qid] || []).filter(value => value !== choice)
                      : [choice]
                    setSelections(current => ({ ...current, [question.qid]: chosen }))
                    setAnswers(current => ({ ...current, [question.qid]: chosen.join(', ') }))
                  }} />
                <span>{choice}</span>
              </label>
            ))}
            <textarea aria-label={question.question} rows={2} value={answers[question.qid] || ''}
              onChange={event => {
                setSelections(current => ({ ...current, [question.qid]: [] }))
                setAnswers(current => ({ ...current, [question.qid]: event.target.value }))
              }} />
          </fieldset>
        ))}
      </div>
      <div className="row">
        <button type="button" onClick={() => { request.respond(params.questions?.length ? {} : { answer: '' }); onClose() }}>不用</button>
        <button type="button" className="primary" onClick={() => {
          request.respond(clarifyResult(params, Object.fromEntries(questions.map(question => [question.qid, (answers[question.qid] || '').trim()]))))
          onClose()
        }}>告诉她</button>
      </div>
    </section>
  )
}