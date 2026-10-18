import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react'
import { mountFigure, type FigureEngine } from './engine'
import type { FigureInput, LookPoint } from './pose'

export interface FigureHandle {
  nod(): void
}

interface StageProps {
  modelUrl: string
  fallback: string
  input: Omit<FigureInput, 'reducedMotion'>
  look: LookPoint
}

export const Live2DStage = forwardRef<FigureHandle, StageProps>(function Live2DStage(
  { modelUrl, fallback, input, look },
  forwarded
) {
  const canvas = useRef<HTMLCanvasElement>(null)
  const inputRef = useRef(input)
  const lookRef = useRef(look)
  const reducedRef = useRef(false)
  const nodRef = useRef<FigureEngine['nod']>(() => undefined)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const reduced = useReducedMotion()

  inputRef.current = input
  lookRef.current = look
  reducedRef.current = reduced

  useImperativeHandle(forwarded, () => ({ nod: () => nodRef.current() }))

  useEffect(() => {
    const element = canvas.current

    if (!element) return
    let engine: FigureEngine | null = null
    let alive = true

    setReady(false)
    setFailed(false)
    void mountFigure(element, modelUrl, () => ({
      input: { ...inputRef.current, reducedMotion: reducedRef.current },
      look: lookRef.current
    }), () => alive)
      .then(next => {
        if (!alive) {
          next.dispose()

          return
        }

        engine = next
        nodRef.current = next.nod
        setReady(true)
      })
      .catch(error => {
        console.error(error)
        if (alive) setFailed(true)
      })

    return () => {
      alive = false
      nodRef.current = () => undefined
      engine?.dispose()
    }
  }, [modelUrl])

  return (
    <span className="sprite portrait">
      {failed || !ready ? <img alt="" draggable={false} src={fallback} /> : null}
      <canvas ref={canvas} className="live2d" style={{ visibility: ready && !failed ? 'visible' : 'hidden' }} />
    </span>
  )
})

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(query.matches)

    query.addEventListener('change', onChange)

    return () => query.removeEventListener('change', onChange)
  }, [])

  return reduced
}
