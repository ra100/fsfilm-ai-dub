import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ImportWizard } from './import-wizard'
import { CharacterTimeline, roleColor, type Waveform } from './timeline'
import './styles.css'

type Counts = { turns: number; approved_translations: number; rendered_turns: number; selected_turns: number }
type Project = { id: string; name: string; config_path: string; has_video: boolean; stage: 'initialized' | 'ready'; counts: Counts; review_files: Record<string, boolean> }
type Group = {
  group: number; role: string; source_start: number; source_end: number; source_text: string; legacy_target_text: string
  lip_sync_text: string; target_word_budget: number; translation_state: string; timing_state: string; role_confidence: number
  candidate_count: number; selection: { candidate?: number; review?: string[] } | null
}
type TranslationRow = { group: string; role: string; lip_sync_text: string; approved: string; translator_notes: string; [key: string]: string }
type Job = { job_id: string; command_name: string; state: string; created_at: string; started_at: string | null; finished_at: string | null; error: string | null }
type Candidate = { variant: number; duration: number | null; seed: number | null; word_recall: number | null; ending_present: boolean | null; available_duration: number | null; overrun: number | null; score: number | null; transcript: string | null; selected: boolean }
type CandidateInfo = { group: number; role: string; text: string; candidates: Candidate[]; selection: { candidate?: number; review?: string[] } | null }
type RoleInfo = { role: string; configured_reference: boolean; configured_emotion: boolean; generated_reference: boolean }
type PauseMarker = { after_word: number; duration_ms: number; mode: 'natural' | 'hard' }
type PauseSummary = { group: number; markers: PauseMarker[]; estimated_speech_seconds: number; requested_pause_seconds: number; available_seconds: number; remaining_seconds: number }
type ApiError = Error & { detail?: string }

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

function App() {
  const videoRef = useRef<HTMLVideoElement>(null)
  const sourceAudioRef = useRef<HTMLAudioElement>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [groups, setGroups] = useState<Group[]>([])
  const [translations, setTranslations] = useState<Map<number, TranslationRow>>(new Map())
  const [jobs, setJobs] = useState<Job[]>([])
  const [waveform, setWaveform] = useState<Waveform | null>(null)
  const [rolesInfo, setRolesInfo] = useState<RoleInfo[]>([])
  const [candidates, setCandidates] = useState<CandidateInfo | null>(null)
  const [pauseMarkers, setPauseMarkers] = useState<PauseMarker[]>([])
  const [pauseSummary, setPauseSummary] = useState<PauseSummary | null>(null)
  const [selectedGroup, setSelectedGroup] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
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

  const activeProject = projects.find((project) => project.id === projectId) ?? null
  const activeGroup = groups.find((group) => group.group === selectedGroup) ?? null
  const roles = useMemo(() => [...new Set(groups.map((group) => group.role))].sort(), [groups])
  const pauseWords = useMemo(() => draft.match(/[\w']+/g) ?? [], [draft])
  const duration = waveform?.duration ?? 0

  async function refreshProjects(preferredProjectId = '') {
    const values = await api<Project[]>('/api/projects')
    setProjects(values)
    setProjectId((current) => preferredProjectId || current || values[0]?.id || '')
  }

  async function refreshProjectData(id = projectId) {
    if (!id) return
    const [nextGroups, review, nextJobs, nextWaveform, nextRoles] = await Promise.all([
      api<Group[]>(`/api/projects/${id}/groups`),
      api<{ rows: TranslationRow[] }>(`/api/projects/${id}/translation-review`).catch(() => ({ rows: [] })),
      api<Job[]>(`/api/projects/${id}/jobs`),
      api<Waveform>(`/api/projects/${id}/waveform`).catch(() => null),
      api<RoleInfo[]>(`/api/projects/${id}/roles`).catch(() => []),
    ])
    setGroups(nextGroups)
    setTranslations(new Map(review.rows.map((row) => [Number(row.group), row])))
    setJobs(nextJobs)
    setWaveform(nextWaveform)
    setRolesInfo(nextRoles)
    setSelectedGroup((current) => current ?? nextGroups[0]?.group ?? null)
  }

  function seek(seconds: number) {
    const next = Math.max(0, Math.min(duration || Number.POSITIVE_INFINITY, seconds))
    setPlayhead(next)
    for (const media of [sourceAudioRef.current, videoRef.current]) {
      if (media && Number.isFinite(next) && Math.abs(media.currentTime - next) > 0.03) media.currentTime = next
    }
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
        if (videoRef.current) await videoRef.current.play()
        setIsPlaying(true)
      } catch (error) {
        setMessage(`Playback failed: ${(error as Error).message}`)
      }
    } else {
      audio.pause()
      videoRef.current?.pause()
      setIsPlaying(false)
    }
  }

  function updatePlayhead(seconds: number) {
    setPlayhead(seconds)
    if (videoRef.current && Math.abs(videoRef.current.currentTime - seconds) > 0.13) videoRef.current.currentTime = seconds
    if (loopTurn && activeGroup && seconds >= activeGroup.source_end + postRoll) {
      const restart = Math.max(0, activeGroup.source_start - preRoll)
      seek(restart)
      void sourceAudioRef.current?.play()
      void videoRef.current?.play()
    }
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

  useEffect(() => { refreshProjects().catch((error: Error) => setMessage(error.message)) }, [])
  useEffect(() => { refreshProjectData().catch((error: Error) => setMessage(error.message)) }, [projectId])
  useEffect(() => {
    const row = selectedGroup ? translations.get(selectedGroup) : undefined
    const group = groups.find((item) => item.group === selectedGroup)
    setDraft(row?.lip_sync_text ?? group?.lip_sync_text ?? '')
    setNotes(row?.translator_notes ?? '')
    setApproved(row?.approved === 'yes')
  }, [selectedGroup, translations, groups])
  useEffect(() => {
    if (!projectId || !selectedGroup) {
      setCandidates(null)
      setPauseMarkers([])
      setPauseSummary(null)
      return
    }
    Promise.all([
      api<CandidateInfo>(`/api/projects/${projectId}/groups/${selectedGroup}/candidates`).catch(() => null),
      api<{ group: number; markers: PauseMarker[] }>(`/api/projects/${projectId}/groups/${selectedGroup}/pauses`).catch(() => null),
    ]).then(([candidateInfo, pauses]) => {
      setCandidates(candidateInfo)
      setPauseMarkers(pauses?.markers ?? [])
      setPauseSummary(null)
    }).catch(() => undefined)
  }, [projectId, selectedGroup])
  useEffect(() => {
    if (!projectId || !jobs.some((job) => job.state === 'queued' || job.state === 'running')) return
    const timer = window.setInterval(() => refreshProjectData().catch(() => undefined), 1200)
    return () => window.clearInterval(timer)
  }, [projectId, jobs])

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
    {showImport && <ImportWizard onClose={() => setShowImport(false)} onImported={(id) => { setShowImport(false); void refreshProjects(id); setMessage('Project imported. Run preflight and build when you are ready to begin review.') }} />}

    {projects.length === 0 ? <section className="empty-start"><h2>Start by dropping a short’s source assets</h2><p>Import the dialogue audio, source and target subtitles, script, and optional video. Everything stays local.</p><button onClick={() => setShowImport(true)}>Import a new project</button></section> : <>
      <section className="project-bar" aria-label="Project selection">
        <label>Project<select value={projectId} onChange={(event) => { setSelectedGroup(null); setPlayhead(0); setProjectId(event.target.value) }}>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
        {activeProject && <div className="metrics"><span><b>{activeProject.counts.turns}</b> turns</span><span><b>{activeProject.counts.approved_translations}</b> approved</span><span><b>{activeProject.counts.rendered_turns}</b> rendered</span><span><b>{activeProject.counts.selected_turns}</b> selected</span></div>}
      </section>

      <section className="workspace">
        <aside className="group-list" aria-label="Dialogue groups"><div className="panel-title"><h2>Dialogue turns</h2><span>{groups.length}</span></div>{groups.map((group) => <button className={`group-row ${group.group === selectedGroup ? 'selected' : ''}`} key={group.group} onClick={() => selectTurn(group)}><span className="group-number">{String(group.group).padStart(2, '0')}</span><span className="role-dot" style={{ background: roleColor(group.role) }} /><span className="group-main"><b>{group.role}</b><small>{timestamp(group.source_start)}–{timestamp(group.source_end)}</small></span><span className={`state state-${group.translation_state}`}>{group.translation_state === 'approved' ? '✓' : '!'}</span></button>)}</aside>

        <section className="review-panel">{activeGroup ? <>
          <div className="turn-heading"><div><p className="eyebrow">TURN {String(activeGroup.group).padStart(2, '0')} · {activeGroup.role}</p><h2>{timestamp(activeGroup.source_start)}–{timestamp(activeGroup.source_end)}</h2></div><div className="turn-facts"><span>{activeGroup.target_word_budget} word budget</span><span>{activeGroup.candidate_count} candidates</span>{activeGroup.selection?.candidate && <span>selected C{activeGroup.selection.candidate}</span>}</div></div>
          <section className="picture-card" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); const file = event.dataTransfer.files[0]; if (file) void uploadVideo(file) }}>
            {activeProject?.has_video ? <video ref={videoRef} muted preload="metadata" src={`${apiBase}/api/projects/${projectId}/media/video`} /> : <div className="video-placeholder"><strong>Drop a picture reference here</strong><span>or <label className="inline-file">choose a video<input type="file" accept="video/*" onChange={(event) => event.target.files?.[0] && void uploadVideo(event.target.files[0])} /></label> to set <code>input.video</code>.</span></div>}
            <audio ref={sourceAudioRef} preload="metadata" src={`${apiBase}/api/projects/${projectId}/media/audio`} onTimeUpdate={(event) => updatePlayhead(event.currentTarget.currentTime)} onPause={() => setIsPlaying(false)} onPlay={() => setIsPlaying(true)} />
            <div className="transport"><button onClick={() => seek(playhead - 1 / 24)} className="secondary">◀ frame</button><button onClick={() => void togglePlayback()}>{isPlaying ? 'Pause' : 'Play source'}</button><button onClick={() => seek(playhead + 1 / 24)} className="secondary">frame ▶</button><button className="secondary" onClick={() => seek(Math.max(0, activeGroup.source_start - preRoll))}>Loop turn</button><label><input type="checkbox" checked={loopTurn} onChange={(event) => setLoopTurn(event.target.checked)} /> loop</label><label>pre <input type="number" min="0" max="3" step="0.1" value={preRoll} onChange={(event) => setPreRoll(Number(event.target.value))} />s</label><label>post <input type="number" min="0" max="3" step="0.1" value={postRoll} onChange={(event) => setPostRoll(Number(event.target.value))} />s</label></div>
          </section>
          {waveform && <CharacterTimeline waveform={waveform} groups={groups} selectedGroup={selectedGroup} playhead={playhead} onSeek={seek} onSelect={(group) => { const fullGroup = groups.find((item) => item.group === group.group); if (fullGroup) selectTurn(fullGroup) }} />}
          {candidates?.candidates.length ? <section className="candidate-panel"><div className="panel-title"><div><h3>Candidate audition</h3><span>All raw takes are retained; choose one manually or let the selector score them.</span></div><button className="secondary" disabled={busy !== null} onClick={() => queue('select', { groups: [activeGroup.group] })}>Apply manual choice</button></div><div className="candidate-grid">{candidates.candidates.map((candidate) => <article key={candidate.variant} className={`candidate-card ${candidate.selected ? 'candidate-selected' : ''}`}><div><b>Candidate {candidate.variant}</b>{candidate.selected && <span className="selected-badge">selected</span>}</div><audio controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/groups/${activeGroup.group}/audio/candidate-${candidate.variant}`} /><dl><div><dt>Duration</dt><dd>{candidate.duration?.toFixed(2) ?? '—'} s</dd></div><div><dt>Recall</dt><dd>{candidate.word_recall ?? 'not scored'}</dd></div><div><dt>Overrun</dt><dd>{candidate.overrun?.toFixed(2) ?? '—'} s</dd></div></dl>{candidate.transcript && <p className="candidate-transcript">{candidate.transcript}</p>}<button className="secondary" disabled={busy !== null} onClick={() => void chooseCandidate(candidate.variant)}>{busy === `candidate-${candidate.variant}` ? 'Choosing…' : `Use candidate ${candidate.variant}`}</button></article>)}</div></section> : null}
          <div className="text-compare"><article><h3>Czech source</h3><p>{activeGroup.source_text}</p></article><article><h3>Legacy English</h3><p>{activeGroup.legacy_target_text}</p></article></div>
          <form className="translation-editor" onSubmit={saveTranslation}><label>Reviewed lip-sync English<textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} required /></label><div className="editor-footer"><label className="approval"><input type="checkbox" checked={approved} onChange={(event) => setApproved(event.target.checked)} /> Approved after bilingual/creative review</label><label className="notes">Notes<input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Pronunciation, intent, or review note" /></label><button type="submit" disabled={busy !== null}>{busy === 'save' ? 'Saving…' : 'Save review'}</button></div></form>
          <section className="pause-panel"><div className="panel-title"><div><h3>Natural pause plan</h3><span>Stored outside subtitle text; render the group again after saving.</span></div><button className="secondary" type="button" disabled={pauseWords.length === 0} onClick={() => setPauseMarkers((markers) => [...markers, { after_word: Math.min(Math.max(1, pauseWords.length), 2), duration_ms: 350, mode: 'natural' }])}>Add pause</button></div>{pauseMarkers.length ? <div className="pause-markers">{pauseMarkers.map((marker, index) => <div className="pause-marker" key={`${marker.after_word}-${index}`}><label>After<select value={marker.after_word} onChange={(event) => setPauseMarkers((markers) => markers.map((item, itemIndex) => itemIndex === index ? { ...item, after_word: Number(event.target.value) } : item))}>{pauseWords.map((word, wordIndex) => <option key={`${word}-${wordIndex}`} value={wordIndex + 1}>{wordIndex + 1}. {word}</option>)}</select></label><label>Pause ms<input type="number" min="50" max="5000" step="50" value={marker.duration_ms} onChange={(event) => setPauseMarkers((markers) => markers.map((item, itemIndex) => itemIndex === index ? { ...item, duration_ms: Number(event.target.value) } : item))} /></label><button className="secondary" type="button" onClick={() => setPauseMarkers((markers) => markers.filter((_, itemIndex) => itemIndex !== index))}>Remove</button></div>)}</div> : <p className="muted">No pause markers. Use punctuation in the translation for ordinary phrasing, or add a deliberate natural pause here.</p>}<div className="pause-footer"><button type="button" disabled={busy !== null} onClick={() => void savePauses()}>{busy === 'pauses' ? 'Saving…' : 'Save natural pauses'}</button>{pauseSummary && <span className={pauseSummary.remaining_seconds < 0 ? 'budget-negative' : 'budget-ok'}>Picture {pauseSummary.available_seconds}s · speech ≈ {pauseSummary.estimated_speech_seconds}s · pauses {pauseSummary.requested_pause_seconds}s · margin {pauseSummary.remaining_seconds}s</span>}</div><p className="muted">Exact hard silence insertion is deliberately not enabled yet: it needs word-aligned QA so it cannot shift later dialogue unnoticed.</p></section>
          <section className="actions" aria-label="Targeted pipeline actions"><button className="secondary" disabled={busy !== null} onClick={() => queue('apply-translations')}>Apply approved text</button><button disabled={busy !== null} onClick={() => queue('render', { groups: [activeGroup.group], variants: 3, force: true })}>Render 3 fresh takes</button><button className="secondary" disabled={busy !== null} onClick={() => queue('select', { groups: [activeGroup.group] })}>Select best take</button></section>
        </> : <p className="empty">Run build after import, then choose a dialogue turn.</p>}</section>

        <aside className="review-sidebar">
          <section><div className="panel-title"><h2>Cast lanes</h2><span>{roles.length}</span></div><div className="role-chips">{roles.map((role) => <span key={role}><i style={{ background: roleColor(role) }} />{role}</span>)}</div></section>
          <section className="reference-panel"><div className="panel-title"><h2>Actor references</h2><span>{rolesInfo.length}</span></div>{rolesInfo.map((role) => <article className="role-reference" key={role.role}><div><b>{role.role}</b><span>{role.generated_reference ? 'prepared' : role.configured_reference ? 'source supplied' : 'missing'}</span></div>{(role.generated_reference || role.configured_reference) && <audio controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/roles/${role.role}/audio/reference`} />}<div className="reference-actions"><label className="inline-file">Reference<input type="file" accept="audio/*" onChange={(event) => event.target.files?.[0] && void uploadRoleAudio(role.role, 'reference', event.target.files[0])} /></label><label className="inline-file">Emotion<input type="file" accept="audio/*" onChange={(event) => event.target.files?.[0] && void uploadRoleAudio(role.role, 'emotion', event.target.files[0])} /></label>{role.configured_reference && !role.generated_reference && <button className="secondary" disabled={busy !== null} onClick={() => queue('make-references', { roles: [role.role], force: true })}>Prepare</button>}</div></article>)}</section>
          <section><div className="panel-title"><h2>Jobs</h2><span>{jobs.length}</span></div><div className="jobs">{jobs.slice(0, 8).map((job) => <div key={job.job_id} className="job"><span className={`job-state ${job.state}`}>{job.state}</span><b>{job.command_name}</b><small>{job.error || job.created_at}</small></div>)}</div></section>
        </aside>
      </section>
    </>}
  </main>
}

createRoot(document.getElementById('root')!).render(<App />)
