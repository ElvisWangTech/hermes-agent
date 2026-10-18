import type { Mood } from '../characters'

/** Same threshold the caption uses for「正在听」. */
export const heardLevel = 0.16

export interface FigureInput {
  mood: Mood
  muted: boolean
  confirming: boolean
  inputLevel: number
  outputLevel: number
  reducedMotion: boolean
}

export type BodyMotion = 'idle' | 'speak' | 'still'

export interface FigurePose {
  motion: BodyMotion
  /** -1..1, viewer's right is positive. */
  lookX: number
  /** -1..1, up is positive. */
  lookY: number
  brow: number
  lean: number
  tilt: number
  /** Playback mouth opening. Null leaves the motion in charge. */
  mouth: number | null
}

export interface LookPoint {
  x: number
  y: number
}

const rest: FigurePose = {
  motion: 'idle',
  lookX: 0,
  lookY: 0,
  brow: 0,
  lean: 0,
  tilt: 0,
  mouth: null
}

export function figurePose(input: FigureInput): FigurePose {
  const speaking = input.mood === 'speak'
  const thinking = input.mood === 'think'
  const listening = input.mood === 'listen' || input.inputLevel > heardLevel
  const pose: FigurePose = { ...rest, mouth: speaking ? clampUnit(input.outputLevel) : null }

  if (speaking) {
    pose.motion = 'speak'
    pose.lookY = -0.08
  } else if (thinking) {
    pose.lookX = 0.55
    pose.lookY = 0.2
    pose.brow = -0.2
  } else if (listening) {
    pose.lookY = -0.35
    pose.brow = 0.25
    pose.lean = 0.35
  } else if (input.confirming) {
    pose.lookY = -0.12
    pose.brow = 0.35
  } else if (input.muted) {
    pose.tilt = -0.25
    pose.brow = -0.1
  }

  if (input.reducedMotion) pose.motion = 'still'

  return pose
}

export function lookAt(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number }
): LookPoint {
  if (rect.width <= 0 || rect.height <= 0) return { x: 0, y: 0 }

  return {
    x: clampUnit((clientX - rect.left) / rect.width * 2 - 1),
    y: clampUnit(-((clientY - rect.top) / rect.height * 2 - 1))
  }
}

function clampUnit(value: number): number {
  if (value < 0 && value < -1) return -1
  if (value < 0) return value
  if (value > 1) return 1

  return value
}
