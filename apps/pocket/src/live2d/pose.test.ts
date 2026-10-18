import { describe, expect, it } from 'vitest'
import { figurePose, lookAt, type FigureInput } from './pose'

const quiet: FigureInput = {
  mood: 'idle',
  muted: false,
  confirming: false,
  inputLevel: 0,
  outputLevel: 0,
  reducedMotion: false
}

describe('figurePose', () => {
  it('opens the mouth from the playback level only while she is speaking', () => {
    const speaking = figurePose({ ...quiet, mood: 'speak', outputLevel: 0.4 })
    const idle = figurePose({ ...quiet, outputLevel: 0.4 })

    expect(speaking.mouth).toBe(0.4)
    expect(speaking.motion).toBe('speak')
    expect(idle.mouth).toBeNull()
    expect(idle.motion).toBe('idle')
  })

  it('keeps the voice on a still body when motion is reduced', () => {
    const pose = figurePose({ ...quiet, mood: 'speak', outputLevel: 2, reducedMotion: true })

    expect(pose.motion).toBe('still')
    expect(pose.mouth).toBe(1)
  })

  it('leans toward a speaker and looks aside while thinking', () => {
    const idle = figurePose(quiet)
    const listen = figurePose({ ...quiet, inputLevel: 0.4 })
    const think = figurePose({ ...quiet, mood: 'think', outputLevel: 0.8 })

    expect(listen.lean).toBeGreaterThan(idle.lean)
    expect(listen.lookY).toBeLessThan(idle.lookY)
    expect(think.lookX).toBeGreaterThan(listen.lookX)
    expect(think.mouth).toBeNull()
  })

  it('raises the brows while waiting and tilts when the microphone is off', () => {
    const waiting = figurePose({ ...quiet, confirming: true })
    const muted = figurePose({ ...quiet, muted: true, mood: 'wait' })

    expect(waiting.brow).toBeGreaterThan(0)
    expect(muted.tilt).not.toBe(0)
    expect(waiting.mouth).toBeNull()
  })
})

describe('lookAt', () => {
  const rect = { left: 0, top: 0, width: 100, height: 100 }

  it('maps the right and the top of the stage to positive look axes', () => {
    expect(lookAt(80, 50, rect).x).toBeGreaterThan(0)
    expect(lookAt(50, 20, rect).y).toBeGreaterThan(0)
    expect(lookAt(20, 80, rect).x).toBeLessThan(0)
    expect(lookAt(20, 80, rect).y).toBeLessThan(0)
  })
})