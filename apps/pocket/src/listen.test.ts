import { expect, it, vi } from 'vitest'
import { openEar } from './listen'

it('pauses automatic recording, resumes listening and permits manual recording while paused', async () => {
  vi.useFakeTimers()
  const onStart = vi.fn()
  const onUtterance = vi.fn()
  const onLevel = vi.fn()
  let listening = false
  const stopTrack = vi.fn()
  const getUserMedia = vi.fn().mockResolvedValue({ getTracks: () => [{ stop: stopTrack }] })
  class TestContext {
    createMediaStreamSource() { return { connect: vi.fn() } }
    createAnalyser() { return { fftSize: 512, getByteTimeDomainData: (samples: Uint8Array) => samples.fill(180) } }
    close() { return Promise.resolve() }
  }
  class TestRecorder {
    static isTypeSupported() { return true }
    state = 'recording'
    onstop?: () => void
    start() {}
    stop() { this.state = 'inactive'; this.onstop?.() }
  }
  vi.stubGlobal('window', globalThis)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
  vi.stubGlobal('AudioContext', TestContext)
  vi.stubGlobal('MediaRecorder', TestRecorder)
  try {
    const ear = await openEar(onUtterance, () => listening, onLevel, onStart)
    await vi.advanceTimersByTimeAsync(240)
    expect(onStart).not.toHaveBeenCalled()
    expect(onLevel).toHaveBeenLastCalledWith(0)
    listening = true
    await vi.advanceTimersByTimeAsync(240)
    expect(onStart).toHaveBeenCalledTimes(1)
    expect(onUtterance).not.toHaveBeenCalled()
    listening = false
    await vi.advanceTimersByTimeAsync(240)
    expect(onLevel).toHaveBeenLastCalledWith(0)
    expect(onUtterance).not.toHaveBeenCalled()
    ear.holdStart()
    await vi.advanceTimersByTimeAsync(120)
    expect(onStart).toHaveBeenCalledTimes(2)
    expect(onLevel.mock.lastCall?.[0]).toBeGreaterThan(0)
    await ear.holdEnd()
    expect(getUserMedia).toHaveBeenCalledWith({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
    ear.stop()
    expect(stopTrack).toHaveBeenCalledOnce()
  } finally {
    vi.clearAllTimers()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  }
})