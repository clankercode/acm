import { useEffect, useMemo, useState } from 'react'
import { api } from '../lib/api'
import {
  compact,
  duration,
  fullTime,
  integer,
  pct,
  rate,
  repoLabel,
  shortModel,
  usd,
} from '../lib/format'
import { useQuery } from '../lib/live'
import type { ColorScale, Palette } from '../lib/palette'
import { sourceLabel, type Filters, type SessionRow } from '../lib/types'
import { TimeChart, type TimeSeries } from './TimeChart'

type SortKey =
  | 'first_ts'
  | 'cost'
  | 'input_tokens'
  | 'cache_rate'
  | 'effective_rate'
  | 'requests'
  | 'output_tokens'
  | 'cost_output'

type Column = { key: SortKey | 'name' | 'source'; label: string; title?: string; out?: boolean }

// Only two of the three output columns: a per-session $/M out is the model's
// list price with extra steps, and the model is already in the row as a swatch.
const COLUMNS: Column[] = [
  { key: 'name', label: 'Session' },
  { key: 'source', label: 'Client' },
  { key: 'first_ts', label: 'Started' },
  { key: 'requests', label: 'Reqs' },
  { key: 'input_tokens', label: 'Input' },
  { key: 'cache_rate', label: 'Cache' },
  { key: 'cost', label: 'Cost' },
  { key: 'effective_rate', label: '$/Mtok', title: 'Lower is better' },
  { key: 'output_tokens', label: 'Out', title: 'Tokens generated', out: true },
  { key: 'cost_output', label: 'Out $', title: 'What generation cost', out: true },
]

interface Props {
  filters: Filters
  generation: number
  colors: ColorScale
  palette: Palette
  showOutput?: boolean
}

export function SessionExplorer({ filters, generation, colors, palette, showOutput }: Props) {
  const [sort, setSort] = useState<{ key: SortKey; desc: boolean }>({
    key: 'first_ts',
    desc: true,
  })
  const [search, setSearch] = useState('')
  const [limit, setLimit] = useState(60)
  const [selected, setSelected] = useState<string | null>(null)

  const { data, loading } = useQuery(
    (signal) => api.sessions(filters, signal),
    [JSON.stringify(filters), generation],
  )

  const rows = useMemo(() => {
    let list = data?.rows ?? []
    const q = search.trim().toLowerCase()
    if (q) {
      list = list.filter((r) =>
        [r.rollout_id, r.source, r.agent_nickname, r.agent_role, r.repo, r.cwd, r.model]
          .filter(Boolean)
          .some((v) => String(v).toLowerCase().includes(q)),
      )
    }
    const copy = [...list]
    copy.sort((a, b) => {
      const cmp = (a[sort.key] as number) - (b[sort.key] as number)
      return sort.desc ? -cmp : cmp
    })
    return copy
  }, [data, search, sort])

  useEffect(() => setLimit(60), [search, JSON.stringify(filters)])

  const columns = useMemo(
    () => COLUMNS.filter((c) => showOutput || !c.out),
    [showOutput],
  )

  // Hiding the group must not leave the table sorted by a column nobody can see,
  // with no header arrow to say why the order looks arbitrary.
  useEffect(() => {
    if (showOutput) return
    setSort((s) =>
      s.key === 'output_tokens' || s.key === 'cost_output'
        ? { key: 'first_ts', desc: true }
        : s,
    )
  }, [showOutput])

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Sessions</h2>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              className="field"
              placeholder="filter by id, agent, repo…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ width: 210 }}
            />
            <span className="panel-note">
              {loading ? 'loading…' : `${integer(rows.length)} rollouts`}
            </span>
          </div>
        </div>
        <div className="panel-body" style={{ padding: 0 }}>
          <div className="table-scroll" style={{ maxHeight: 460, overflowY: 'auto' }}>
            <table className="data">
              <thead>
                <tr>
                  {columns.map((col) => (
                    <th
                      key={col.key}
                      className={
                        col.out
                          ? col.key === 'output_tokens' ? 'out group-start' : 'out'
                          : undefined
                      }
                      title={col.title}
                      aria-sort={
                        sort.key === col.key
                          ? sort.desc
                            ? 'descending'
                            : 'ascending'
                          : undefined
                      }
                      onClick={() => {
                        // Both are text; the comparator here is numeric.
                        if (col.key === 'name' || col.key === 'source') return
                        const key = col.key as SortKey
                        setSort((s) =>
                          s.key === key ? { key, desc: !s.desc } : { key, desc: true },
                        )
                      }}
                    >
                      {col.label}
                      {sort.key === col.key ? (sort.desc ? ' ↓' : ' ↑') : ''}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, limit).map((row) => (
                  <tr
                    key={row.rollout_id}
                    className="clickable"
                    onClick={() => setSelected(row.rollout_id)}
                  >
                    <td>
                      <span className="name-cell">
                        <span
                          className="swatch"
                          style={{ background: colors.get(row.model ?? '') }}
                        />
                        <span className="label">
                          {row.agent_nickname || row.rollout_id.slice(0, 8)}
                        </span>
                        {row.is_subagent && (
                          <span className="tag">
                            sub{row.depth != null ? ` ${row.depth}` : ''}
                          </span>
                        )}
                        {row.agent_role && <span className="tag">{row.agent_role}</span>}
                        <span className="tag">{repoLabel(row.repo)}</span>
                      </span>
                    </td>
                    <td className="dim">{sourceLabel(row.source)}</td>
                    <td title={fullTime(row.first_ts)}>
                      {new Date(row.first_ts).toLocaleString(undefined, {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: false,
                      })}
                    </td>
                    <td>{integer(row.requests)}</td>
                    <td>{compact(row.input_tokens)}</td>
                    <td className={row.cache_rate < 0.8 ? 'bad' : undefined}>
                      {pct(row.cache_rate, 1)}
                    </td>
                    <td>{usd(row.cost)}</td>
                    <td>{rate(row.effective_rate)}</td>
                    {showOutput && (
                      <>
                        <td className="out group-start">{compact(row.output_tokens)}</td>
                        <td className="out">{usd(row.cost_output)}</td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {rows.length > limit && (
            <div style={{ padding: 8, textAlign: 'center' }}>
              <button className="btn" type="button" onClick={() => setLimit((n) => n + 200)}>
                Show more ({integer(rows.length - limit)} remaining)
              </button>
            </div>
          )}
        </div>
      </div>

      {selected && (
        <SessionDrawer
          rolloutId={selected}
          onClose={() => setSelected(null)}
          colors={colors}
          palette={palette}
          row={rows.find((r) => r.rollout_id === selected) ?? null}
          showOutput={showOutput}
        />
      )}
    </>
  )
}

function SessionDrawer({
  rolloutId,
  onClose,
  colors,
  palette,
  row,
  showOutput,
}: {
  rolloutId: string
  onClose: () => void
  colors: ColorScale
  palette: Palette
  row: SessionRow | null
  showOutput?: boolean
}) {
  const { data } = useQuery((signal) => api.session(rolloutId, signal), [rolloutId])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const { t, cacheSeries, ctxSeries, outSeries, markers } = useMemo(() => {
    const reqs = data?.requests ?? []
    const times = reqs.map((r) => Math.round(r.ts / 1000))
    return {
      t: times,
      cacheSeries: [
        {
          key: 'cache',
          label: 'Cache rate',
          color: colors.get(row?.model ?? '') || palette.slots[0],
          values: reqs.map((r) => r.cache_rate * 100),
        },
      ] as TimeSeries[],
      ctxSeries: [
        {
          key: 'ctx',
          label: 'Prompt tokens',
          color: palette.slots[1],
          values: reqs.map((r) => r.input),
          area: true,
        },
      ] as TimeSeries[],
      // Per request rather than per bucket, which is the one place the shape of
      // a turn is visible: a long reasoning pass is a spike here and invisible
      // in the prompt-size chart above it.
      outSeries: [
        {
          key: 'out',
          label: 'Output tokens',
          color: palette.slots[2],
          values: reqs.map((r) => r.output),
          area: true,
        },
      ] as TimeSeries[],
      markers: (data?.events ?? []).map((e) => ({ t: Math.round(e.ts / 1000), kind: e.kind })),
    }
  }, [data, colors, palette, row])

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" aria-label="Session detail">
        <div className="drawer-head">
          <strong>{row?.agent_nickname || rolloutId.slice(0, 12)}</strong>
          {row?.is_subagent && <span className="tag">subagent</span>}
          <div className="spacer" style={{ flex: 1 }} />
          <button className="btn" type="button" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="drawer-body">
          {!data && <div className="empty">Loading…</div>}
          {data && (
            <>
              <dl className="kv">
                <dt>Rollout</dt>
                <dd>{rolloutId}</dd>
                <dt>Lineage</dt>
                <dd>{row?.session_id ?? '--'}</dd>
                <dt>Repo</dt>
                <dd>{row?.repo ?? '--'}</dd>
                <dt>Working dir</dt>
                <dd>{row?.cwd ?? '--'}</dd>
                <dt>Models</dt>
                <dd>{(row?.models ?? []).map(shortModel).join(', ') || '--'}</dd>
                <dt>Span</dt>
                <dd>
                  {fullTime(row?.first_ts)} · {duration(row?.duration_ms)}
                </dd>
              </dl>

              <div className="kpis">
                <div className="kpi">
                  <div className="kpi-label">Requests</div>
                  <div className="kpi-value">{integer(data.totals.requests)}</div>
                </div>
                <div className="kpi">
                  <div className="kpi-label">Cache</div>
                  <div className="kpi-value">{pct(data.totals.cache_rate, 1)}</div>
                </div>
                <div className="kpi">
                  <div className="kpi-label">Cost</div>
                  <div className="kpi-value">{usd(data.totals.cost)}</div>
                </div>
                <div className="kpi">
                  <div className="kpi-label">$/Mtok</div>
                  <div className="kpi-value lead">{rate(data.totals.effective_rate)}</div>
                </div>
                {showOutput && (
                  <div className="kpi">
                    <div className="kpi-label">Output</div>
                    <div className="kpi-value">{compact(data.totals.output_tokens)}</div>
                  </div>
                )}
              </div>

              <div className="panel">
                <div className="panel-head">
                  <h3 className="panel-title">Cache rate per request</h3>
                  {markers.length > 0 && (
                    <span className="panel-note">
                      {markers.length} compaction/abort marker
                      {markers.length === 1 ? '' : 's'}
                    </span>
                  )}
                </div>
                <div className="panel-body">
                  <TimeChart
                    t={t}
                    series={cacheSeries}
                    palette={palette}
                    height={150}
                    bucketSeconds={60}
                    format={(v) => (v == null ? '--' : `${v.toFixed(0)}%`)}
                    unit="%"
                    yMin={0}
                    yMax={100}
                    markers={markers}
                  />
                </div>
              </div>

              <div className="panel">
                <div className="panel-head">
                  <h3 className="panel-title">Prompt size per request</h3>
                </div>
                <div className="panel-body">
                  <TimeChart
                    t={t}
                    series={ctxSeries}
                    palette={palette}
                    height={150}
                    bucketSeconds={60}
                    format={(v) => compact(v)}
                    unit="tokens"
                    markers={markers}
                  />
                </div>
              </div>

              {showOutput && (
                <div className="panel">
                  <div className="panel-head">
                    <h3 className="panel-title">Output per request</h3>
                    <span className="panel-note">reasoning included</span>
                  </div>
                  <div className="panel-body">
                    <TimeChart
                      t={t}
                      series={outSeries}
                      palette={palette}
                      height={150}
                      bucketSeconds={60}
                      format={(v) => compact(v)}
                      unit="tokens"
                      markers={markers}
                    />
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    </>
  )
}
