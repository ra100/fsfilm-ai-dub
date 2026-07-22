import { useEffect, useRef, type MouseEvent, type PointerEvent, type WheelEvent } from 'react'

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
  const scrollerRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<{ pointerId: number; startX: number; startScrollLeft: number } | null>(null)
  const didDragRef = useRef(false)
  const roles = [...new Set(groups.map((group) => group.role))].sort()
  const width = 1200
  const waveformHeight = 86
  const laneHeight = 44
  const left = 58
  const duration = Math.max(waveform.duration, 0.001)
  const normalizedZoom = clamp(Number.isFinite(zoom) ? zoom : 1, 1, 16)
  const contentWidth = (width - left - 8) * normalizedZoom
  const displayWidth = left + contentWidth + 8
  const height = waveformHeight + roles.length * laneHeight + 27
  const estimatedVisibleDuration = duration / normalizedZoom
  const estimatedMaximumStart = Math.max(0, duration - estimatedVisibleDuration)
  const displayedStart = clamp(viewStart, 0, estimatedMaximumStart)
  const displayedEnd = Math.min(duration, displayedStart + estimatedVisibleDuration)
  const timeToX = (seconds: number) => left + clamp(seconds / duration, 0, 1) * contentWidth

  const sampleCount = Math.min(waveform.min.length, waveform.max.length)
  const amplitude = Math.max(0.02, ...waveform.min.map(Math.abs), ...waveform.max.map(Math.abs))
  const wavePath = waveform.min.slice(0, sampleCount).map((minimum, index) => {
    const x = left + (index / Math.max(1, sampleCount - 1)) * contentWidth
    const low = waveformHeight / 2 - (minimum / amplitude) * (waveformHeight * 0.4)
    const high = waveformHeight / 2 - (waveform.max[index] / amplitude) * (waveformHeight * 0.4)
    return `M${x.toFixed(2)} ${low.toFixed(2)}L${x.toFixed(2)} ${high.toFixed(2)}`
  }).join('')

  function rangeForScroller(scroller: HTMLDivElement) {
    const visibleDuration = duration * scroller.clientWidth / Math.max(1, scroller.scrollWidth)
    return { visibleDuration, maximumStart: Math.max(0, duration - visibleDuration) }
  }

  function scrollToStart(seconds: number) {
    const scroller = scrollerRef.current
    if (!scroller) return
    const { maximumStart } = rangeForScroller(scroller)
    const maximumScroll = Math.max(0, scroller.scrollWidth - scroller.clientWidth)
    const nextStart = clamp(seconds, 0, maximumStart)
    scroller.scrollLeft = maximumStart > 0 ? nextStart / maximumStart * maximumScroll : 0
    onViewStartChange(nextStart)
  }

  function syncViewStart(scroller: HTMLDivElement) {
    const { maximumStart } = rangeForScroller(scroller)
    const maximumScroll = Math.max(0, scroller.scrollWidth - scroller.clientWidth)
    onViewStartChange(maximumScroll > 0 ? scroller.scrollLeft / maximumScroll * maximumStart : 0)
  }

  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const { maximumStart } = rangeForScroller(scroller)
    const maximumScroll = Math.max(0, scroller.scrollWidth - scroller.clientWidth)
    const wanted = maximumStart > 0 ? clamp(viewStart, 0, maximumStart) / maximumStart * maximumScroll : 0
    if (Math.abs(scroller.scrollLeft - wanted) > 2) scroller.scrollLeft = wanted
  }, [duration, normalizedZoom, viewStart])

  function setZoom(nextZoom: number, focusRatio = 0.5) {
    const targetZoom = clamp(nextZoom, 1, 16)
    const scroller = scrollerRef.current
    const { visibleDuration } = scroller ? rangeForScroller(scroller) : { visibleDuration: estimatedVisibleDuration }
    const focus = displayedStart + clamp(focusRatio, 0, 1) * visibleDuration
    const targetVisibleDuration = duration / targetZoom
    onViewStartChange(clamp(focus - clamp(focusRatio, 0, 1) * targetVisibleDuration, 0, Math.max(0, duration - targetVisibleDuration)))
    onZoomChange(targetZoom)
  }

  function pan(ratio: number) {
    const scroller = scrollerRef.current
    const { visibleDuration } = scroller ? rangeForScroller(scroller) : { visibleDuration: estimatedVisibleDuration }
    scrollToStart(displayedStart + visibleDuration * ratio)
  }

  function seekFromPointer(event: MouseEvent<SVGSVGElement>) {
    if (didDragRef.current) {
      didDragRef.current = false
      return
    }
    const bounds = event.currentTarget.getBoundingClientRect()
    const ratio = clamp((event.clientX - bounds.left) / bounds.width, 0, 1)
    onSeek(duration * ratio)
  }

  function zoomWithWheel(event: WheelEvent<HTMLDivElement>) {
    if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) return
    event.preventDefault()
    const bounds = event.currentTarget.getBoundingClientRect()
    const focusRatio = clamp((event.clientX - bounds.left) / Math.max(1, bounds.width), 0, 1)
    setZoom(normalizedZoom * Math.exp(-event.deltaY * 0.0018), focusRatio)
  }

  function beginDrag(event: PointerEvent<SVGSVGElement>) {
    const scroller = scrollerRef.current
    if (!scroller) return
    dragRef.current = { pointerId: event.pointerId, startX: event.clientX, startScrollLeft: scroller.scrollLeft }
    didDragRef.current = false
    event.currentTarget.setPointerCapture(event.pointerId)
  }

  function dragTimeline(event: PointerEvent<SVGSVGElement>) {
    const drag = dragRef.current
    const scroller = scrollerRef.current
    if (!drag || !scroller || drag.pointerId !== event.pointerId) return
    const delta = event.clientX - drag.startX
    if (Math.abs(delta) > 3) didDragRef.current = true
    scroller.scrollLeft = drag.startScrollLeft - delta
  }

  function endDrag(event: PointerEvent<SVGSVGElement>) {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  const gridDivisions = Math.max(4, Math.ceil(4 * normalizedZoom))
  const gridRatios = Array.from({ length: gridDivisions + 1 }, (_, index) => index / gridDivisions)

  return <section className="timeline-card" aria-label="Source waveform and character timeline">
    <div className="timeline-heading">
      <div><h3>Source performance timeline</h3><span>Wheel to zoom · drag or use the horizontal scrollbar to pan · click to seek</span></div>
      <div className="timeline-controls" aria-label="Timeline zoom and pan">
        <button className="secondary" type="button" onClick={() => pan(-0.65)} disabled={displayedStart <= 0}>←</button>
        <button className="secondary" type="button" onClick={() => setZoom(normalizedZoom / 1.5)} disabled={normalizedZoom <= 1}>−</button>
        <label>Zoom <input aria-label="Timeline zoom" type="range" min="1" max="16" step="0.25" value={normalizedZoom} onChange={(event) => setZoom(Number(event.target.value))} /></label>
        <button className="secondary" type="button" onClick={() => setZoom(normalizedZoom * 1.5)} disabled={normalizedZoom >= 16}>+</button>
        <button className="secondary" type="button" onClick={() => { onZoomChange(1); onViewStartChange(0) }} disabled={normalizedZoom === 1 && displayedStart === 0}>Fit</button>
        <button className="secondary" type="button" onClick={() => pan(0.65)} disabled={displayedStart >= estimatedMaximumStart}>→</button>
        <span>{normalizedZoom.toFixed(1)}× · {formatTime(displayedStart)}–{formatTime(displayedEnd)}</span>
      </div>
    </div>
    <div className="timeline-scroll" ref={scrollerRef} onWheel={zoomWithWheel} onScroll={(event) => syncViewStart(event.currentTarget)}>
      <svg className="timeline" style={{ width: `${normalizedZoom * 100}%` }} viewBox={`0 0 ${displayWidth} ${height}`} role="img" onClick={seekFromPointer} onPointerDown={beginDrag} onPointerMove={dragTimeline} onPointerUp={endDrag} onPointerCancel={endDrag}>
        <rect width={displayWidth} height={waveformHeight} className="wave-background" />
        {gridRatios.map((ratio) => <g key={ratio}><line x1={left + ratio * contentWidth} x2={left + ratio * contentWidth} y1="0" y2={height - 24} className="timeline-grid" /><text x={left + ratio * contentWidth} y={height - 7} className="time-label" textAnchor={ratio === 0 ? 'start' : ratio === 1 ? 'end' : 'middle'}>{formatTime(duration * ratio)}</text></g>)}
        <path d={wavePath} className="waveform-path" />
        {roles.map((role, index) => {
          const laneY = waveformHeight + index * laneHeight
          return <g key={role}>
            <text x={left - 8} y={laneY + 25} className="lane-label" textAnchor="end">{role}</text>
            <line x1={left} x2={displayWidth - 8} y1={laneY + laneHeight} y2={laneY + laneHeight} className="lane-divider" />
            {groups.filter((group) => group.role === role).map((group) => {
              const x = timeToX(group.source_start)
              const groupEnd = timeToX(group.source_end)
              const selected = group.group === selectedGroup
              return <g key={group.group} className="timeline-group" onClick={(event) => { event.stopPropagation(); if (didDragRef.current) { didDragRef.current = false; return } onSelect(group) }}>
                <rect x={x} y={laneY + 7} width={Math.max(5, groupEnd - x)} height={laneHeight - 14} rx="4" fill={roleColor(role)} opacity={selected ? 1 : 0.72} className={selected ? 'timeline-group-selected' : ''} />
                <text x={x + 5} y={laneY + 25} className="group-label">{String(group.group).padStart(2, '0')}</text>
                <title>{`${role} · group ${group.group} · ${formatTime(group.source_start)}–${formatTime(group.source_end)}`}</title>
              </g>
            })}
          </g>
        })}
        <line x1={timeToX(playhead)} x2={timeToX(playhead)} y1="0" y2={height - 24} className="playhead" />
      </svg>
    </div>
  </section>
}
