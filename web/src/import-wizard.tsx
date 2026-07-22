import { DragEvent, FormEvent, useEffect, useRef, useState } from 'react'

type AssetKind = 'audio' | 'video' | 'source_srt' | 'target_srt' | 'dialogue_script'
type Assets = Partial<Record<AssetKind, File>>

const required: AssetKind[] = ['audio', 'source_srt', 'target_srt', 'dialogue_script']
const commonLanguageCodes = ['ar', 'cs', 'de', 'en', 'es', 'fr', 'hi', 'it', 'ja', 'ko', 'nl', 'pl', 'pt', 'ru', 'tr', 'uk', 'vi', 'yue', 'zh']

function assetLabel(kind: AssetKind, sourceLanguage: string, targetLanguage: string): string {
  if (kind === 'audio') return 'Dialogue audio'
  if (kind === 'video') return 'Picture video (optional)'
  if (kind === 'source_srt') return sourceLanguage.trim() ? `Source SRT · ${sourceLanguage.trim().toUpperCase()}` : 'Source-language SRT'
  if (kind === 'target_srt') return targetLanguage.trim() ? `Target SRT · ${targetLanguage.trim().toUpperCase()}` : 'Target-language SRT'
  return 'Role-labelled source dialogue script'
}

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
  const dialogRef = useRef<HTMLElement>(null)
  const [assets, setAssets] = useState<Assets>({})
  const [name, setName] = useState('')
  const [sourceLanguage, setSourceLanguage] = useState('')
  const [targetLanguage, setTargetLanguage] = useState('')
  const [message, setMessage] = useState('Drop the five project assets here, then confirm their roles.')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null
    const focusables = () => Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href]') ?? []).filter((element) => element.offsetParent !== null)
    focusables()[0]?.focus()
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const elements = focusables()
      if (!elements.length) return
      const first = elements[0]
      const last = elements[elements.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      previousFocus?.focus()
    }
  }, [onClose])

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

  return <section className="import-overlay" ref={dialogRef} role="dialog" aria-modal="true" aria-label="Import a new dubbing project">
    <form className="import-wizard" onSubmit={submit}>
      <div className="wizard-heading"><div><p className="eyebrow">NEW LOCAL PROJECT</p><h2>Drop the source assets</h2></div><button type="button" className="secondary" onClick={onClose}>Close</button></div>
      <div className="drop-target" onDragOver={(event) => event.preventDefault()} onDrop={dropped}>
        <strong>Drag dialogue audio, video, two SRTs, and the dialogue script here</strong>
        <span>The browser copies dropped files into this project’s ignored <code>inputs/</code> folder. Their original paths are never sent anywhere.</span>
        <label className="file-button">Choose files<input type="file" multiple onChange={(event) => event.target.files && addFiles(event.target.files)} /></label>
      </div>
      <div className="asset-grid">{(['audio', 'video', 'source_srt', 'target_srt', 'dialogue_script'] as AssetKind[]).map((kind) => <label className={`asset-slot ${assets[kind] ? 'ready' : ''}`} key={kind}>
        <span>{assetLabel(kind, sourceLanguage, targetLanguage)}{required.includes(kind) ? ' *' : ''}</span>
        <b>{assets[kind]?.name ?? 'Drop above or choose file'}</b>
        <input type="file" accept={kind === 'audio' ? 'audio/*' : kind === 'video' ? 'video/*' : kind.includes('srt') ? '.srt' : '.txt,.md'} onChange={(event) => event.target.files?.[0] && setAssets((current) => ({ ...current, [kind]: event.target.files![0] }))} />
      </label>)}</div>
      <div className="wizard-fields">
        <label>Project name<input value={name} onChange={(event) => setName(event.target.value)} required placeholder="my_short" /></label>
        <label>Source language code<input list="language-codes" value={sourceLanguage} onChange={(event) => setSourceLanguage(event.target.value.toLowerCase())} required placeholder="e.g. cs" /></label>
        <label>Target language code<input list="language-codes" value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value.toLowerCase())} required placeholder="e.g. en" /></label>
      </div>
      <datalist id="language-codes">{commonLanguageCodes.map((code) => <option key={code} value={code} />)}</datalist>
      <p className="muted">Use the 2–8 letter language code supported by your translation, TTS, and ASR models. The app accepts any supported source → target pair.</p>
      <p className="wizard-message">{message}</p>
      <button disabled={busy}>{busy ? 'Importing local files…' : 'Create project from dropped assets'}</button>
    </form>
  </section>
}
