import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ImportWizard } from './import-wizard'
import { CharacterTimeline, roleColor, type Waveform } from './timeline'
import './styles.css'

type Counts = { turns: number; approved_translations: number; rendered_turns: number; selected_turns: number; qa_warning_turns?: number }
type Language = { code?: string; name?: string }
type Project = { id: string; name: string; config_path: string; has_video: boolean; frame_rate?: number | null; video_delay_seconds?: number; stage: 'initialized' | 'ready'; counts: Counts; review_files: Record<string, boolean>; languages?: { source?: Language; target?: Language } }
type Group = {
  group: number; role: string; source_start: number; source_end: number; source_text: string; legacy_target_text: string
  lip_sync_text: string; target_word_budget: number; translation_state: string; timing_state: string; role_confidence: number
  candidate_count: number; selection: { candidate?: number; duration?: number; review?: string[] } | null; qa_flags: string[]
}
type TranslationRow = { group: string; role: string; lip_sync_text: string; approved: string; translator_notes: string; [key: string]: string }
type TranslationQcRow = { group: string; role: string; word_budget: string; word_count: string; translation_state: string; issues: string; text: string }
type Job = { job_id: string; command_name: string; state: string; created_at: string; started_at: string | null; finished_at: string | null; return_code?: number | null; error: string | null; arguments?: { command?: string; groups?: number[]; roles?: string[]; variants?: number; force?: boolean; strict?: boolean; confirm?: boolean } }
type Candidate = { variant: number; duration: number | null; seed: number | null; word_recall: number | null; ending_present: boolean | null; available_duration: number | null; overrun: number | null; score: number | null; transcript: string | null; selected: boolean }
type CandidateInfo = { group: number; role: string; text: string; candidates: Candidate[]; selection: { candidate?: number; review?: string[] } | null }
type RoleInfo = { role: string; configured_reference: boolean; configured_emotion: boolean; generated_reference: boolean }
type PauseMarker = { after_word: number; duration_ms: number; mode: 'natural' | 'hard' }
type PauseSummary = { group: number; markers: PauseMarker[]; estimated_speech_seconds: number; requested_pause_seconds: number; available_seconds: number; remaining_seconds: number }
type DeliveryAsset = { name: string; size: number; kind: 'audio' | 'subtitle' }
type ApiError = Error & { detail?: string }
type GroupFilter = 'all' | 'needs-review' | 'qa-warning' | 'unselected' | 'unrendered'
type AuditionMode = 'source' | 'selected' | number

const apiBase = import.meta.env.VITE_API_BASE ?? ''

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('content-type', 'application/json')
  const response = await fetch(`${apiBase}${path}`, { ...init, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const error = new Error(payload.detail || `Request failed (${response.status})`) as ApiError
    error.detail = payload.detail
    throw error
  }
  return response.json() as Promise<T>
}

function timestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds - minutes * 60
  return `${minutes}:${remainder.toFixed(2).padStart(5, '0')}`
}

function timecode(seconds: number, frameRate: number): string {
  const safeRate = Number.isFinite(frameRate) && frameRate > 0 ? frameRate : 24
  const wholeSeconds = Math.max(0, Math.floor(seconds))
  const frames = Math.min(Math.round(safeRate) - 1, Math.floor((seconds - wholeSeconds) * safeRate + 1e-5))
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainder = wholeSeconds % 60
  return [hours, minutes, remainder, frames].map((value) => String(value).padStart(2, '0')).join(':')
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value))
}

function pictureDelayLabel(delay: number): string {
  if (Math.abs(delay) < 0.001) return 'in sync'
  return `${Math.abs(delay).toFixed(2)} s ${delay > 0 ? 'later' : 'earlier'}`
}

function CandidateWaveform({ waveform, onSeek }: { waveform: Waveform | undefined; onSeek: (ratio: number) => void }) {
  if (!waveform) return null
  const bins = Math.min(waveform.min.length, waveform.max.length)
  if (!bins) return null
  const width = 240
  const height = 42
  const center = height / 2
  const count = Math.min(96, bins)
  const amplitude = Math.max(0.02, ...waveform.min.map(Math.abs), ...waveform.max.map(Math.abs))
  const points = Array.from({ length: count }, (_, index) => {
    const bin = Math.round(index / Math.max(1, count - 1) * Math.max(0, bins - 1))
    const x = index / Math.max(1, count - 1) * width
    return { x, top: center - waveform.max[bin] / amplitude * (height * 0.42), bottom: center - waveform.min[bin] / amplitude * (height * 0.42) }
  })
  const line = points.map((point) => `${point.x.toFixed(1)} ${point.top.toFixed(1)}`).join(' L ')
  const lower = [...points].reverse().map((point) => `${point.x.toFixed(1)} ${point.bottom.toFixed(1)}`).join(' L ')
  return <svg className="candidate-waveform" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-label="Candidate waveform; click to seek the audio preview" role="img" onClick={(event) => { const bounds = event.currentTarget.getBoundingClientRect(); onSeek(clamp((event.clientX - bounds.left) / Math.max(1, bounds.width), 0, 1)) }}><line x1="0" x2={width} y1={center} y2={center} /><path d={`M ${line} L ${lower} Z`} /></svg>
}

function languageLabel(language: Language | undefined, fallback: string): string {
  const name = language?.name?.trim()
  const code = language?.code?.trim()
  if (name && code && name.toLowerCase() !== code.toLowerCase()) return `${name} (${code})`
  return name || code || fallback
}

function App() {
  const videoRef = useRef<HTMLVideoElement>(null)
  const sourceAudioRef = useRef<HTMLAudioElement>(null)
  const auditionAudioRef = useRef<HTMLAudioElement>(null)
  const candidateAudioRefs = useRef(new Map<number, HTMLAudioElement>())
  const auditionTimerRef = useRef<number | null>(null)
  const manualSeekRef = useRef(false)
  const previousPlayheadRef = useRef(0)
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [groups, setGroups] = useState<Group[]>([])
  const [translations, setTranslations] = useState<Map<number, TranslationRow>>(new Map())
  const [translationQc, setTranslationQc] = useState<TranslationQcRow[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [waveform, setWaveform] = useState<Waveform | null>(null)
  const [rolesInfo, setRolesInfo] = useState<RoleInfo[]>([])
  const [candidates, setCandidates] = useState<CandidateInfo | null>(null)
  const [candidateWaveforms, setCandidateWaveforms] = useState<Map<number, Waveform>>(new Map())
  const [pauseMarkers, setPauseMarkers] = useState<PauseMarker[]>([])
  const [pauseSummary, setPauseSummary] = useState<PauseSummary | null>(null)
  const [selectedGroup, setSelectedGroup] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [roleDraft, setRoleDraft] = useState('')
  const [notes, setNotes] = useState('')
  const [approved, setApproved] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [playhead, setPlayhead] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [loopTurn, setLoopTurn] = useState(true)
  const [preRoll, setPreRoll] = useState(0.4)
  const [postRoll, setPostRoll] = useState(0.4)
  const [timelineZoom, setTimelineZoom] = useState(1)
  const [timelineStart, setTimelineStart] = useState(0)
  const [isProjectLoading, setIsProjectLoading] = useState(false)
  const [loadedProjectId, setLoadedProjectId] = useState('')
  const [groupFilter, setGroupFilter] = useState<GroupFilter>('all')
  const [groupQuery, setGroupQuery] = useState('')
  const [frameRate, setFrameRate] = useState(24)
  const [auditionMode, setAuditionMode] = useState<AuditionMode>('source')
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null)
  const [jobLog, setJobLog] = useState('')
  const [deliveryConfirmed, setDeliveryConfirmed] = useState(false)
  const [deliveryAssets, setDeliveryAssets] = useState<DeliveryAsset[]>([])
  const [playbackRate, setPlaybackRate] = useState(1)
  const [videoDelay, setVideoDelay] = useState(0)

  const activeProject = projects.find((project) => project.id === projectId) ?? null
  const activeGroup = groups.find((group) => group.group === selectedGroup) ?? null
  const sourceLanguage = languageLabel(activeProject?.languages?.source, 'Source language')
  const targetLanguage = languageLabel(activeProject?.languages?.target, 'Target language')
  const roles = useMemo(() => [...new Set(groups.map((group) => group.role))].sort(), [groups])
  const availableRoles = useMemo(() => [...new Set([...roles, ...rolesInfo.map((role) => role.role)])].sort(), [roles, rolesInfo])
  const pauseWords = useMemo(() => draft.match(/[\w']+/g) ?? [], [draft])
  const duration = waveform?.duration ?? 0
  const selectedJob = jobs.find((job) => job.job_id === selectedJobId) ?? null
  const activeGroupAssetJobId = jobs.find((job) => (job.command_name === 'render' || job.command_name === 'select') && job.state === 'completed' && job.arguments?.groups?.includes(selectedGroup ?? -1))?.job_id
  const latestDeliveryValidation = jobs.find((job) => job.command_name === 'validate') ?? null
  const deliveryValidationPassed = latestDeliveryValidation?.state === 'completed'
  const isInitialProjectLoad = isProjectLoading && loadedProjectId !== projectId
  const auditionAsset = auditionMode === 'selected' ? 'selected' : typeof auditionMode === 'number' ? `candidate-${auditionMode}` : null
  const missingReferences = rolesInfo.filter((role) => !role.generated_reference && !role.configured_reference)
  const translationQcIssues = useMemo(() => translationQc.filter((row) => row.issues.trim() || row.translation_state !== 'approved'), [translationQc])
  const filteredGroups = useMemo(() => {
    const query = groupQuery.trim().toLowerCase()
    return groups.filter((group) => {
      const matchesFilter = groupFilter === 'all'
        || groupFilter === 'needs-review' && group.translation_state !== 'approved'
        || groupFilter === 'qa-warning' && (group.qa_flags?.length ?? 0) > 0
        || groupFilter === 'unselected' && group.candidate_count > 0 && !group.selection?.candidate
        || groupFilter === 'unrendered' && group.candidate_count === 0
      const matchesQuery = !query || `${group.group} ${group.role} ${group.source_text} ${group.lip_sync_text}`.toLowerCase().includes(query)
      return matchesFilter && matchesQuery
    })
  }, [groups, groupFilter, groupQuery])

  async function refreshProjects(preferredProjectId = '') {
    const values = await api<Project[]>('/api/projects')
    setProjects(values)
    setProjectId((current) => preferredProjectId || current || values[0]?.id || '')
  }

  async function refreshProjectData(id = projectId) {
    if (!id) return
    setIsProjectLoading(true)
    try {
      const [nextGroups, review, qc, nextJobs, nextWaveform, nextRoles, nextDeliveryAssets] = await Promise.all([
        api<Group[]>(`/api/projects/${id}/groups`),
        api<{ rows: TranslationRow[] }>(`/api/projects/${id}/translation-review`).catch(() => ({ rows: [] })),
        api<{ rows: TranslationQcRow[] }>(`/api/projects/${id}/translation-qc`).catch(() => ({ rows: [] })),
        api<Job[]>(`/api/projects/${id}/jobs`),
        api<Waveform>(`/api/projects/${id}/waveform`).catch(() => null),
        api<RoleInfo[]>(`/api/projects/${id}/roles`).catch(() => []),
        api<DeliveryAsset[]>(`/api/projects/${id}/delivery-assets`).catch(() => []),
      ])
      setGroups(nextGroups.map((group) => ({ ...group, qa_flags: group.qa_flags ?? [] })))
      setTranslations(new Map(review.rows.map((row) => [Number(row.group), row])))
      setTranslationQc(qc.rows)
      setJobs(nextJobs)
      setWaveform(nextWaveform)
      setRolesInfo(nextRoles)
      setDeliveryAssets(nextDeliveryAssets)
      setSelectedGroup((current) => current ?? nextGroups[0]?.group ?? null)
      setLoadedProjectId(id)
    } finally {
      setIsProjectLoading(false)
    }
  }

  function clearAuditionTimer() {
    if (auditionTimerRef.current !== null) {
      window.clearTimeout(auditionTimerRef.current)
      auditionTimerRef.current = null
    }
  }

  function syncAuditionAudio(globalSeconds: number, shouldPlay: boolean) {
    clearAuditionTimer()
    if (auditionMode === 'source' || !activeGroup || !auditionAudioRef.current) return
    const audio = auditionAudioRef.current
    const offset = globalSeconds - activeGroup.source_start
    const groupDuration = activeGroup.source_end - activeGroup.source_start
    if (offset < 0) {
      if (shouldPlay) {
        auditionTimerRef.current = window.setTimeout(() => syncAuditionAudio(activeGroup.source_start, true), Math.max(0, offset * -1000 / playbackRate))
      }
      audio.pause()
      return
    }
    if (offset >= groupDuration) {
      audio.pause()
      return
    }
    if (Math.abs(audio.currentTime - offset) > 0.12) audio.currentTime = offset
    if (shouldPlay) void audio.play().catch((error: Error) => setMessage(`Target playback failed: ${error.message}`))
  }

  function syncVideo(globalSeconds: number, shouldPlay: boolean) {
    const video = videoRef.current
    if (!video) return
    const pictureStartsAt = videoDelay
    if (globalSeconds < pictureStartsAt) {
      video.pause()
      if (Math.abs(video.currentTime) > 0.03) video.currentTime = 0
      return
    }
    const maximum = Number.isFinite(video.duration) ? video.duration : Number.POSITIVE_INFINITY
    const target = clamp(globalSeconds - pictureStartsAt, 0, maximum)
    if (Math.abs(video.currentTime - target) > 0.13) video.currentTime = target
    if (shouldPlay && video.paused) void video.play().catch((error: Error) => setMessage(`Picture playback failed: ${error.message}`))
  }

  function seekCandidatePreview(variant: number, ratio: number, fallbackDuration: number) {
    const audio = candidateAudioRefs.current.get(variant)
    if (!audio) return
    const duration = Number.isFinite(audio.duration) ? audio.duration : fallbackDuration
    audio.currentTime = clamp(duration * ratio, 0, duration)
  }

  function seek(seconds: number) {
    const next = Math.max(0, Math.min(duration || Number.POSITIVE_INFINITY, seconds))
    manualSeekRef.current = true
    previousPlayheadRef.current = next
    setPlayhead(next)
    const source = sourceAudioRef.current
    if (source && Number.isFinite(next) && Math.abs(source.currentTime - next) > 0.03) source.currentTime = next
    syncVideo(next, isPlaying)
    if (isPlaying && auditionMode !== 'source') syncAuditionAudio(next, true)
  }

  function selectTurn(group: Group, seekToTurn = true) {
    setSelectedGroup(group.group)
    if (seekToTurn) seek(Math.max(0, group.source_start - preRoll))
  }

  async function togglePlayback() {
    const audio = sourceAudioRef.current
    if (!audio) return
    if (audio.paused) {
      seek(playhead)
      try {
        await audio.play()
        setIsPlaying(true)
        syncVideo(playhead, true)
        if (auditionMode !== 'source') syncAuditionAudio(playhead, true)
      } catch (error) {
        setMessage(`Playback failed: ${(error as Error).message}`)
      }
    } else {
      clearAuditionTimer()
      audio.pause()
      auditionAudioRef.current?.pause()
      videoRef.current?.pause()
      setIsPlaying(false)
    }
  }

  function updatePlayhead(seconds: number) {
    const previous = previousPlayheadRef.current
    const wasManualSeek = manualSeekRef.current
    manualSeekRef.current = false
    previousPlayheadRef.current = seconds
    setPlayhead(seconds)
    syncVideo(seconds, true)
    const loopEnd = activeGroup ? activeGroup.source_end + postRoll : Number.POSITIVE_INFINITY
    if (!wasManualSeek && isPlaying && loopTurn && activeGroup && previous < loopEnd && seconds >= loopEnd) {
      const restart = Math.max(0, activeGroup.source_start - preRoll)
      seek(restart)
      void sourceAudioRef.current?.play()
      syncVideo(restart, true)
      if (auditionMode !== 'source') syncAuditionAudio(restart, true)
    }
  }

  function setAudition(nextMode: AuditionMode) {
    clearAuditionTimer()
    auditionAudioRef.current?.pause()
    setAuditionMode(nextMode)
    if (activeGroup) seek(Math.max(0, activeGroup.source_start - preRoll))
  }

  function navigateFiltered(delta: number) {
    if (!filteredGroups.length) return
    const index = filteredGroups.findIndex((group) => group.group === selectedGroup)
    const nextIndex = index < 0 ? 0 : (index + delta + filteredGroups.length) % filteredGroups.length
    selectTurn(filteredGroups[nextIndex])
  }

  async function uploadVideo(file: File) {
    if (!projectId) return
    setBusy('video')
    try {
      const body = new FormData()
      body.set('video', file)
      const response = await fetch(`${apiBase}/api/projects/${projectId}/video`, { method: 'POST', body })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail || `Video upload failed (${response.status})`)
      await refreshProjects(projectId)
      setMessage('Picture reference added to input.video. It remains local to this project.')
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function saveVideoDelay(value: number) {
    if (!projectId) return
    const delay = Math.round(clamp(value, -5, 5) * 1000) / 1000
    setVideoDelay(delay)
    setBusy('video-delay')
    try {
      const saved = await api<{ video_delay_seconds: number }>(`/api/projects/${projectId}/video-delay`, {
        method: 'PUT', body: JSON.stringify({ video_delay_seconds: delay }),
      })
      setVideoDelay(saved.video_delay_seconds)
      setProjects((current) => current.map((project) => project.id === projectId ? { ...project, video_delay_seconds: saved.video_delay_seconds } : project))
      setMessage(`Picture timing saved: ${pictureDelayLabel(saved.video_delay_seconds)}.`)
    } catch (error) {
      setVideoDelay(activeProject?.video_delay_seconds ?? 0)
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  useEffect(() => { refreshProjects().catch((error: Error) => setMessage(error.message)) }, [])
  useEffect(() => {
    setTimelineZoom(1)
    setTimelineStart(0)
    setGroupFilter('all')
    setGroupQuery('')
    setSelectedJobId(null)
    setJobLog('')
    setAuditionMode('source')
    refreshProjectData().catch((error: Error) => setMessage(error.message))
  }, [projectId])
  useEffect(() => {
    setFrameRate(activeProject?.frame_rate && activeProject.frame_rate > 0 ? activeProject.frame_rate : 24)
  }, [activeProject?.id, activeProject?.frame_rate])
  useEffect(() => {
    setVideoDelay(activeProject?.video_delay_seconds ?? 0)
  }, [activeProject?.id, activeProject?.video_delay_seconds])
  useEffect(() => {
    for (const media of [sourceAudioRef.current, auditionAudioRef.current, videoRef.current]) if (media) media.playbackRate = playbackRate
  }, [playbackRate, auditionAsset])
  useEffect(() => {
    syncVideo(playhead, isPlaying)
  }, [videoDelay])
  useEffect(() => {
    const row = selectedGroup ? translations.get(selectedGroup) : undefined
    const group = groups.find((item) => item.group === selectedGroup)
    setDraft(row?.lip_sync_text ?? group?.lip_sync_text ?? '')
    setRoleDraft(group?.role ?? '')
    setNotes(row?.translator_notes ?? '')
    setApproved(row?.approved === 'yes')
  }, [selectedGroup, translations, groups])
  useEffect(() => {
    if (!projectId || !selectedGroup) {
      setCandidates(null)
      setCandidateWaveforms(new Map())
      setPauseMarkers([])
      setPauseSummary(null)
      return
    }
    let cancelled = false
    setCandidateWaveforms(new Map())
    Promise.all([
      api<CandidateInfo>(`/api/projects/${projectId}/groups/${selectedGroup}/candidates`).catch(() => null),
      api<{ group: number; markers: PauseMarker[] }>(`/api/projects/${projectId}/groups/${selectedGroup}/pauses`).catch(() => null),
    ]).then(([candidateInfo, pauses]) => {
      if (cancelled) return
      setCandidates(candidateInfo)
      setPauseMarkers(pauses?.markers ?? [])
      setPauseSummary(null)
      if (!candidateInfo?.candidates.length) return
      void Promise.all(candidateInfo.candidates.map(async (candidate) => ({
        variant: candidate.variant,
        waveform: await api<Waveform>(`/api/projects/${projectId}/groups/${selectedGroup}/audio/candidate-${candidate.variant}/waveform`).catch(() => null),
      }))).then((entries) => {
        if (cancelled) return
        const values = new Map<number, Waveform>()
        for (const entry of entries) if (entry.waveform) values.set(entry.variant, entry.waveform)
        setCandidateWaveforms(values)
      })
    }).catch(() => undefined)
    return () => { cancelled = true }
  }, [projectId, selectedGroup, activeGroup?.candidate_count, activeGroup?.selection?.candidate, activeGroupAssetJobId])
  useEffect(() => {
    if (auditionMode === 'source') return
    const hasSelected = auditionMode === 'selected' && Boolean(activeGroup?.selection?.candidate)
    const hasCandidate = typeof auditionMode === 'number' && candidates?.candidates.some((candidate) => candidate.variant === auditionMode)
    if (!hasSelected && !hasCandidate) setAuditionMode('source')
  }, [activeGroup?.group, activeGroup?.selection?.candidate, auditionMode, candidates])
  useEffect(() => {
    if (auditionMode === 'source') {
      auditionAudioRef.current?.pause()
      return
    }
    if (isPlaying) syncAuditionAudio(playhead, true)
  }, [auditionMode, activeGroup?.group])
  useEffect(() => {
    if (!projectId || !jobs.some((job) => job.state === 'queued' || job.state === 'running')) return
    const timer = window.setInterval(() => refreshProjectData().catch(() => undefined), 1200)
    return () => window.clearInterval(timer)
  }, [projectId, jobs])
  useEffect(() => {
    if (!projectId || !selectedJobId) return
    let cancelled = false
    const loadLog = () => api<{ log: string }>(`/api/jobs/${selectedJobId}/log`).then((value) => {
      if (!cancelled) setJobLog(value.log)
    }).catch((error: Error) => {
      if (!cancelled) setJobLog(error.message)
    })
    void loadLog()
    if (!selectedJob || selectedJob.state === 'completed' || selectedJob.state === 'failed' || selectedJob.state === 'cancelled') return () => { cancelled = true }
    const timer = window.setInterval(() => { void loadLog() }, 900)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [projectId, selectedJobId, selectedJob?.state])
  useEffect(() => () => clearAuditionTimer(), [])
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      if (target && ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(target.tagName)) return
      if (event.key === ' ' || event.key.toLowerCase() === 'k') {
        event.preventDefault()
        void togglePlayback()
      } else if (event.key.toLowerCase() === 'j') {
        event.preventDefault()
        seek(playhead - 1)
      } else if (event.key.toLowerCase() === 'l') {
        event.preventDefault()
        seek(playhead + 1)
      } else if (event.key === '[') {
        event.preventDefault()
        navigateFiltered(-1)
      } else if (event.key === ']') {
        event.preventDefault()
        navigateFiltered(1)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [playhead, filteredGroups, selectedGroup, activeGroup, auditionMode, frameRate])

  async function saveTranslation(event: FormEvent) {
    event.preventDefault()
    if (!projectId || !selectedGroup) return
    setBusy('save')
    try {
      await api(`/api/projects/${projectId}/translation-review/${selectedGroup}`, {
        method: 'PUT', body: JSON.stringify({ lip_sync_text: draft, approved, translator_notes: notes }),
      })
      setMessage(`Group ${selectedGroup} saved. Apply translations before rendering.`)
      await refreshProjectData()
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function changeRole(event: FormEvent) {
    event.preventDefault()
    if (!projectId || !activeGroup || !roleDraft.trim()) return
    setBusy('role')
    try {
      const result = await api<{ role: string; previous_role: string; affected_groups: number[]; changed: boolean; next_step: string }>(`/api/projects/${projectId}/groups/${activeGroup.group}/role`, {
        method: 'PUT', body: JSON.stringify({ role: roleDraft }),
      })
      setRoleDraft(result.role)
      const affected = result.affected_groups.length ? ` Groups ${result.affected_groups.join(', ')} now need fresh takes.` : ''
      setMessage(result.changed ? `Character corrected: ${result.previous_role} → ${result.role}.${affected} ${result.next_step}` : result.next_step)
      await refreshProjectData()
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function queue(command: string, extra: Record<string, unknown> = {}) {
    if (!projectId) return
    setBusy(command)
    try {
      await api(`/api/projects/${projectId}/jobs`, { method: 'POST', body: JSON.stringify({ command, ...extra }) })
      setMessage(`${command} queued.`)
      await refreshProjectData()
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function cancelJob(job: Job) {
    setBusy(`cancel-${job.job_id}`)
    try {
      await api(`/api/jobs/${job.job_id}/cancel`, { method: 'POST' })
      setMessage(`${job.command_name} cancelled.`)
      await refreshProjectData()
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  function retryJob(job: Job) {
    const request = job.arguments
    if (!request?.command) return
    void queue(request.command, {
      groups: request.groups ?? [], roles: request.roles ?? [], variants: request.variants,
      force: request.force, strict: request.strict, confirm: request.confirm,
    })
  }

  async function chooseCandidate(variant: number) {
    if (!projectId || !selectedGroup) return
    setBusy(`candidate-${variant}`)
    try {
      await api(`/api/projects/${projectId}/groups/${selectedGroup}/candidate-override`, { method: 'PUT', body: JSON.stringify({ variant }) })
      setMessage(`Candidate ${variant} is marked for manual selection. Run select to build selected.wav.`)
      await refreshProjectData()
      setCandidates((current) => current ? { ...current, candidates: current.candidates.map((candidate) => ({ ...candidate, selected: candidate.variant === variant })) } : current)
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function savePauses() {
    if (!projectId || !selectedGroup) return
    setBusy('pauses')
    try {
      const summary = await api<PauseSummary>(`/api/projects/${projectId}/groups/${selectedGroup}/pauses`, { method: 'PUT', body: JSON.stringify({ markers: pauseMarkers }) })
      setPauseSummary(summary)
      setMessage('Natural pause plan saved. Render this group again to hear it.')
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  async function uploadRoleAudio(role: string, kind: 'reference' | 'emotion', file: File) {
    if (!projectId) return
    setBusy(`${role}-${kind}`)
    try {
      const body = new FormData()
      body.set('audio', file)
      const response = await fetch(`${apiBase}/api/projects/${projectId}/roles/${role}/audio/${kind}`, { method: 'POST', body })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail || `Role audio upload failed (${response.status})`)
      setRolesInfo(payload.roles)
      setMessage(`${role} ${kind} audio saved locally. Build that role's reference before rendering.`)
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return <main className="app-shell">
    <header className="topbar">
      <div><p className="eyebrow">LOCAL · SOURCE-PERFORMANCE DUBBING</p><h1>FSFilm AI Dub</h1></div>
      <div className="topbar-actions"><button className="secondary" onClick={() => setShowImport(true)}>Import dropped files</button><button className="secondary" onClick={() => refreshProjects().catch((error: Error) => setMessage(error.message))}>Refresh projects</button></div>
    </header>
    {message && <p className="message" role="status">{message}</p>}
    {showImport && <ImportWizard onClose={() => setShowImport(false)} onImported={(id) => { setShowImport(false); void refreshProjects(id); setMessage('Project imported. Run preflight, then Build project to begin review.') }} />}

    {projects.length === 0 ? <section className="empty-start"><h2>Start by dropping a short’s source assets</h2><p>Import the dialogue audio, source and target subtitles, script, and optional video. Everything stays local.</p><button onClick={() => setShowImport(true)}>Import a new project</button></section> : <>
      <section className="project-bar" aria-label="Project selection">
        <label>Project<select value={projectId} onChange={(event) => { setSelectedGroup(null); setPlayhead(0); setTimelineZoom(1); setTimelineStart(0); setProjectId(event.target.value) }}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        {activeProject && <div className="metrics"><span><b>{activeProject.counts.turns}</b> turns</span><span><b>{activeProject.counts.approved_translations}</b> approved</span><span><b>{activeProject.counts.rendered_turns}</b> rendered</span><span><b>{activeProject.counts.selected_turns}</b> selected</span><span className={activeProject.counts.qa_warning_turns ? 'metric-warning' : ''}><b>{activeProject.counts.qa_warning_turns ?? 0}</b> QA flags</span></div>}
      </section>
      {activeProject && <section className="readiness-card" aria-label="Project readiness">
        <div><p className="eyebrow">PROJECT READINESS</p><h2>{activeProject.stage === 'initialized' ? 'Build review data before editing' : missingReferences.length ? `${missingReferences.length} actor reference${missingReferences.length === 1 ? '' : 's'} still need attention` : activeProject.counts.qa_warning_turns ? `${activeProject.counts.qa_warning_turns} turn${activeProject.counts.qa_warning_turns === 1 ? '' : 's'} need QA review` : 'Ready for editorial review and delivery validation'}</h2><p>{activeProject.stage === 'initialized' ? 'Build creates the dialogue groups and review artifacts from the local source assets.' : 'Pipeline actions are queued locally, one GPU job at a time. Review job logs before retrying a failed action.'}</p></div>
        <div className="readiness-actions"><button className="secondary" type="button" disabled={busy !== null} onClick={() => queue('preflight')}>Run preflight</button>{activeProject.stage === 'initialized' ? <button type="button" disabled={busy !== null} onClick={() => queue('build')}>Build project</button> : <span className="rebuild-guard">Rebuild is disabled after review data exists.</span>}<button className="secondary" type="button" disabled={busy !== null || activeProject.stage !== 'ready'} onClick={() => queue('validate-translations', { strict: true })}>Validate translations</button></div>
      </section>}

      <section className="workspace">
        <aside className="group-list" aria-label="Dialogue groups"><div className="panel-title"><h2>Dialogue turns</h2><span>{filteredGroups.length}/{groups.length}</span></div><div className="group-tools"><input aria-label="Search dialogue turns" value={groupQuery} onChange={(event) => setGroupQuery(event.target.value)} placeholder="Search turn, character, text" /><select aria-label="Filter dialogue turns" value={groupFilter} onChange={(event) => setGroupFilter(event.target.value as GroupFilter)}><option value="all">All turns</option><option value="needs-review">Needs translation review</option><option value="qa-warning">QA warnings</option><option value="unselected">Rendered, unselected</option><option value="unrendered">Not rendered</option></select><div><button className="secondary" type="button" onClick={() => navigateFiltered(-1)} disabled={filteredGroups.length < 2}>Previous</button><button className="secondary" type="button" onClick={() => navigateFiltered(1)} disabled={filteredGroups.length < 2}>Next</button></div></div>{isInitialProjectLoad ? <p className="list-loading">Loading dialogue turns…</p> : filteredGroups.length ? filteredGroups.map((group) => <button className={`group-row ${group.group === selectedGroup ? 'selected' : ''}`} key={group.group} onClick={() => selectTurn(group)} title={group.qa_flags.join(' · ')}><span className="group-number">{String(group.group).padStart(2, '0')}</span><span className="role-ribbon" aria-hidden="true" style={{ background: roleColor(group.role) }} /><span className="group-main"><b>{group.role}</b><small>{timestamp(group.source_start)}–{timestamp(group.source_end)}</small></span><span className={`state state-${group.translation_state}`} aria-label={group.qa_flags.length ? `${group.qa_flags.length} QA warnings` : group.translation_state === 'approved' ? 'Translation approved' : 'Translation needs review'}>{group.qa_flags.length ? `!${group.qa_flags.length}` : group.translation_state === 'approved' ? '✓' : '!'}</span></button>) : <p className="list-loading">No turns match this filter.</p>}</aside>

        <section className="review-panel">{isInitialProjectLoad ? <p className="empty">Loading project review data…</p> : activeGroup ? <>
          <div className="turn-heading"><div><p className="eyebrow">TURN {String(activeGroup.group).padStart(2, '0')} · {activeGroup.role}</p><h2>{timecode(activeGroup.source_start, frameRate)}–{timecode(activeGroup.source_end, frameRate)}</h2><p className="time-detail">{timestamp(activeGroup.source_start)}–{timestamp(activeGroup.source_end)} · {frameRate.toFixed(frameRate % 1 ? 3 : 0)} fps</p></div><div className="turn-facts"><span>{activeGroup.target_word_budget} word budget</span><span>{activeGroup.candidate_count} candidates</span>{activeGroup.selection?.candidate && <span>selected C{activeGroup.selection.candidate}</span>}{activeGroup.qa_flags.map((flag) => <span className="qa-badge" key={flag}>{flag}</span>)}</div></div>
          <form className="role-editor" onSubmit={changeRole}>
            <label>Character / actor<select value={roleDraft} onChange={(event) => setRoleDraft(event.target.value)}>{availableRoles.map((role) => <option key={role} value={role}>{role}</option>)}</select></label>
            <button className="secondary" type="submit" disabled={busy !== null || roleDraft.trim().toUpperCase() === activeGroup.role}>Correct character</button>
            <span>Complete cast from the dialogue script. Updates matching lines and retires old takes for affected turn(s).</span>
          </form>
          <section className="picture-card" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const file = event.dataTransfer.files[0]; if (file) void uploadVideo(file) }}>
            {activeProject?.has_video ? <video ref={videoRef} muted preload="metadata" src={`${apiBase}/api/projects/${projectId}/media/video`} onLoadedMetadata={() => syncVideo(playhead, isPlaying)} /> : <div className="video-placeholder"><strong>Drop a picture reference here</strong><span>or <label className="inline-file">choose a video<input type="file" accept="video/*" onChange={(event) => event.target.files?.[0] && void uploadVideo(event.target.files[0])} /></label> to set <code>input.video</code>.</span></div>}
            <audio ref={sourceAudioRef} muted={auditionMode !== 'source'} preload="metadata" src={`${apiBase}/api/projects/${projectId}/media/audio`} onTimeUpdate={(event) => updatePlayhead(event.currentTarget.currentTime)} onPause={() => { clearAuditionTimer(); auditionAudioRef.current?.pause(); setIsPlaying(false) }} onPlay={() => setIsPlaying(true)} />
            {auditionAsset && <audio ref={auditionAudioRef} preload="auto" src={`${apiBase}/api/projects/${projectId}/groups/${activeGroup.group}/audio/${auditionAsset}`} />}
            <div className="audition-selector" role="group" aria-label="Synchronized audio audition"><span>Audition</span><button className={auditionMode === 'source' ? 'audition-active' : 'secondary'} type="button" onClick={() => setAudition('source')}>Source</button>{activeGroup.selection?.candidate && <button className={auditionMode === 'selected' ? 'audition-active' : 'secondary'} type="button" onClick={() => setAudition('selected')}>Selected</button>}{candidates?.candidates.map((candidate) => <button className={auditionMode === candidate.variant ? 'audition-active' : 'secondary'} type="button" key={candidate.variant} onClick={() => setAudition(candidate.variant)}>C{candidate.variant}</button>)}</div>
            <div className="transport"><button type="button" onClick={() => seek(playhead - 1 / frameRate)} className="secondary">◀ frame</button><button type="button" onClick={() => void togglePlayback()}>{isPlaying ? 'Pause' : `Play ${auditionMode === 'source' ? 'source' : auditionMode === 'selected' ? 'selected' : `C${auditionMode}`}`}</button><button type="button" onClick={() => seek(playhead + 1 / frameRate)} className="secondary">frame ▶</button><button className="secondary" type="button" onClick={() => seek(Math.max(0, activeGroup.source_start - preRoll))}>Loop turn</button><span className="current-time">{timecode(playhead, frameRate)}</span><label>fps <select value={frameRate} onChange={(event) => setFrameRate(Number(event.target.value))}><option value="23.976">23.976</option><option value="24">24</option><option value="25">25</option><option value="29.97">29.97</option><option value="30">30</option></select></label><label>speed <select value={playbackRate} onChange={(event) => setPlaybackRate(Number(event.target.value))}><option value="0.5">0.5×</option><option value="0.75">0.75×</option><option value="1">1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option></select></label><label><input type="checkbox" checked={loopTurn} onChange={(event) => setLoopTurn(event.target.checked)} /> loop</label><label>pre <input type="number" min="0" max="3" step="0.1" value={preRoll} onChange={(event) => setPreRoll(Number(event.target.value))} />s</label><label>post <input type="number" min="0" max="3" step="0.1" value={postRoll} onChange={(event) => setPostRoll(Number(event.target.value))} />s</label></div>
            {activeProject?.has_video && <div className="picture-delay" role="group" aria-label="Picture timing offset"><span>Picture timing</span><button className="secondary" type="button" disabled={busy !== null} onClick={() => void saveVideoDelay(videoDelay - 0.1)}>Earlier 0.1 s</button><input aria-label="Picture delay" type="range" min="-2" max="2" step="0.01" disabled={busy !== null} value={videoDelay} onChange={(event) => setVideoDelay(Number(event.target.value))} onPointerUp={(event) => void saveVideoDelay(Number(event.currentTarget.value))} onKeyUp={(event) => void saveVideoDelay(Number(event.currentTarget.value))} /><output>{pictureDelayLabel(videoDelay)}</output><button className="secondary" type="button" disabled={busy !== null} onClick={() => void saveVideoDelay(videoDelay + 0.1)}>Later 0.1 s</button><button className="secondary" type="button" disabled={busy !== null || Math.abs(videoDelay) < 0.001} onClick={() => void saveVideoDelay(0)}>Reset</button></div>}
            <p className="transport-help">Space/K play or pause · J/L step one second · [ / ] previous or next filtered turn. Target takes stay aligned to picture; source is muted while they play.</p>
          </section>
          {waveform && <CharacterTimeline waveform={waveform} groups={groups} selectedGroup={selectedGroup} playhead={playhead} zoom={timelineZoom} viewStart={timelineStart} onZoomChange={setTimelineZoom} onViewStartChange={setTimelineStart} onSeek={seek} onSelect={(group) => { const fullGroup = groups.find((item) => item.group === group.group); if (fullGroup) selectTurn(fullGroup) }} />}
          {candidates?.candidates.length ? <section className="candidate-panel">
            <div className="panel-title"><div><h3>Candidate audition</h3><span>Click a waveform to seek its preview, then retain a deliberate manual or scored selection.</span></div><button className="secondary" type="button" disabled={busy !== null} onClick={() => queue('select', { groups: [activeGroup.group] })}>Apply manual choice</button></div>
            <div className="candidate-grid">{candidates.candidates.map((candidate) => <article key={candidate.variant} className={`candidate-card ${candidate.selected ? 'candidate-selected' : ''} ${candidate.word_recall !== null && candidate.word_recall < 0.82 || candidate.ending_present === false ? 'candidate-warning' : ''}`}>
              <div><b>Candidate {candidate.variant}</b>{candidate.selected && <span className="selected-badge">selected</span>}</div>
              <CandidateWaveform waveform={candidateWaveforms.get(candidate.variant)} onSeek={(ratio) => seekCandidatePreview(candidate.variant, ratio, candidate.duration ?? candidateWaveforms.get(candidate.variant)?.duration ?? 0)} />
              <audio ref={(audio) => { if (audio) candidateAudioRefs.current.set(candidate.variant, audio); else candidateAudioRefs.current.delete(candidate.variant) }} controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/groups/${activeGroup.group}/audio/candidate-${candidate.variant}`} />
              <dl><div><dt>Duration</dt><dd>{candidate.duration?.toFixed(2) ?? '—'} s</dd></div><div><dt>Recall</dt><dd>{candidate.word_recall ?? 'not scored'}</dd></div><div><dt>Overrun</dt><dd>{candidate.overrun?.toFixed(2) ?? '—'} s</dd></div><div><dt>Ending</dt><dd>{candidate.ending_present === null ? '—' : candidate.ending_present ? 'present' : 'missing'}</dd></div><div><dt>Score</dt><dd>{candidate.score?.toFixed(2) ?? '—'}</dd></div></dl>
              {candidate.transcript && <p className="candidate-transcript">{candidate.transcript}</p>}
              <div className="candidate-actions"><button className="secondary" type="button" onClick={() => setAudition(candidate.variant)}>Audition in picture</button><button className="secondary" type="button" disabled={busy !== null} onClick={() => void chooseCandidate(candidate.variant)}>{busy === `candidate-${candidate.variant}` ? 'Choosing…' : `Use candidate ${candidate.variant}`}</button></div>
            </article>)}</div>
          </section> : null}
          <div className="text-compare"><article><h3>{sourceLanguage} source</h3><p>{activeGroup.source_text}</p></article><article><h3>Legacy {targetLanguage} subtitle</h3><p>{activeGroup.legacy_target_text}</p></article></div>
          <form className="translation-editor" onSubmit={saveTranslation}><label>Reviewed {targetLanguage} lip-sync dialogue<textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} required /></label><div className="editor-footer"><label className="approval"><input type="checkbox" checked={approved} onChange={(event) => setApproved(event.target.checked)} /> Approved after bilingual/creative review</label><label className="notes">Notes<input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Pronunciation, intent, or review note" /></label><button type="submit" disabled={busy !== null}>{busy === 'save' ? 'Saving…' : 'Save review'}</button></div></form>
          <section className="pause-panel"><div className="panel-title"><div><h3>Natural pause plan</h3><span>Stored outside subtitle text; render the group again after saving.</span></div><button className="secondary" type="button" disabled={pauseWords.length === 0} onClick={() => setPauseMarkers((markers) => [...markers, { after_word: Math.min(Math.max(1, pauseWords.length), 2), duration_ms: 350, mode: 'natural' }])}>Add pause</button></div>{pauseMarkers.length ? <div className="pause-markers">{pauseMarkers.map((marker, index) => <div className="pause-marker" key={`${marker.after_word}-${index}`}><label>After<select value={marker.after_word} onChange={(event) => setPauseMarkers((markers) => markers.map((item, itemIndex) => itemIndex === index ? { ...item, after_word: Number(event.target.value) } : item))}>{pauseWords.map((word, wordIndex) => <option key={`${word}-${wordIndex}`} value={wordIndex + 1}>{wordIndex + 1}. {word}</option>)}</select></label><label>Pause ms<input type="number" min="50" max="5000" step="50" value={marker.duration_ms} onChange={(event) => setPauseMarkers((markers) => markers.map((item, itemIndex) => itemIndex === index ? { ...item, duration_ms: Number(event.target.value) } : item))} /></label><button className="secondary" type="button" onClick={() => setPauseMarkers((markers) => markers.filter((_, itemIndex) => itemIndex !== index))}>Remove</button></div>)}</div> : <p className="muted">No pause markers. Use punctuation in the translation for ordinary phrasing, or add a deliberate natural pause here.</p>}<div className="pause-footer"><button type="button" disabled={busy !== null} onClick={() => void savePauses()}>{busy === 'pauses' ? 'Saving…' : 'Save natural pauses'}</button>{pauseSummary && <span className={pauseSummary.remaining_seconds < 0 ? 'budget-negative' : 'budget-ok'}>Picture {pauseSummary.available_seconds}s · speech ≈ {pauseSummary.estimated_speech_seconds}s · pauses {pauseSummary.requested_pause_seconds}s · margin {pauseSummary.remaining_seconds}s</span>}</div><p className="muted">Exact hard silence insertion is deliberately not enabled yet: it needs word-aligned QA so it cannot shift later dialogue unnoticed.</p></section>
          <section className="actions" aria-label="Targeted pipeline actions"><button className="secondary" type="button" disabled={busy !== null} onClick={() => queue('apply-translations')}>Apply approved text</button><button type="button" disabled={busy !== null || activeGroup.translation_state !== 'approved'} title={activeGroup.translation_state === 'approved' ? '' : 'Approve the reviewed translation before rendering'} onClick={() => queue('render', { groups: [activeGroup.group], variants: 3, force: true })}>Render 3 fresh takes</button><button className="secondary" type="button" disabled={busy !== null || activeGroup.candidate_count === 0} onClick={() => queue('select', { groups: [activeGroup.group] })}>Select best take</button><button className="secondary" type="button" disabled={busy !== null} onClick={() => queue('refine-timing')}>Propose timing</button><button className="secondary" type="button" disabled={busy !== null} onClick={() => queue('apply-timing')}>Apply timing</button></section>
        </> : activeProject?.stage === 'initialized' ? <p className="empty">Run preflight, then Build project to create dialogue turns and review artifacts.</p> : <p className="empty">No dialogue turn is selected.</p>}</section>

        <aside className="review-sidebar">
          <section><div className="panel-title"><h2>Cast lanes</h2><span>{roles.length}</span></div><div className="role-chips">{roles.map((role) => <span key={role}><i style={{ background: roleColor(role) }} />{role}</span>)}</div></section>
          <section className="reference-panel"><div className="panel-title"><h2>Actor references</h2><span>{rolesInfo.length}</span></div>{rolesInfo.map((role) => <article className="role-reference" key={role.role}><div><b>{role.role}</b><span>{role.generated_reference ? 'prepared' : role.configured_reference ? 'source supplied' : 'missing'}</span></div>{(role.generated_reference || role.configured_reference) && <audio controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/roles/${role.role}/audio/reference`} />}<div className="reference-actions"><label className="inline-file">Reference<input type="file" accept="audio/*" onChange={(event) => event.target.files?.[0] && void uploadRoleAudio(role.role, 'reference', event.target.files[0])} /></label><label className="inline-file">Emotion<input type="file" accept="audio/*" onChange={(event) => event.target.files?.[0] && void uploadRoleAudio(role.role, 'emotion', event.target.files[0])} /></label>{role.configured_reference && !role.generated_reference && <button className="secondary" disabled={busy !== null} onClick={() => queue('make-references', { roles: [role.role], force: true })}>Prepare</button>}</div></article>)}</section>
          <section className="translation-qc-panel"><div className="panel-title"><h2>Translation QC</h2><span>{translationQc.length ? `${translationQcIssues.length} issues` : 'not run'}</span></div>{translationQc.length === 0 ? <p className="muted">Run Validate translations to see the latest word-budget and approval checks.</p> : translationQcIssues.length === 0 ? <p className="muted">All {translationQc.length} translations passed the latest QC run.</p> : <div className="translation-qc-list">{translationQcIssues.map((row) => <button className={`translation-qc-row ${Number(row.group) === selectedGroup ? 'selected' : ''}`} type="button" key={row.group} onClick={() => { const group = groups.find((item) => item.group === Number(row.group)); if (group) selectTurn(group) }}><b>{String(row.group).padStart(2, '0')} · {row.role}</b><span>{row.word_count}/{row.word_budget} words</span><small>{row.issues || 'translation not approved'}</small></button>)}</div>}</section>
          <section className="delivery-panel"><div className="panel-title"><h2>Delivery</h2><span>explicit</span></div><p>{deliveryValidationPassed ? 'Delivery validation passed. Assembly can now create the output files you reviewed.' : 'Validate the complete project before creating output files. Assembly stays locked until validation passes and you confirm it.'}</p><div className="delivery-actions"><button className="secondary" type="button" disabled={busy !== null || activeProject?.stage !== 'ready'} onClick={() => queue('validate', { strict: true, confirm: true })}>Validate delivery</button><label><input type="checkbox" checked={deliveryConfirmed} onChange={(event) => setDeliveryConfirmed(event.target.checked)} /> I reviewed the selected takes and want to assemble delivery files.</label><button type="button" disabled={busy !== null || !deliveryConfirmed || !deliveryValidationPassed || activeProject?.stage !== 'ready'} onClick={() => queue('assemble', { confirm: true })}>Assemble delivery</button></div>{deliveryAssets.length ? <div className="delivery-files"><h3>Delivery files</h3>{deliveryAssets.map((asset) => <article key={asset.name}><div><a href={`${apiBase}/api/projects/${projectId}/delivery-assets/${encodeURIComponent(asset.name)}`} download>{asset.name}</a><span>{asset.kind} · {(asset.size / 1024 / 1024).toFixed(1)} MB</span></div>{asset.kind === 'audio' && <audio controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/delivery-assets/${encodeURIComponent(asset.name)}`} />}</article>)}</div> : <p className="delivery-empty">Assembled audio and subtitle files will appear here for final preview and download.</p>}</section>
          <section><div className="panel-title"><h2>Jobs</h2><span>{jobs.length}</span></div><div className="jobs">{jobs.slice(0, 8).map((job) => <button type="button" key={job.job_id} className={`job ${job.job_id === selectedJobId ? 'job-selected' : ''}`} onClick={() => setSelectedJobId(job.job_id)}><span className={`job-state ${job.state}`}>{job.state}</span><b>{job.command_name}</b><small>{job.error || job.created_at}</small></button>)}</div>{selectedJob && <div className="job-detail"><div><b>{selectedJob.command_name}</b><span className={`job-state ${selectedJob.state}`}>{selectedJob.state}</span></div><small>{selectedJob.error || selectedJob.return_code === null || selectedJob.return_code === undefined ? selectedJob.created_at : `exit ${selectedJob.return_code}`}</small><pre aria-label={`${selectedJob.command_name} log`}>{jobLog || 'Waiting for job output…'}</pre><div>{(selectedJob.state === 'queued' || selectedJob.state === 'running') && <button className="secondary" type="button" disabled={busy !== null} onClick={() => void cancelJob(selectedJob)}>{busy === `cancel-${selectedJob.job_id}` ? 'Cancelling…' : 'Cancel job'}</button>}{(selectedJob.state === 'failed' || selectedJob.state === 'cancelled') && selectedJob.arguments?.command && <button className="secondary" type="button" disabled={busy !== null} onClick={() => retryJob(selectedJob)}>Retry action</button>}</div></div>}</section>
        </aside>
      </section>
    </>}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
