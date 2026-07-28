import { useMemo, useState } from 'react'
import { compact, pct, rate, usd } from '../lib/format'
import { seqColor, type Palette } from '../lib/palette'
import type { HeatCell } from '../lib/types'

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

interface Props {
  cells: HeatCell[]
  palette: Palette
  /** Continuous magnitude to encode, in [0, 1] after normalisation. */
  metric: 'cache_rate' | 'effective_rate'
}

/**
 * Weekday x hour-of-day grid. A single-hue sequential ramp encodes magnitude,
 * with a scale legend; cells with no traffic stay at surface colour rather than
 * reading as a zero.
 */
export function Heatmap({ cells, palette, metric }: Props) {
  const [hover, setHover] = useState<HeatCell | null>(null)

  const { grid, min, max } = useMemo(() => {
    const g = new Map<string, HeatCell>()
    let lo = Infinity
    let hi = -Infinity
    for (const c of cells) {
      g.set(`${c.day}:${c.hour}`, c)
      const v = metric === 'cache_rate' ? c.cache_rate : c.effective_rate
      if (!isFinite(v)) continue
      lo = Math.min(lo, v)
      hi = Math.max(hi, v)
    }
    return { grid: g, min: isFinite(lo) ? lo : 0, max: isFinite(hi) ? hi : 1 }
  }, [cells, metric])

  if (!cells.length) return <div className="empty">Nothing in this range</div>

  const span = max - min || 1
  // Cache rate reads better inverted: darker means more cache misses, which is
  // the thing worth spotting.
  const normalise = (c: HeatCell) => {
    const v = metric === 'cache_rate' ? c.cache_rate : c.effective_rate
    const t = (v - min) / span
    return metric === 'cache_rate' ? 1 - t : t
  }

  return (
    <div>
      {/* Above the plot and the same height as a legend, so this chart starts its
          grid at the same y as a line chart beside it. */}
      <div className="chart-scale">
        <div className="scale">
          <span>{metric === 'cache_rate' ? 'best cache' : 'cheapest'}</span>
          <span className="ramp">
            {palette.seq.map((hex, i) => (
              <i key={i} style={{ background: hex }} />
            ))}
          </span>
          <span>{metric === 'cache_rate' ? 'worst cache' : 'dearest'}</span>
          <span style={{ marginLeft: 6 }}>
            {metric === 'cache_rate'
              ? `(${pct(min, 0)} – ${pct(max, 0)})`
              : `(${rate(min)} – ${rate(max)})`}
          </span>
          <span
            className="heat-cell empty"
            style={{ width: 10, minHeight: 10, marginLeft: 10 }}
          />
          <span>no traffic</span>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 6, position: 'relative' }}>
        <div
          style={{
            display: 'grid',
            gridTemplateRows: 'repeat(7, 1fr)',
            gap: 2,
            fontSize: 10,
            color: 'var(--text-muted)',
            paddingTop: 14,
          }}
        >
          {DAYS.map((d) => (
            <div key={d} style={{ display: 'grid', placeItems: 'center end', minHeight: 12 }}>
              {d}
            </div>
          ))}
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(24, 1fr)',
              gap: 2,
              fontSize: 9.5,
              color: 'var(--text-muted)',
              marginBottom: 2,
            }}
          >
            {Array.from({ length: 24 }, (_, h) => (
              <div key={h} style={{ textAlign: 'center' }}>
                {h % 3 === 0 ? h : ''}
              </div>
            ))}
          </div>
          <div className="heat-grid" style={{ gridTemplateColumns: 'repeat(24, 1fr)' }}>
            {Array.from({ length: 7 * 24 }, (_, i) => {
              const day = Math.floor(i / 24)
              const hour = i % 24
              const cell = grid.get(`${day}:${hour}`)
              return (
                <div
                  key={i}
                  className={'heat-cell' + (cell ? '' : ' empty')}
                  style={
                    cell ? { background: seqColor(palette, normalise(cell)) } : undefined
                  }
                  onMouseEnter={() => cell && setHover(cell)}
                  onMouseLeave={() => setHover(null)}
                  title={
                    cell
                      ? `${DAYS[day]} ${String(hour).padStart(2, '0')}:00 — ${pct(cell.cache_rate, 1)} cache`
                      : `${DAYS[day]} ${String(hour).padStart(2, '0')}:00 — no traffic`
                  }
                />
              )
            })}
          </div>
        </div>

        {hover && (
          <div className="tooltip" style={{ right: 0, top: 0 }}>
            <div className="tooltip-time">
              {DAYS[hover.day]} {String(hover.hour).padStart(2, '0')}:00
            </div>
            <div className="tooltip-row">
              <span className="name">Cache</span>
              <span className="num">{pct(hover.cache_rate, 1)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">$/Mtok</span>
              <span className="num">{rate(hover.effective_rate)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">Input</span>
              <span className="num">{compact(hover.input_tokens)}</span>
            </div>
            <div className="tooltip-row">
              <span className="name">Cost</span>
              <span className="num">{usd(hover.cost)}</span>
            </div>
          </div>
        )}
      </div>

    </div>
  )
}
