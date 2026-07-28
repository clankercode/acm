import { useMemo, useState } from 'react'
import { compact, integer, pct, rate, usd } from '../lib/format'
import type { ColorScale } from '../lib/palette'
import type { BreakdownRow } from '../lib/types'

type Col = {
  key: keyof BreakdownRow
  label: string
  render: (row: BreakdownRow) => string
  /** Draws a proportional bar behind the cell, scaled to the column max. */
  bar?: boolean
  title?: string
}

const COLUMNS: Col[] = [
  { key: 'requests', label: 'Reqs', render: (r) => integer(r.requests) },
  { key: 'input_tokens', label: 'Input', render: (r) => compact(r.input_tokens), bar: true },
  {
    key: 'cache_rate',
    label: 'Cache',
    render: (r) => pct(r.cache_rate, 1),
    title: 'Cached share of prompt tokens',
  },
  { key: 'cost', label: 'Cost', render: (r) => usd(r.cost), bar: true },
  { key: 'saved', label: 'Saved', render: (r) => usd(r.saved) },
  {
    key: 'effective_rate',
    label: '$/Mtok',
    render: (r) => rate(r.effective_rate),
    bar: true,
    title: 'Cost per million input tokens processed. Lower is better.',
  },
  {
    key: 'efficiency',
    label: 'Of list',
    render: (r) => pct(r.efficiency, 0),
    title: 'Share of the undiscounted list price actually paid',
  },
]

interface Props {
  rows: BreakdownRow[]
  label: (key: string) => string
  colors?: ColorScale
  /** Marks rows worth attention, e.g. a route with an unusually poor cache rate. */
  flag?: (row: BreakdownRow) => string | null
  onSelect?: (key: string) => void
}

export function BreakdownTable({ rows, label, colors, flag, onSelect }: Props) {
  const [sort, setSort] = useState<{ key: keyof BreakdownRow; desc: boolean }>({
    key: 'cost',
    desc: true,
  })

  const sorted = useMemo(() => {
    const copy = [...rows]
    copy.sort((a, b) => {
      const av = a[sort.key]
      const bv = b[sort.key]
      const cmp =
        typeof av === 'number' && typeof bv === 'number'
          ? av - bv
          : String(av).localeCompare(String(bv))
      return sort.desc ? -cmp : cmp
    })
    return copy
  }, [rows, sort])

  const maxima = useMemo(() => {
    const m: Partial<Record<keyof BreakdownRow, number>> = {}
    for (const col of COLUMNS) {
      if (!col.bar) continue
      m[col.key] = Math.max(...rows.map((r) => Number(r[col.key]) || 0), 0)
    }
    return m
  }, [rows])

  const totals = useMemo(() => {
    const input = rows.reduce((a, r) => a + r.input_tokens, 0)
    const cost = rows.reduce((a, r) => a + r.cost, 0)
    return {
      requests: rows.reduce((a, r) => a + r.requests, 0),
      input,
      cached: rows.reduce((a, r) => a + r.cached_tokens, 0),
      cost,
      saved: rows.reduce((a, r) => a + r.saved, 0),
      uncached: rows.reduce((a, r) => a + r.uncached_cost, 0),
      eff: input > 0 ? cost / (input / 1e6) : 0,
    }
  }, [rows])

  if (!rows.length) return <div className="empty">Nothing in this range</div>

  const header = (col: Col) => (
    <th
      key={String(col.key)}
      title={col.title}
      aria-sort={sort.key === col.key ? (sort.desc ? 'descending' : 'ascending') : undefined}
      onClick={() =>
        setSort((s) => (s.key === col.key ? { key: col.key, desc: !s.desc } : { key: col.key, desc: true }))
      }
    >
      {col.label}
      {sort.key === col.key ? (sort.desc ? ' ↓' : ' ↑') : ''}
    </th>
  )

  return (
    <div className="table-scroll">
      <table className="data">
        <thead>
          <tr>
            <th
              aria-sort={sort.key === 'key' ? (sort.desc ? 'descending' : 'ascending') : undefined}
              onClick={() =>
                setSort((s) => (s.key === 'key' ? { key: 'key', desc: !s.desc } : { key: 'key', desc: false }))
              }
            >
              Name
            </th>
            {COLUMNS.map(header)}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => {
            const note = flag?.(row)
            return (
              <tr
                key={row.key}
                className={onSelect ? 'clickable' : undefined}
                onClick={onSelect ? () => onSelect(row.key) : undefined}
              >
                <td>
                  <span className="name-cell">
                    {colors && (
                      <span className="swatch" style={{ background: colors.get(row.key) }} />
                    )}
                    <span className="label">{label(row.key)}</span>
                    {note && <span className="tag">{note}</span>}
                  </span>
                </td>
                {COLUMNS.map((col) => {
                  const max = maxima[col.key]
                  const value = Number(row[col.key]) || 0
                  return (
                    <td key={String(col.key)} className={col.bar ? 'bar-cell' : undefined}>
                      {col.bar && max ? (
                        <span className="bar" style={{ width: `${(value / max) * 100}%` }} />
                      ) : null}
                      <span className="txt">{col.render(row)}</span>
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>Total</td>
            <td>{integer(totals.requests)}</td>
            <td>{compact(totals.input)}</td>
            <td>{pct(totals.input ? totals.cached / totals.input : 0, 1)}</td>
            <td>{usd(totals.cost)}</td>
            <td>{usd(totals.saved)}</td>
            <td>{rate(totals.eff)}</td>
            <td>{pct(totals.uncached ? totals.cost / totals.uncached : 0, 0)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  )
}
