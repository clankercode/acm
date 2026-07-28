import { useMemo, useRef, useState } from 'react'
import { compact, pct, shortModel, usd } from '../lib/format'
import { seqColor, type Palette } from '../lib/palette'
import type { CalendarDay } from '../lib/types'
import { ChartTooltip } from './ChartTooltip'

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

export type CalendarMetric = 'input_tokens' | 'cost' | 'cache_rate' | 'output_tokens'

const LABELS: Record<CalendarMetric, string> = {
  input_tokens: 'prompt tokens',
  cost: 'spend',
  cache_rate: 'cache hit rate',
  output_tokens: 'output tokens',
}

interface Props {
  days: CalendarDay[]
  palette: Palette
  metric: CalendarMetric
}

/** Local calendar date for a day index, without tripping over time zones. */
function toDate(dayIndex: number): Date {
  return new Date(dayIndex * 86400e3)
}

function monthKey(d: Date): number {
  return d.getUTCFullYear() * 12 + d.getUTCMonth()
}

/**
 * A month at a time, coloured by how much went through it.
 *
 * The weekday heatmap answers "when in the week"; this answers "which days",
 * which is the shape you actually recognise -- a week off, a heavy Sunday, the
 * day a long-running job ran. Deliberately shows all history rather than the
 * selected range, since the month arrows are its own time control.
 */
export function Calendar({ days, palette, metric }: Props) {
  const box = useRef<HTMLDivElement | null>(null)
  const [hover, setHover] = useState<{ day: CalendarDay; x: number; y: number } | null>(
    null,
  )

  const byDay = useMemo(() => new Map(days.map((d) => [d.day, d])), [days])
  const months = useMemo(() => {
    const seen = new Set<number>()
    for (const d of days) seen.add(monthKey(toDate(d.day)))
    return [...seen].sort((a, b) => a - b)
  }, [days])

  // Opens on the newest month with traffic, which is the one being asked about.
  const [month, setMonth] = useState<number | null>(null)
  const current = month ?? months[months.length - 1] ?? monthKey(new Date())

  const value = (d: CalendarDay) =>
    metric === 'cost' ? d.cost
    : metric === 'cache_rate' ? d.cache_rate
    : metric === 'output_tokens' ? d.output_tokens
    : d.input_tokens

  const grid = useMemo(() => {
    const year = Math.floor(current / 12)
    const m = current % 12
    // Day indices are UTC-midnight instants standing for local dates, so the
    // whole grid is built with the UTC accessors and never shifts a date.
    const first = Date.UTC(year, m, 1) / 86400e3
    const length = new Date(Date.UTC(year, m + 1, 0)).getUTCDate()
    const lead = new Date(Date.UTC(year, m, 1)).getUTCDay()

    const cells: { date: number | null; day: CalendarDay | null }[] = Array.from(
      { length: lead },
      () => ({ date: null, day: null }),
    )
    let lo = Infinity
    let hi = -Infinity
    const total = { input: 0, cost: 0, cached: 0, requests: 0 }
    for (let i = 0; i < length; i++) {
      const day = byDay.get(first + i) ?? null
      cells.push({ date: i + 1, day })
      if (!day) continue
      const v = value(day)
      lo = Math.min(lo, v)
      hi = Math.max(hi, v)
      total.input += day.input_tokens
      total.cost += day.cost
      total.cached += day.cached_tokens
      total.requests += day.requests
    }
    while (cells.length % 7) cells.push({ date: null, day: null })
    return {
      cells,
      label: `${MONTHS[m]} ${year}`,
      min: isFinite(lo) ? lo : 0,
      max: isFinite(hi) ? hi : 1,
      total,
    }
    // `value` is derived from `metric`, which is already a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [byDay, current, metric])

  /**
   * Where a day sits on the ramp, in [0, 1].
   *
   * Volumes are measured from zero, so a quiet day looks quiet rather than
   * being stretched to mid-ramp. A cache *rate* is not a volume -- every day
   * is somewhere in the high nineties — so that one is scaled across the
   * observed band or the whole month is one flat colour.
   */
  const level = (d: CalendarDay) => {
    if (metric === 'cache_rate') {
      return (value(d) - grid.min) / (grid.max - grid.min || 1)
    }
    return Math.min(1, Math.max(0, value(d) / (grid.max || 1)))
  }

  // The ramp runs from near-canvas to far-from-canvas in both themes, so the
  // ink that contrasts with a fill follows the value rather than the theme.
  const ink = (t: number) => (t > 0.5 ? 'var(--page)' : 'var(--text-primary)')

  // Stepping by calendar month rather than by months-that-have-data: a gap
  // month showing as empty is the honest answer, and skipping it silently
  // would misrepresent the shape of the history.
  const earliest = months[0] ?? current
  const latest = months[months.length - 1] ?? current
  const step = (delta: number) =>
    setMonth(Math.min(latest, Math.max(earliest, current + delta)))

  return (
    <div>
      {/* Above the grid and the same height as a legend, so a calendar lines up
          with whatever chart shares its row. */}
      <div className="chart-scale">
        <div className="scale">
          {/* Volumes are measured from zero; a rate is measured across the band
              it actually occupies, so the low end has to say which. */}
          <span>{metric === 'cache_rate' ? pct(grid.min, 1) : 'none'}</span>
          <span className="ramp">
            {palette.seq.map((hex, i) => (
              <i key={i} style={{ background: hex }} />
            ))}
          </span>
          <span>
            {metric === 'cost'
              ? usd(grid.max)
              : metric === 'cache_rate'
                ? pct(grid.max, 1)
                : compact(grid.max)}
          </span>
          <span style={{ marginLeft: 6 }}>{LABELS[metric]} per day</span>
          <span className="cal-cell quiet" style={{ marginLeft: 10 }} />
          <span>no traffic</span>
        </div>
      </div>

      <div className="cal-head">
        <div className="seg" role="group" aria-label="Month">
          <button
            type="button"
            onClick={() => step(-1)}
            disabled={current <= earliest}
            aria-label="Previous month"
          >
            ‹
          </button>
          <button
            type="button"
            onClick={() => step(1)}
            disabled={current >= latest}
            aria-label="Next month"
          >
            ›
          </button>
        </div>
        <span className="cal-month">{grid.label}</span>
        <span className="scan-meta">
          {compact(grid.total.input)} tokens · {usd(grid.total.cost)} ·{' '}
          {pct(grid.total.input ? grid.total.cached / grid.total.input : 0, 1)} cached
        </span>
      </div>

      <div className="cal-weekdays">
        {WEEKDAYS.map((d) => (
          <span key={d}>{d}</span>
        ))}
      </div>

      <div className="cal-grid" ref={box}>
        {grid.cells.map(({ date, day }, i) => {
          const t = day ? level(day) : 0
          return (
            <div
              key={i}
              className={'cal-cell' + (day ? '' : date ? ' quiet' : ' outside')}
              style={day ? { background: seqColor(palette, t) } : undefined}
              onMouseMove={(e) => {
                if (!day || !box.current) return
                const r = box.current.getBoundingClientRect()
                setHover({ day, x: e.clientX - r.left, y: e.clientY - r.top })
              }}
              onMouseLeave={() => setHover(null)}
            >
              {date && (
                <span className="cal-date" style={day ? { color: ink(t) } : undefined}>
                  {date}
                </span>
              )}
            </div>
          )
        })}

        {hover && (
          <ChartTooltip x={hover.x} y={hover.y} container={box.current}>
            <div className="tooltip-time">
              {toDate(hover.day.day).toUTCString().slice(0, 16)}
            </div>
            <div className="tooltip-row">
              <span className="name">Prompt tokens</span>
              <span className="num">{compact(hover.day.input_tokens)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">Requests</span>
              <span className="num">{compact(hover.day.requests, 0)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">Cache</span>
              <span className="num">{pct(hover.day.cache_rate, 1)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">Cost</span>
              <span className="num">{usd(hover.day.cost)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">$/Mtok</span>
              <span className="num">{usd(hover.day.effective_rate, 4)}</span>
            </div>
            {hover.day.top.length > 0 && <div className="tooltip-rule" />}
            {hover.day.top.map((m) => (
              <div className="tooltip-row" key={m.key}>
                <span className="name">{shortModel(m.key)}</span>
                <span className="num">{usd(m.cost)}</span>
              </div>
            ))}
          </ChartTooltip>
        )}
      </div>

    </div>
  )
}
