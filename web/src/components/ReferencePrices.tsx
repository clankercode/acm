import { useState } from 'react'
import { api } from '../lib/api'
import { compact, fullTime, shortModel } from '../lib/format'
import { useQuery } from '../lib/live'
import type { ReferenceField, ReferenceStatus } from '../lib/types'

const FIELD_LABELS: Record<string, string> = {
  input: 'Input',
  cached_input: 'Cached',
  cache_write: 'Cache write',
  output: 'Output',
}

/**
 * A second opinion on the rate table.
 *
 * `pricing.toml` stays the source of truth — it is hand-checked against each
 * vendor's own page — but a rate that quietly goes stale is invisible without
 * something to hold it against.
 *
 * The distinction that matters here is between a difference that would change a
 * figure and one that cannot. models.dev quotes a cache-write rate for the
 * OpenAI models (its house 1.25x convention) that OpenAI does not charge and
 * Codex never reports. Flagging that as a discrepancy would train the reader to
 * ignore this table, so a difference in a category with no observed tokens is
 * shown as inert.
 */
export function ReferencePrices({ generation }: { generation: number }) {
  const { data, loading, refetch } = useQuery((s) => api.reference(s), [generation])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showAll, setShowAll] = useState(false)

  const refresh = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.refreshReference()
      refetch()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const models = data?.models ?? []
  const differing = models.filter((m) => m.status === 'differs')
  const unlisted = models.filter((m) => m.status === 'unlisted')
  const shown = showAll ? models : models.filter((m) => m.status !== 'match')

  return (
    <div className="stack">
      <div className="row">
        <span className="note">
          {data?.available
            ? `models.dev · ${data.providers} providers · fetched ${
                data.fetched_at ? fullTime(data.fetched_at * 1000) : 'never'
              }`
            : 'models.dev has not been fetched yet'}
        </span>
        <button className="btn" type="button" onClick={refresh} disabled={busy}>
          {busy ? 'Fetching…' : data?.available ? 'Refresh' : 'Fetch now'}
        </button>
        {models.length > 0 && (
          <button className="btn" type="button" onClick={() => setShowAll((v) => !v)}>
            {showAll ? 'Only differences' : `Show all ${models.length}`}
          </button>
        )}
      </div>

      {(error || data?.error) && <p className="note bad">{error ?? data?.error}</p>}

      {!loading && data?.available && (
        <p className="note">
          {differing.length === 0 ? (
            <>
              Every configured rate that carries traffic agrees with models.dev.
              {unlisted.length > 0 &&
                ` ${unlisted.length} model${unlisted.length === 1 ? ' is' : 's are'} not listed there.`}
            </>
          ) : (
            <span className="bad">
              {differing.length} model{differing.length === 1 ? '' : 's'} priced
              differently from models.dev in a category that carries tokens.
            </span>
          )}
        </p>
      )}

      {shown.length > 0 && (
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th>Model</th>
                <th>Listed by</th>
                <th className="num">Input</th>
                <th className="num">Cached</th>
                <th className="num">Cache write</th>
                <th className="num">Output</th>
                <th className="num">Tokens seen</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((m) => (
                <tr key={m.model}>
                  <td>
                    <span className="name-cell">
                      <span className="label">{shortModel(m.model)}</span>
                      {m.estimated && (
                        <span className="tag" title="Inferred, not published">
                          est
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="dim">
                    {m.provider ?? <span className="warn">not listed</span>}
                    {m.offers && m.offers > 1 ? ` +${m.offers - 1}` : ''}
                  </td>
                  {['input', 'cached_input', 'cache_write', 'output'].map((name) => (
                    <Cell
                      key={name}
                      field={m.fields.find((f) => f.field === name)}
                    />
                  ))}
                  <td className="num dim">{compact(m.observed_tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {models.some((m) => m.status === 'inert') && (
        <p className="note">
          <strong>inert</strong> marks a rate that disagrees in a category with no
          tokens recorded against it, so it cannot move any figure — models.dev
          quotes a cache-write price for the OpenAI models, which OpenAI does not
          charge and Codex never reports.
        </p>
      )}
    </div>
  )
}

function Cell({ field }: { field: ReferenceField | undefined }) {
  if (!field) return <td className="num dim">—</td>
  if (field.state === 'unlisted') {
    return (
      <td className="num dim" title="Not quoted by models.dev">
        {field.ours}
      </td>
    )
  }
  if (field.state === 'match') {
    return (
      <td className="num" title={`${FIELD_LABELS[field.field]}: agrees`}>
        {field.ours}
      </td>
    )
  }
  const inert = field.state === 'inert'
  return (
    <td
      className={'num ' + (inert ? 'dim' : 'bad')}
      title={
        inert
          ? `models.dev says ${field.theirs}, but no tokens were billed in this category`
          : `models.dev says ${field.theirs} across ${compact(field.tokens)} tokens`
      }
    >
      {field.ours} <span className="dim">≠ {field.theirs}</span>
      {inert && <span className="tag">inert</span>}
    </td>
  )
}

export type { ReferenceStatus }
