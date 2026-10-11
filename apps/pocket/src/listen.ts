export interface Ear {
  stop: () => void
  holdStart: () => void
  holdEnd: () => Promise<Blob | null>
}

export async function openEar(onUtterance: (blob: Blob) => void, live: () => boolean): Promise<Ear> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const context = new AudioContext()
  const source = context.createMediaStreamSource(stream)
  const analyser = context.createAnalyser()

  analyser.fftSize = 512
  source.connect(analyser)
  const samples = new Uint8Array(analyser.fftSize)
  let recorder: MediaRecorder | null = null
  let chunks: Blob[] = []
  let heard = false
  let quietSince = 0
  let holding = false
  let timer = 0

  const mime = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : ''

  const start = () => {
    if (recorder) return
    chunks = []
    recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
    recorder.ondataavailable = event => {
      if (event.data.size) chunks.push(event.data)
    }
    recorder.start()
  }

  const finish = () => {
    const current = recorder

    if (!current || current.state === 'inactive') {
      recorder = null

      return Promise.resolve(null)
    }

    return new Promise<Blob | null>(resolve => {
      current.onstop = () => {
        const blob = chunks.length ? new Blob(chunks, { type: current.mimeType || 'audio/webm' }) : null

        recorder = null
        chunks = []
        resolve(blob && blob.size > 800 ? blob : null)
      }
      current.stop()
    })
  }

  const tick = () => {
    if (!live() || holding) {
      timer = window.setTimeout(tick, 120)

      return
    }

    analyser.getByteTimeDomainData(samples)
    let sum = 0

    for (const sample of samples) {
      const centered = (sample - 128) / 128

      sum += centered * centered
    }

    const loud = Math.sqrt(sum / samples.length) > 0.04

    if (loud) {
      heard = true
      quietSince = 0
      start()
    } else if (recorder && heard) {
      quietSince = quietSince || performance.now()
      if (performance.now() - quietSince > 900) {
        heard = false
        quietSince = 0
        void finish().then(blob => {
          if (blob) onUtterance(blob)
        })
      }
    }

    timer = window.setTimeout(tick, 120)
  }

  timer = window.setTimeout(tick, 120)

  return {
    stop: () => {
      window.clearTimeout(timer)
      void finish()
      stream.getTracks().forEach(track => track.stop())
      void context.close()
    },
    holdStart: () => {
      holding = true
      heard = false
      start()
    },
    holdEnd: async () => {
      holding = false

      return finish()
    }
  }
}
