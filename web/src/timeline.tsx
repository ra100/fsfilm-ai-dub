import type { MouseEvent } from 'react'

export type Waveform = {
  duration: number
  bins: number
  min: number[]
  max: number[]
}

export type TimelineGroup = {
  group: number
  role: string
  source_start: number
  source_end: number
  translation_state: string
  candidate_count: number
  selection: { candidate?: number } | null
}

const palette = ['#5bd9b5', '#7cb8ff', '#f0c36b', '#e88db4', '#ae9cf5', '#67cbd5', '#ee9d68', '#b4d47a']

export function roleColor(role: string): string {
  let value = 0
  for (const character of role) value = (value * 31 + character.charCodeAt(0)) >>> 0
  return palette[value % palette.length]
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value))
}

function formatTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds - minutes * 60
  return `${minutes}:${remainder.toFixed(1).padStart(4, '0')}`
}

type Props = {
  waveform: Waveform
  groups: TimelineGroup[]
  selectedGroup: number | null
  playhead: number
  zoom: number
  viewStart: number
  onZoomChange: (zoom: number) => void
  onViewStartChange: (seconds: number) => void
  onSeek: (seconds: number) => void
  onSelect: (group: TimelineGroup) => void
}

export function CharacterTimeline({
  waveform, groups, selectedGroup, playhead, zoom, viewStart, onZoomChange, onViewStartChange, onSeek, onSelect,
}: Props) {
  const roles = [...new Set(groups.map((group) => group.role))].sort()
  const width = 1200
  const waveformHeight = 86
  const laneHeight = 44
  const left = 58
  const contentWidth = width - left - 8
  const height = waveformHeight + roles.length * laneHeight + 27
  const duration = Math.max(waveform.duration, 0.001)
  const normalizedZoom = clamp(Number.isFinite(zoom) ? zoom : 1, 1, 16)
  const viewDuration = duration / normalizedZoom
  const maximumStart = Math.max(0, duration - viewDuration)
  const start = clamp(viewStart, 0, maximumStart)
  const end = start + viewDuration
  const timeToX = (seconds: number) => left + clamp((seconds - start) / viewDuration, 0, 1) * contentWidth

  const sampleCount = Math.min(waveform.min.length, waveform.max.length)
  const firstSample = clamp(Math.floor((start / duration) * Math.max(0, sampleCount - 1)), 0, Math.max(0, sampleCount - 1))
  const lastSample = clamp(Math.ceil((end / duration) * Math.max(0, sampleCount - 1)), firstSample, Math.max(0, sampleCount - 1))
  const visibleMinimum = waveform.min.slice(firstSample, lastSample + 1)
  const visibleMaximum = waveform.max.slice(firstSample, lastSample + 1)
  const amplitude = Math.max(0.02, ...visibleMinimum.map(Math.abs), ...visibleMaximum.map(Math.abs))
  const wavePath = visibleMinimum.map((minimum, index) => {
    const sourceIndex = firstSample + index
    const seconds = sampleCount > 1 ? (sourceIndex / (sampleCount - 1)) * duration : start
    const x = timeToX(seconds)
    const low = waveformHeight / 2 - (minimum / amplitude) * (waveformHeight * 0.4)
    const high = waveformHeight / 2 - (visibleMaximum[index] / amplitude) * (waveformHeight * 0.4)
    return `M${x.toFixed(2)} ${low.toFixed(2)}L${x.toFixed(2)} ${high.toFixed(2)}`
  }).join('')

  function seekFromPointer(event: MouseEvent<SVGSVGElement>) {
    const bounds = event.currentTarget.getBoundingClientRect()
    const ratio = clamp((event.clientX - bounds.left) / bounds.width, 0, 1)
    onSeek(start + ratio * viewDuration)
  }

  function setZoom(nextZoom: number) {
    const targetZoom = clamp(nextZoom, 1, 16)
    const center = start + viewDuration / 2
    const nextDuration = duration / targetZoom
    onViewStartChange(clamp(center - nextDuration / 2, 0, Math.max(0, duration - nextDuration)))
    onZoomChange(targetZoom)
  }

  function pan(ratio: number) {
    onViewStartChange(clamp(start + viewDuration * ratio, 0, maximumStart))
  }

  return <section className="timeline-card" aria-label="Source waveform and character timeline">
    <div className="timeline-heading">
      <div><h3>Source performance timeline</h3><span>Click to seek · coloured lanes are dialogue turns</span></div>
      <div className="timeline-controls" aria-label="Timeline zoom and pan">
        <button className="secondary" type="button" onClick={() => pan(-0.65)} disabled={start <= 0}>←</button>
        <button className="secondary" type="button" onClick={() => setZoom(normalizedZoom / 1.5)} disabled={normalizedZoom <= 1}>−</button>
        <label>Zoom <input aria-label="Timeline zoom" type="range" min="1" max="16" step="0.25" value={normalizedZoom} onChange={(event) => setZoom(Number(event.target.value))} /></label>
        <button className="secondary" type="button" onClick={() => setZoom(normalizedZoom * 1.5)} disabled={normalizedZoom >= 16}>+</button>
        <button className="secondary" type="button" onClick={() => { onZoomChange(1); onViewStartChange(0) }} disabled={normalizedZoom === 1 && start === 0}>Fit</button>
        <button className="secondary" type="button" onClick={() => pan(0.65)} disabled={start >= maximumStart}>→</button>
        <span>{normalizedZoom.toFixed(1)}× · {formatTime(start)}–{formatTime(end)}</span>
      </div>
    </div>
    <div className="timeline-scroll">
      <svg className="timeline" viewBox={`0 0 ${width} ${height}`} role="img" onClick={seekFromPointer}>
        <rect width={width} height={waveformHeight} className="wave-background" />
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => <g key={ratio}><line x1={left + ratio * contentWidth} x2={left + ratio * contentWidth} y1="0" y2={height - 24} className="timeline-grid" /><text x={left + ratio * contentWidth} y={height - 7} className="time-label" textAnchor={ratio === 0 ? 'start' : ratio === 1 ? 'end' : 'middle'}>{formatTime(start + viewDuration * ratio)}</text></g>)}
        <path d={wavePath} className="waveform-path" />
        {roles.map((role, index) => {
          const laneY = waveformHeight + index * laneHeight
          return <g key={role}>
            <text x={left - 8} y={laneY + 25} className="lane-label" textAnchor="end">{role}</text>
            <line x1={left} x2={width - 8} y1={laneY + laneHeight} y2={laneY + laneHeight} className="lane-divider" />
            {groups.filter((group) => group.role === role && group.source_end >= start && group.source_start <= end).map((group) => {
              const x = timeToX(group.source_start)
              const groupEnd = timeToX(group.source_end)
              const selected = group.group === selectedGroup
              return <g key={group.group} className="timeline-group" onClick={(event) => { event.stopPropagation(); onSelect(group) }}>
                <rect x={x} y={laneY + 7} width={Math.max(5, groupEnd - x)} height={laneHeight - 14} rx="4" fill={roleColor(role)} opacity={selected ? 1 : 0.72} className={selected ? 'timeline-group-selected' : ''} />
                <text x={x + 5} y={laneY + 25} className="group-label">{String(group.group).padStart(2, '0')}</text>
                <title>{`${role} · group ${group.group} · ${formatTime(group.source_start)}–${formatTime(group.source_end)}`}</title>
              </g>
            })}
          </g>
        })}
        {playhead >= start && playhead <= end && <line x1={timeToX(playhead)} x2={timeToX(playhead)} y1="0" y2={height - 24} className="playhead" />}
      </svg>
    </div>
  </section>
}
