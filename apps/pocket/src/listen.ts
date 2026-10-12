import { audioLevel } from './audio'

export interface Ear {
  stop: () => void
  holdStart: () => void
  holdEnd: () => Promise<Blob | null>
}

export async function openEar(onUtterance: (blob: Blob) => void, live: () => boolean, onLevel: (level: number) => void = () => {}, onSpeechStart: () => void = () => {}): Promise<Ear> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
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
    onSpeechStart()
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
    analyser.getByteTimeDomainData(samples)
    const listening = live()
    onLevel(listening || holding ? audioLevel(samples) : 0)
    if (!listening || holding) {
      if (!holding && recorder) {
        heard = false
        quietSince = 0
        void finish()
      }
      timer = window.setTimeout(tick, 120)

      return
    }

    const loud = audioLevel(samples) > 0.16

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
      onLevel(0)
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
