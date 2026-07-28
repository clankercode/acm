import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { compact, integer, pct, rate, usd } from '../lib/format'
import type { ColorScale } from '../lib/palette'
import type { BreakdownRow } from '../lib/types'

/** Rows shown before a table starts scrolling instead of growing the page. */
const DEFAULT_ROWS = 24

/**
 * Extra rows a table may exceed the cap by and still be drawn whole.
 *
 * Clipping 26 rows down to 24 trades two rows of page height for a scrollbar
 * and a handle, which is a worse deal than just showing them.
 */
const CLIP_SLACK = 4

/** Small enough to be useful for a glance at the top few, not a sliver. */
const MIN_ROWS = 5

/** Fallback when a row height cannot be measured yet, e.g. the first paint. */
const ASSUMED_ROW_PX = 22

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

  // `null` means "however many the cap allows"; a number is the user's own
  // choice, which survives the data changing underneath it.
  const [chosenRows, setChosenRows] = useState<number | null>(null)
  const [rowPx, setRowPx] = useState(ASSUMED_ROW_PX)
  const [chromePx, setChromePx] = useState(0)
  const headRef = useRef<HTMLTableSectionElement | null>(null)
  const bodyRef = useRef<HTMLTableSectionElement | null>(null)
  const footRef = useRef<HTMLTableSectionElement | null>(null)

  // Long enough to be worth clipping at all. Short tables -- every panel here
  // except the repository one -- are left exactly as they were.
  const clipped = rows.length > DEFAULT_ROWS + CLIP_SLACK
  const visible = Math.min(
    Math.max(chosenRows ?? DEFAULT_ROWS, MIN_ROWS),
    rows.length,
  )

  // Measured rather than assumed, so the clip lands on a row boundary instead of
  // halfway through one. Re-measured when the font or the data changes size.
  useLayoutEffect(() => {
    if (!clipped) return
    const measure = () => {
      const row = bodyRef.current?.rows[0]
      if (row) setRowPx(row.getBoundingClientRect().height || ASSUMED_ROW_PX)
      const head = headRef.current?.getBoundingClientRect().height ?? 0
      const foot = footRef.current?.getBoundingClientRect().height ?? 0
      setChromePx(head + foot)
    }
    measure()
    const observer = new ResizeObserver(measure)
    if (bodyRef.current) observer.observe(bodyRef.current)
    return () => observer.disconnect()
  }, [clipped, rows.length])

  const drag = useRef<{ y: number; rows: number } | null>(null)

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.currentTarget.setPointerCapture(e.pointerId)
      drag.current = { y: e.clientY, rows: visible }
    },
    [visible],
  )

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!drag.current) return
      const moved = Math.round((e.clientY - drag.current.y) / rowPx)
      setChosenRows(drag.current.rows + moved)
    },
    [rowPx],
  )

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    drag.current = null
    e.currentTarget.releasePointerCapture(e.pointerId)
  }, [])

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const step =
        e.key === 'ArrowDown' ? 1
        : e.key === 'ArrowUp' ? -1
        : e.key === 'PageDown' ? 5
        : e.key === 'PageUp' ? -5
        : 0
      if (step) {
        e.preventDefault()
        setChosenRows(visible + step)
      } else if (e.key === 'End') {
        e.preventDefault()
        setChosenRows(rows.length)
      } else if (e.key === 'Home') {
        e.preventDefault()
        setChosenRows(DEFAULT_ROWS)
      }
    },
    [visible, rows.length],
  )

  // A sort change can move an interesting row out of the visible window, and a
  // table scrolled halfway down then re-sorted is showing an arbitrary slice.
  const scrollRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0 })
  }, [sort])

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
    <>
    <div
      className={'table-scroll' + (clipped ? ' clipped' : '')}
      ref={scrollRef}
      style={clipped ? { maxHeight: chromePx + rowPx * visible } : undefined}
    >
      <table className="data">
        <thead ref={headRef}>
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
        <tbody ref={bodyRef}>
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
        <tfoot ref={footRef}>
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
    {/* Only drawn for a table that is actually holding rows back, so it doubles
        as the notice that there are more -- a scrollbar alone is easy to miss
        against a page that scrolls itself. */}
    {clipped && (
      <div
        className="table-resize"
        role="separator"
        aria-orientation="horizontal"
        aria-label="Rows shown"
        aria-valuenow={visible}
        aria-valuemin={MIN_ROWS}
        aria-valuemax={rows.length}
        tabIndex={0}
        title="Drag to show more or fewer rows. Double-click for all of them."
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onKeyDown={onKeyDown}
        onDoubleClick={() =>
          setChosenRows(visible >= rows.length ? DEFAULT_ROWS : rows.length)
        }
      >
        <span className="table-resize-grip" aria-hidden="true" />
        <span className="table-resize-note">
          {visible} of {rows.length}
        </span>
      </div>
    )}
    </>
  )
}
