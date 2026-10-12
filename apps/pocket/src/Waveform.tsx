const segments = [0.18, 0.32, 0.5, 0.28, 0.68, 0.44, 0.82, 0.58, 1, 0.72, 0.9, 0.52, 0.76, 0.38, 0.6, 0.3, 0.46, 0.24, 0.16]

export function Waveform({ label, level }: { label: string; level: number }) {
  const amplitude = Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 0
  return (
    <div className="audio-channel">
      <span className="audio-label">{label}</span>
      <div className="waveform" role="meter" aria-label={`${label}的音量`}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(amplitude * 100)}>
        <div className="waveform-bars" aria-hidden="true">
          {segments.map((weight, index) => (
            <span key={index} style={{ height: `${3 + amplitude * weight * 37}px`, opacity: 0.35 + amplitude * 0.65 }} />
          ))}
        </div>
      </div>
    </div>
  )
}