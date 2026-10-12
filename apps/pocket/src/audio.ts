export function audioLevel(samples: Uint8Array): number {
  if (!samples.length) return 0
  let energy = 0
  for (const sample of samples) {
    const centered = (sample - 128) / 128
    energy += centered * centered
  }
  return Math.min(1, Math.sqrt(energy / samples.length) * 4)
}

export function measurePlayback(audio: HTMLAudioElement, onLevel: (level: number) => void): () => void {
  const context = new AudioContext()
  const source = context.createMediaElementSource(audio)
  const analyser = context.createAnalyser()
  analyser.fftSize = 512
  source.connect(analyser)
  analyser.connect(context.destination)
  const samples = new Uint8Array(analyser.fftSize)
  let timer = 0
  const tick = () => {
    analyser.getByteTimeDomainData(samples)
    onLevel(audioLevel(samples))
    timer = window.setTimeout(tick, 80)
  }
  void context.resume().catch(() => undefined)
  tick()
  return () => {
    window.clearTimeout(timer)
    onLevel(0)
    source.disconnect()
    analyser.disconnect()
    void context.close()
  }
}