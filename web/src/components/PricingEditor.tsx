import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import { integer, shortModel } from '../lib/format'
import type { Pricing } from '../lib/types'

interface Props {
  pricing: Pricing | null
  onSaved: () => void
}

type Draft = Record<string, Record<string, number>>

/**
 * Rates apply on read, so an edit here changes every figure on the page with no
 * rescan. The one exception is the long-context threshold, which decides which
 * tier a request falls into and therefore forces a rollup rebuild -- the server
 * handles that automatically.
 */
export function PricingEditor({ pricing, onSaved }: Props) {
  const [draft, setDraft] = useState<Draft>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => setDraft({}), [pricing?.path])

  if (!pricing) return <div className="empty">Loading rates…</div>

  const dirty = Object.keys(draft).length > 0

  const value = (model: string, field: string, fallback: number) =>
    draft[model]?.[field] ?? fallback

  const edit = (model: string, field: string, raw: string) => {
    const n = Number(raw)
    if (!isFinite(n)) return
    setDraft((d) => ({ ...d, [model]: { ...(d[model] ?? {}), [field]: n } }))
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const models: Record<string, unknown> = {}
      for (const [name, fields] of Object.entries(draft)) {
        const entry: Record<string, unknown> = {}
        const long: Record<string, number> = {}
        for (const [field, v] of Object.entries(fields)) {
          // long_context_threshold is a top-level key that happens to share the
          // prefix; stripping it would write `long.context_threshold`, which
          // nothing reads, and the edit would silently do nothing.
          if (field.startsWith('long_') && field !== 'long_context_threshold') {
            long[field.slice(5)] = v
          } else {
            entry[field] = v
          }
        }
        if (Object.keys(long).length) entry.long = long
        models[name] = entry
      }
      await api.savePricing(models)
      setDraft({})
      onSaved()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const names = Object.keys(pricing.models).sort()

  return (
    <div>
      <div className="table-scroll">
        <table className="data">
          <thead>
            <tr>
              <th>Model</th>
              <th title="USD per million prompt tokens, short context">Input</th>
              <th title="USD per million cached prompt tokens, short context">Cached</th>
              <th title="USD per million prompt tokens written to a 5-minute cache entry. Zero where the provider stores for free.">
                Write 5m
              </th>
              <th title="USD per million prompt tokens written to a 1-hour cache entry">
                Write 1h
              </th>
              <th title="USD per million output tokens, short context">Output</th>
              <th title="Prompt tokens above which long-context rates apply">Long above</th>
              <th title="USD per million prompt tokens, long context">Long in</th>
              <th title="USD per million cached prompt tokens, long context">Long cache</th>
              <th title="USD per million output tokens, long context">Long out</th>
            </tr>
          </thead>
          <tbody>
            {names.map((name) => {
              const m = pricing.models[name]
              return (
                <tr key={name}>
                  <td>
                    <span className="name-cell">
                      <span className="label">{shortModel(name)}</span>
                      {m.estimated && <span className="tag" title="Inferred, not published">est</span>}
                      {m.long_tier_unknown && (
                        <span className="tag" title="No published long-context tier">
                          no long tier
                        </span>
                      )}
                    </span>
                  </td>
                  <NumCell v={value(name, 'input', m.input)} on={(s) => edit(name, 'input', s)} />
                  <NumCell
                    v={value(name, 'cached_input', m.cached_input)}
                    on={(s) => edit(name, 'cached_input', s)}
                  />
                  <NumCell
                    v={value(name, 'cache_write', m.cache_write)}
                    on={(s) => edit(name, 'cache_write', s)}
                    muted={!m.charges_cache_writes}
                  />
                  <NumCell
                    v={value(name, 'cache_write_1h', m.cache_write_1h)}
                    on={(s) => edit(name, 'cache_write_1h', s)}
                    muted={!m.charges_cache_writes}
                  />
                  <NumCell v={value(name, 'output', m.output)} on={(s) => edit(name, 'output', s)} />
                  <NumCell
                    v={value(name, 'long_context_threshold', m.threshold)}
                    on={(s) => edit(name, 'long_context_threshold', s)}
                    step={10000}
                    width={80}
                  />
                  <NumCell
                    v={value(name, 'long_input', m.long_input)}
                    on={(s) => edit(name, 'long_input', s)}
                    muted={!m.has_long_tier}
                  />
                  <NumCell
                    v={value(name, 'long_cached_input', m.long_cached_input)}
                    on={(s) => edit(name, 'long_cached_input', s)}
                    muted={!m.has_long_tier}
                  />
                  <NumCell
                    v={value(name, 'long_output', m.long_output)}
                    on={(s) => edit(name, 'long_output', s)}
                    muted={!m.has_long_tier}
                  />
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginTop: 10 }}>
        <button className="btn" type="button" disabled={!dirty || saving} onClick={save}>
          {saving ? 'Saving…' : dirty ? `Apply ${Object.keys(draft).length} change(s)` : 'No changes'}
        </button>
        {dirty && (
          <button className="btn" type="button" onClick={() => setDraft({})}>
            Discard
          </button>
        )}
        <span className="note">
          USD per million tokens. Edits are written to {pricing.path} and applied
          immediately — no rescan.
        </span>
        {error && <span className="bad">{error}</span>}
      </div>

      {pricing.unpriced.length > 0 && (
        <div className="callout" style={{ marginTop: 10 }}>
          <span>
            <strong>{pricing.unpriced.length} unpriced model(s)</strong> seen in the
            corpus: {pricing.unpriced.join(', ')}. Their tokens are counted but cost
            nothing until you add rates.
          </span>
        </div>
      )}

      <p className="note" style={{ marginTop: 8 }}>
        Default long-context threshold: {integer(pricing.default_threshold)} prompt
        tokens.
      </p>
    </div>
  )
}

function NumCell({
  v,
  on,
  step = 0.05,
  width = 66,
  muted,
}: {
  v: number
  on: (raw: string) => void
  step?: number
  width?: number
  muted?: boolean
}) {
  return (
    <td>
      <input
        className="field mono"
        type="number"
        step={step}
        min={0}
        value={v}
        onChange={(e) => on(e.target.value)}
        style={{ width, textAlign: 'right', opacity: muted ? 0.55 : 1 }}
      />
    </td>
  )
}
