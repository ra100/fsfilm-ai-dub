import { DragEvent, FormEvent, useState } from 'react'

type AssetKind = 'audio' | 'video' | 'source_srt' | 'target_srt' | 'dialogue_script'
type Assets = Partial<Record<AssetKind, File>>

const labels: Record<AssetKind, string> = {
  audio: 'Dialogue audio',
  video: 'Picture video (optional)',
  source_srt: 'Czech/source SRT',
  target_srt: 'English/target SRT',
  dialogue_script: 'Role-labelled dialogue script',
}
const required: AssetKind[] = ['audio', 'source_srt', 'target_srt', 'dialogue_script']

function classify(file: File, assets: Assets): AssetKind | null {
  const extension = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (['.wav', '.wave', '.flac', '.mp3', '.m4a', '.aac', '.ogg'].includes(extension)) return 'audio'
  if (['.mov', '.mp4', '.mkv', '.avi', '.webm'].includes(extension)) return 'video'
  if (extension === '.srt') return assets.source_srt ? 'target_srt' : 'source_srt'
  if (['.txt', '.md'].includes(extension)) return 'dialogue_script'
  return null
}

type Props = {
  onImported: (projectId: string) => void
  onClose: () => void
}

export function ImportWizard({ onImported, onClose }: Props) {
  const [assets, setAssets] = useState<Assets>({})
  const [name, setName] = useState('')
  const [sourceLanguage, setSourceLanguage] = useState('cs')
  const [targetLanguage, setTargetLanguage] = useState('en')
  const [message, setMessage] = useState('Drop the five project assets here, then confirm their roles.')
  const [busy, setBusy] = useState(false)

  function addFiles(files: FileList | File[]) {
    setAssets((current) => {
      const next = { ...current }
      for (const file of Array.from(files)) {
        const kind = classify(file, next)
        if (kind) next[kind] = file
      }
      return next
    })
    if (!name) {
      const audio = Array.from(files).find((file) => classify(file, assets) === 'audio')
      if (audio) setName(audio.name.replace(/\.[^.]+$/, ''))
    }
    setMessage('Check the detected mapping before importing. You can replace any slot below.')
  }

  function dropped(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    addFiles(event.dataTransfer.files)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (required.some((kind) => !assets[kind])) {
      setMessage('Dialogue audio, both SRTs, and the role-labelled script are required.')
      return
    }
    setBusy(true)
    try {
      const body = new FormData()
      body.set('project_name', name)
      body.set('source_language', sourceLanguage)
      body.set('target_language', targetLanguage)
      for (const [kind, file] of Object.entries(assets)) if (file) body.set(kind, file)
      const response = await fetch(`${import.meta.env.VITE_API_BASE ?? ''}/api/projects/import`, { method: 'POST', body })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(payload.detail || `Import failed (${response.status})`)
      onImported(payload.id)
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <section className="import-overlay" role="dialog" aria-modal="true" aria-label="Import a new dubbing project">
    <form className="import-wizard" onSubmit={submit}>
      <div className="wizard-heading"><div><p className="eyebrow">NEW LOCAL PROJECT</p><h2>Drop the source assets</h2></div><button type="button" className="secondary" onClick={onClose}>Close</button></div>
      <div className="drop-target" onDragOver={(event) => event.preventDefault()} onDrop={dropped}>
        <strong>Drag dialogue audio, video, two SRTs, and the dialogue script here</strong>
        <span>The browser copies dropped files into this project’s ignored <code>inputs/</code> folder. Their original paths are never sent anywhere.</span>
        <label className="file-button">Choose files<input type="file" multiple onChange={(event) => event.target.files && addFiles(event.target.files)} /></label>
      </div>
      <div className="asset-grid">{(Object.keys(labels) as AssetKind[]).map((kind) => <label className={`asset-slot ${assets[kind] ? 'ready' : ''}`} key={kind}>
        <span>{labels[kind]}{required.includes(kind) ? ' *' : ''}</span>
        <b>{assets[kind]?.name ?? 'Drop above or choose file'}</b>
        <input type="file" accept={kind === 'audio' ? 'audio/*' : kind === 'video' ? 'video/*' : kind.includes('srt') ? '.srt' : '.txt,.md'} onChange={(event) => event.target.files?.[0] && setAssets((current) => ({ ...current, [kind]: event.target.files![0] }))} />
      </label>)}</div>
      <div className="wizard-fields">
        <label>Project name<input value={name} onChange={(event) => setName(event.target.value)} required placeholder="my_short_en" /></label>
        <label>Source language<input value={sourceLanguage} onChange={(event) => setSourceLanguage(event.target.value)} required /></label>
        <label>Target language<input value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)} required /></label>
      </div>
      <p className="wizard-message">{message}</p>
      <button disabled={busy}>{busy ? 'Importing local files…' : 'Create project from dropped assets'}</button>
    </form>
  </section>
}
