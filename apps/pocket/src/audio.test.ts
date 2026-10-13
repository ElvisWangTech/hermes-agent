import { expect, it, vi } from 'vitest'
import { measurePlayback } from './audio'

it('releases playback resources only once on repeated cleanup', () => {
  const close = vi.fn().mockResolvedValue(undefined)
  const disconnect = vi.fn()
  class TestContext {
    state = 'running'
    destination = {}
    createMediaElementSource() { return { connect: vi.fn(), disconnect } }
    createAnalyser() { return { fftSize: 512, connect: vi.fn(), disconnect, getByteTimeDomainData: (samples: Uint8Array) => samples.fill(128) } }
    resume() { return Promise.resolve() }
    close = close
  }
  vi.useFakeTimers()
  vi.stubGlobal('window', globalThis)
  vi.stubGlobal('AudioContext', TestContext)
  try {
    const stop = measurePlayback({} as HTMLAudioElement, vi.fn())
    stop()
    stop()
    expect(close).toHaveBeenCalledTimes(1)
    expect(disconnect).toHaveBeenCalledTimes(2)
    expect(vi.getTimerCount()).toBe(0)
  } finally {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  }
})