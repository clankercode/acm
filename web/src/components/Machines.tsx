import { useRef, useState, type DragEvent } from 'react'
import { api } from '../lib/api'
import { compact, fullTime, integer, pct, rate, usd } from '../lib/format'
import { useQuery } from '../lib/live'
import type { ImportPreview, Machine } from '../lib/types'
import { sourceLabel } from '../lib/types'

interface Props {
  generation: number
  onChanged: () => void
}

/**
 * Carrying stats between machines.
 *
 * Only token counts travel; costs are always recomputed here, against this
 * machine's rate table. That is the whole reason figures from several machines
 * can be laid side by side at all -- if each bundle carried its own prices, the
 * combined total would be an average of whatever each exporter happened to
 * believe on the day.
 */
export function Machines({ generation, onChanged }: Props) {
  const { data, refetch } = useQuery((s) => api.machines(s), [generation])
  const machines = data?.machines ?? []
  const imported = machines.filter((m) => !m.local)

  const [label, setLabel] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string> | null>(null)
  const [pending, setPending] = useState<{
    bundle: unknown
    preview: ImportPreview
    label: string
  } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement | null>(null)

  const over = (e: DragEvent) => {
    // Both handlers must cancel the event, or the browser navigates to the file
    // and the page -- with everything on it -- is gone.
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    setDragging(true)
  }

  const exportLabel = label ?? data?.local_label ?? ''
  // Default to everything, so a machine that has already pooled others
  // re-exports the pool rather than just its own slice.
  const chosen = selected ?? new Set(machines.map((m) => m.origin))

  const toggle = (origin: string) => {
    const next = new Set(chosen)
    if (next.has(origin)) next.delete(origin)
    else next.add(origin)
    setSelected(next)
  }

  const pickFile = async (file: File) => {
    setError(null)
    setPending(null)
    let bundle: unknown
    try {
      bundle = JSON.parse(await file.text())
    } catch {
      // A parser error here is almost always the wrong file, and quoting the
      // parser at someone who dropped a screenshot on it explains nothing.
      setError(`${file.name} is not a JSON file.`)
      return
    }
    try {
      const preview = await api.previewImport(bundle)
      setPending({ bundle, preview, label: preview.label })
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const commit = async () => {
    if (!pending) return
    setBusy(true)
    setError(null)
    try {
      await api.commitImport(pending.bundle, pending.label)
      setPending(null)
      setSelected(null)
      refetch()
      onChanged()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (m: Machine) => {
    await api.deleteMachine(m.origin)
    refetch()
    onChanged()
  }

  const rename = async (m: Machine, next: string) => {
    if (!next.trim() || next === m.label) return
    await api.renameMachine(m.origin, next)
    refetch()
    onChanged()
  }

  return (
    <div className="stack">
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th>Machine</th>
              <th className="num">Requests</th>
              <th className="num">Input</th>
              <th className="num">Cache</th>
              <th className="num">Cost</th>
              <th className="num">$/Mtok</th>
              <th>Imported</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {machines.map((m) => (
              <tr key={m.origin || 'local'}>
                <td>
                  <span className="name-cell">
                    <input
                      className="field inline-name"
                      defaultValue={m.label}
                      aria-label={`Name for ${m.label}`}
                      onBlur={(e) => rename(m, e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                      }}
                    />
                    {m.local && <span className="tag">this machine</span>}
                    {m.contributors.length > 1 && (
                      <span className="tag" title={m.contributors.join(', ')}>
                        {m.contributors.length} machines
                      </span>
                    )}
                  </span>
                </td>
                <td className="num">{integer(m.requests)}</td>
                <td className="num">{compact(m.input_tokens)}</td>
                <td className="num">{pct(m.cache_rate, 1)}</td>
                <td className="num">{usd(m.cost)}</td>
                <td className="num">{rate(m.effective_rate)}</td>
                <td className="dim">
                  {m.imported_at ? fullTime(m.imported_at) : 'live'}
                </td>
                <td className="num">
                  {!m.local && (
                    <button className="btn" type="button" onClick={() => remove(m)}>
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="split">
        <section>
          <h3 className="sub-title">Export</h3>
          <p className="note">
            Token counts and per-session stats only. No costs, no file paths, no
            working directories — the receiving machine prices what arrives with its
            own rate table.
          </p>
          <div className="row">
            <label className="control" htmlFor="export-label">
              Label
            </label>
            <input
              id="export-label"
              className="field"
              value={exportLabel}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="this machine"
            />
            <button
              className="btn primary"
              type="button"
              disabled={chosen.size === 0}
              onClick={() => api.downloadExport(exportLabel, [...chosen])}
            >
              Download bundle
            </button>
          </div>
          {machines.length > 1 && (
            <div className="row wrap">
              <span className="control">Include</span>
              {machines.map((m) => (
                <label className="chip" key={m.origin || 'local'}>
                  <input
                    type="checkbox"
                    checked={chosen.has(m.origin)}
                    onChange={() => toggle(m.origin)}
                  />
                  {m.label}
                </label>
              ))}
            </div>
          )}
        </section>

        <section>
          <h3 className="sub-title">Import</h3>
          <p className="note">
            Adds another machine's data alongside this one. Re-importing under an
            existing name replaces it.
          </p>
          {/* A drop target that is also a button, because a bundle arrives
              either way -- dragged out of a downloads folder, or picked. The
              input stays in the DOM (hidden) so the label keeps it keyboard
              reachable and screen readers still announce a file control. */}
          <label
            className={'dropzone' + (dragging ? ' over' : '')}
            onDragEnter={over}
            onDragOver={over}
            onDragLeave={(e) => {
              // Ignore the leave events fired when the cursor crosses onto a
              // child, or the zone flickers as you move across it.
              if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false)
            }}
            onDrop={(e) => {
              e.preventDefault()
              setDragging(false)
              const file = e.dataTransfer.files?.[0]
              if (file) pickFile(file)
            }}
          >
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              className="visually-hidden"
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) pickFile(file)
                e.target.value = ''
              }}
            />
            <span className="dropzone-main">
              {dragging ? 'Drop to read it' : 'Drop a bundle here'}
            </span>
            <span className="dropzone-sub">or click to choose a .json file</span>
          </label>

          {pending && (
            <div className="preview">
              <dl className="kv">
                <dt>From</dt>
                <dd>
                  {pending.preview.machine ?? 'unknown'}
                  {pending.preview.contributors.length > 1 &&
                    ` · pooled from ${pending.preview.contributors.length} machines`}
                </dd>
                <dt>Exported</dt>
                <dd>
                  {pending.preview.exported_at
                    ? fullTime(pending.preview.exported_at)
                    : 'unknown'}
                </dd>
                <dt>Contains</dt>
                <dd>
                  {integer(pending.preview.summary.requests ?? 0)} requests ·{' '}
                  {compact(pending.preview.summary.input_tokens ?? 0)} input ·{' '}
                  {integer(pending.preview.sessions)} session rows
                </dd>
                <dt>Clients</dt>
                <dd>
                  {(pending.preview.summary.clients ?? []).map(sourceLabel).join(', ') ||
                    'unknown'}
                </dd>
                <dt>Range</dt>
                <dd>
                  {pending.preview.summary.first_ts
                    ? `${fullTime(pending.preview.summary.first_ts)} → ${fullTime(
                        pending.preview.summary.last_ts ?? pending.preview.summary.first_ts,
                      )}`
                    : 'unknown'}
                </dd>
              </dl>

              <div className="row">
                <label className="control" htmlFor="import-label">
                  Label
                </label>
                <input
                  id="import-label"
                  className="field"
                  value={pending.label}
                  onChange={(e) => setPending({ ...pending, label: e.target.value })}
                />
                <button
                  className="btn primary"
                  type="button"
                  disabled={busy || !pending.label.trim()}
                  onClick={commit}
                >
                  {busy ? 'Importing…' : 'Import'}
                </button>
                <button className="btn" type="button" onClick={() => setPending(null)}>
                  Cancel
                </button>
              </div>
              {pending.preview.collision && (
                <p className="note warn">
                  A machine called “{pending.preview.suggested_label}” already exists,
                  so the name was made unique. Type the original name to replace it
                  instead.
                </p>
              )}
            </div>
          )}

          {error && <p className="note bad">{error}</p>}
        </section>
      </div>

      {imported.length > 0 && (
        <p className="note">
          Imported machines contribute to every chart, KPI and table. They carry no
          per-request timeline, so their sessions show totals only.
        </p>
      )}
    </div>
  )
}
