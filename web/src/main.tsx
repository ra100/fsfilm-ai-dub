import { FormEvent, useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './styles.css'

type Counts = {
  turns: number
  approved_translations: number
  rendered_turns: number
  selected_turns: number
}

type Project = {
  id: string
  name: string
  config_path: string
  has_video: boolean
  stage: 'initialized' | 'ready'
  counts: Counts
  review_files: Record<string, boolean>
}

type Group = {
  group: number
  role: string
  source_start: number
  source_end: number
  source_text: string
  legacy_target_text: string
  lip_sync_text: string
  target_word_budget: number
  translation_state: string
  timing_state: string
  role_confidence: number
  candidate_count: number
  selection: { candidate?: number; review?: string[] } | null
}

type TranslationRow = {
  group: string
  role: string
  lip_sync_text: string
  approved: string
  translator_notes: string
  [key: string]: string
}

type Job = {
  job_id: string
  command_name: string
  state: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

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
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState<string>('')
  const [groups, setGroups] = useState<Group[]>([])
  const [translations, setTranslations] = useState<Map<number, TranslationRow>>(new Map())
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedGroup, setSelectedGroup] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  const [notes, setNotes] = useState('')
  const [approved, setApproved] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState('')

  const activeProject = projects.find((project) => project.id === projectId) ?? null
  const activeGroup = groups.find((group) => group.group === selectedGroup) ?? null
  const roles = useMemo(() => [...new Set(groups.map((group) => group.role))].sort(), [groups])

  async function refreshProjects() {
    const values = await api<Project[]>('/api/projects')
    setProjects(values)
    setProjectId((current) => current || values[0]?.id || '')
  }

  async function refreshProjectData(id = projectId) {
    if (!id) return
    const [nextGroups, review, nextJobs] = await Promise.all([
      api<Group[]>(`/api/projects/${id}/groups`),
      api<{ rows: TranslationRow[] }>(`/api/projects/${id}/translation-review`).catch(() => ({ rows: [] })),
      api<Job[]>(`/api/projects/${id}/jobs`),
    ])
    setGroups(nextGroups)
    setTranslations(new Map(review.rows.map((row) => [Number(row.group), row])))
    setJobs(nextJobs)
    setSelectedGroup((current) => current ?? nextGroups[0]?.group ?? null)
  }

  useEffect(() => {
    refreshProjects().catch((error: Error) => setMessage(error.message))
  }, [])

  useEffect(() => {
    refreshProjectData().catch((error: Error) => setMessage(error.message))
  }, [projectId])

  useEffect(() => {
    const row = selectedGroup ? translations.get(selectedGroup) : undefined
    const group = groups.find((item) => item.group === selectedGroup)
    setDraft(row?.lip_sync_text ?? group?.lip_sync_text ?? '')
    setNotes(row?.translator_notes ?? '')
    setApproved(row?.approved === 'yes')
  }, [selectedGroup, translations, groups])

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
        method: 'PUT',
        body: JSON.stringify({ lip_sync_text: draft, approved, translator_notes: notes }),
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
      await api(`/api/projects/${projectId}/jobs`, {
        method: 'POST',
        body: JSON.stringify({ command, ...extra }),
      })
      setMessage(`${command} queued.`)
      await refreshProjectData()
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">LOCAL · SOURCE-PERFORMANCE DUBBING</p>
          <h1>FSFilm AI Dub</h1>
        </div>
        <button className="secondary" onClick={() => refreshProjects().catch((error: Error) => setMessage(error.message))}>Refresh projects</button>
      </header>

      {message && <p className="message" role="status">{message}</p>}

      <section className="project-bar" aria-label="Project selection">
        <label>
          Project
          <select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </label>
        {activeProject && <div className="metrics">
          <span><b>{activeProject.counts.turns}</b> turns</span>
          <span><b>{activeProject.counts.approved_translations}</b> approved</span>
          <span><b>{activeProject.counts.rendered_turns}</b> rendered</span>
          <span><b>{activeProject.counts.selected_turns}</b> selected</span>
        </div>}
      </section>

      <section className="workspace">
        <aside className="group-list" aria-label="Dialogue groups">
          <div className="panel-title"><h2>Dialogue turns</h2><span>{groups.length}</span></div>
          {groups.map((group) => <button
            className={`group-row ${group.group === selectedGroup ? 'selected' : ''}`}
            key={group.group}
            onClick={() => setSelectedGroup(group.group)}
          >
            <span className="group-number">{String(group.group).padStart(2, '0')}</span>
            <span className="group-main"><b>{group.role}</b><small>{timestamp(group.source_start)}–{timestamp(group.source_end)}</small></span>
            <span className={`state state-${group.translation_state}`}>{group.translation_state === 'approved' ? '✓' : '!'}</span>
          </button>)}
        </aside>

        <section className="review-panel">
          {activeGroup ? <>
            <div className="turn-heading">
              <div><p className="eyebrow">TURN {String(activeGroup.group).padStart(2, '0')} · {activeGroup.role}</p><h2>{timestamp(activeGroup.source_start)}–{timestamp(activeGroup.source_end)}</h2></div>
              <div className="turn-facts"><span>{activeGroup.target_word_budget} word budget</span><span>{activeGroup.candidate_count} candidates</span>{activeGroup.selection?.candidate && <span>selected C{activeGroup.selection.candidate}</span>}</div>
            </div>

            <section className="picture-card">
              {activeProject?.has_video ? <video controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/media/video`} /> :
                <div className="video-placeholder"><strong>Video not configured</strong><span>Add optional <code>input.video</code> to this project’s pipeline configuration to enable picture review.</span></div>}
              <audio controls preload="metadata" src={`${apiBase}/api/projects/${projectId}/media/audio`} />
            </section>

            <div className="text-compare">
              <article><h3>Czech source</h3><p>{activeGroup.source_text}</p></article>
              <article><h3>Legacy English</h3><p>{activeGroup.legacy_target_text}</p></article>
            </div>

            <form className="translation-editor" onSubmit={saveTranslation}>
              <label>
                Reviewed lip-sync English
                <textarea value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} required />
              </label>
              <div className="editor-footer">
                <label className="approval"><input type="checkbox" checked={approved} onChange={(event) => setApproved(event.target.checked)} /> Approved after bilingual/creative review</label>
                <label className="notes">Notes<input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Pronunciation, intent, or review note" /></label>
                <button type="submit" disabled={busy !== null}>{busy === 'save' ? 'Saving…' : 'Save review'}</button>
              </div>
            </form>

            <section className="actions" aria-label="Targeted pipeline actions">
              <button className="secondary" disabled={busy !== null} onClick={() => queue('apply-translations')}>Apply approved text</button>
              <button disabled={busy !== null} onClick={() => queue('render', { groups: [activeGroup.group], variants: 3, force: true })}>Render 3 fresh takes</button>
              <button className="secondary" disabled={busy !== null} onClick={() => queue('select', { groups: [activeGroup.group] })}>Select best take</button>
            </section>
          </> : <p className="empty">Choose a project with built dialogue turns.</p>}
        </section>

        <aside className="review-sidebar">
          <section>
            <div className="panel-title"><h2>Cast</h2><span>{roles.length}</span></div>
            <div className="role-chips">{roles.map((role) => <span key={role}>{role}</span>)}</div>
            <p className="muted">Reference-audio review is the next panel in this workspace.</p>
          </section>
          <section>
            <div className="panel-title"><h2>Jobs</h2><span>{jobs.length}</span></div>
            <div className="jobs">{jobs.slice(0, 8).map((job) => <div key={job.job_id} className="job"><span className={`job-state ${job.state}`}>{job.state}</span><b>{job.command_name}</b><small>{job.error || job.created_at}</small></div>)}</div>
          </section>
        </aside>
      </section>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
