import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { compact, integer, pct, rate, usd } from '../lib/format'
import type { ColorScale } from '../lib/palette'
import type { BreakdownRow } from '../lib/types'

/** Rows shown before a table starts scrolling instead of growing the page. */
const DEFAULT_ROWS = 15

/**
 * Extra rows a table may exceed the cap by and still be drawn whole.
 *
 * Clipping 17 rows down to 15 trades two rows of page height for a scrollbar
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
  /** The footer cell. Given every row, because a rate is a weighted ratio of two
   *  sums rather than the sum of the column -- averaging the column would let a
   *  one-request model drag the total around. */
  total: (rows: BreakdownRow[]) => string
  /** Draws a proportional bar behind the cell, scaled to the column max. */
  bar?: boolean
  title?: string
  /** Tighter than the input columns. The output group is supporting detail, and
   *  three more full-width columns push the input ones off a narrow panel. */
  narrow?: boolean
}

const sum = (rows: BreakdownRow[], key: keyof BreakdownRow) =>
  rows.reduce((a, r) => a + (Number(r[key]) || 0), 0)

/** Per-million rate over a whole column: total cost against total tokens. */
const ratio = (cost: number, tokens: number) => (tokens > 0 ? cost / (tokens / 1e6) : 0)

const COLUMNS: Col[] = [
  {
    key: 'requests',
    label: 'Reqs',
    render: (r) => integer(r.requests),
    total: (rows) => integer(sum(rows, 'requests')),
  },
  {
    key: 'input_tokens',
    label: 'Input',
    render: (r) => compact(r.input_tokens),
    total: (rows) => compact(sum(rows, 'input_tokens')),
    bar: true,
  },
  {
    key: 'cache_rate',
    label: 'Cache',
    render: (r) => pct(r.cache_rate, 1),
    total: (rows) => {
      const input = sum(rows, 'input_tokens')
      return pct(input ? sum(rows, 'cached_tokens') / input : 0, 1)
    },
    title: 'Cached share of prompt tokens',
  },
  {
    key: 'cost',
    label: 'Cost',
    render: (r) => usd(r.cost),
    total: (rows) => usd(sum(rows, 'cost')),
    bar: true,
  },
  {
    key: 'saved',
    label: 'Saved',
    render: (r) => usd(r.saved),
    total: (rows) => usd(sum(rows, 'saved')),
  },
  {
    key: 'effective_rate',
    label: '$/Mtok',
    render: (r) => rate(r.effective_rate),
    total: (rows) => rate(ratio(sum(rows, 'cost'), sum(rows, 'input_tokens'))),
    bar: true,
    title: 'Cost per million input tokens processed. Lower is better.',
  },
  {
    key: 'efficiency',
    label: 'Of list',
    render: (r) => pct(r.efficiency, 0),
    total: (rows) => {
      const uncached = sum(rows, 'uncached_cost')
      return pct(uncached ? sum(rows, 'cost') / uncached : 0, 0)
    },
    title: 'Share of the undiscounted list price actually paid',
  },
]

/**
 * Generation, kept to the right and off by default.
 *
 * Deliberately a group rather than three more columns in the run: output is a
 * different quantity from prompt tokens -- never cached, billed several times as
 * dearly, and a fraction of the volume -- so mixing the two runs invites reading
 * `$/Mtok` and `$/M out` as comparable numbers, which they are not.
 */
const OUTPUT_COLUMNS: Col[] = [
  {
    key: 'output_tokens',
    label: 'Out',
    render: (r) => compact(r.output_tokens),
    total: (rows) => compact(sum(rows, 'output_tokens')),
    bar: true,
    narrow: true,
    title: 'Tokens generated, reasoning included',
  },
  {
    key: 'cost_output',
    label: 'Out $',
    render: (r) => usd(r.cost_output),
    total: (rows) => usd(sum(rows, 'cost_output')),
    narrow: true,
    title: 'The part of the bill that paid for generation',
  },
  {
    key: 'output_rate',
    label: '$/M out',
    // No generation, or generation on a model with no rate in the table, both
    // arrive here as a zero that would read as free output. There is no answer
    // in either case, so say so rather than print one.
    render: (r) => (r.output_tokens && r.cost_output ? rate(r.output_rate) : '—'),
    total: (rows) => {
      const r = ratio(sum(rows, 'cost_output'), sum(rows, 'output_tokens'))
      return r ? rate(r) : '—'
    },
    narrow: true,
    title:
      'Cost per million output tokens. Effectively the model’s list output' +
      ' price, since output is never cached — so it reads as model mix,' +
      ' not as caching.',
  },
]

interface Props {
  rows: BreakdownRow[]
  label: (key: string) => string
  colors?: ColorScale
  /** Marks rows worth attention, e.g. a route with an unusually poor cache rate. */
  flag?: (row: BreakdownRow) => string | null
  onSelect?: (key: string) => void
  /** Reveals the output group. Driven from the top bar, so every table on the
   *  page shows the same columns. */
  showOutput?: boolean
}

export function BreakdownTable({ rows, label, colors, flag, onSelect, showOutput }: Props) {
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

  const columns = useMemo(
    () => (showOutput ? [...COLUMNS, ...OUTPUT_COLUMNS] : COLUMNS),
    [showOutput],
  )

  const maxima = useMemo(() => {
    const m: Partial<Record<keyof BreakdownRow, number>> = {}
    for (const col of columns) {
      if (!col.bar) continue
      m[col.key] = Math.max(...rows.map((r) => Number(r[col.key]) || 0), 0)
    }
    return m
  }, [rows, columns])

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

  // The rule that separates the two groups belongs to the first output cell, so
  // it lands in exactly one place per row whether or not the group is shown.
  const cellClass = (col: Col, extra?: string) =>
    [
      extra,
      col.narrow ? 'out' : null,
      col === OUTPUT_COLUMNS[0] ? 'group-start' : null,
    ]
      .filter(Boolean)
      .join(' ') || undefined

  const header = (col: Col) => (
    <th
      key={String(col.key)}
      className={cellClass(col)}
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
          {/* Named once above the group rather than in each of its three labels,
              which would otherwise all have to carry "out". */}
          {showOutput && (
            <tr className="group-row">
              {/* A plain cell, not a header: an empty `th` is announced as a
                  column header with no name. */}
              <td colSpan={1 + COLUMNS.length} />
              <th
                className="group-start"
                scope="colgroup"
                colSpan={OUTPUT_COLUMNS.length}
              >
                Output
              </th>
            </tr>
          )}
          <tr>
            <th
              aria-sort={sort.key === 'key' ? (sort.desc ? 'descending' : 'ascending') : undefined}
              onClick={() =>
                setSort((s) => (s.key === 'key' ? { key: 'key', desc: !s.desc } : { key: 'key', desc: false }))
              }
            >
              Name
            </th>
            {columns.map(header)}
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
                {columns.map((col) => {
                  const max = maxima[col.key]
                  const value = Number(row[col.key]) || 0
                  return (
                    <td key={String(col.key)} className={cellClass(col, col.bar ? 'bar-cell' : undefined)}>
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
            {/* Driven off the same list as the header, so a column can never be
                added on one row and forgotten on the other. */}
            {columns.map((col) => (
              <td key={String(col.key)} className={cellClass(col)}>
                {col.total(rows)}
              </td>
            ))}
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
