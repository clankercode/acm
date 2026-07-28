import { useMemo, useState } from 'react'
import { compact, integer, pct } from '../lib/format'
import { seqColor, type Palette } from '../lib/palette'
import type { ScatterPoint } from '../lib/types'
import { ChartTooltip } from './ChartTooltip'

const BINS = 32

/**
 * How the y-axis maps a scatter point to a [0,1] fraction.
 *
 * Cache rate and effective rate are pre-normalised ratios that fit [0,1]
 * directly. Cost and output are magnitudes that span orders of magnitude, so
 * they are log-scaled against the observed maximum to avoid one giant bucket
 * squashing everything else into the bottom row.
 */
export interface DensityMetric {
  key: string
  /** What appears in the panel title and the legend. */
  label: string
  extract: (p: ScatterPoint) => number
  format: (v: number) => string
  /** Whether the y value is already in [0,1] (rate) or needs log-scaling. */
  scale: 'rate' | 'volume'
  /** Y-axis label. */
  axisLabel: string
}

interface Props {
  points: ScatterPoint[]
  xLogMin: number
  xLogMax: number
  count: number
  palette: Palette
  /** Marks the long-context boundary, where the input rate doubles. */
  thresholdTokens?: number
  metric: DensityMetric
  height?: number
}

/**
 * Per-request density against prompt size, binned client-side from the
 * per-bucket scatter points. The server returns one weighted point per hourly
 * rollup bucket (so imported machines, which have no per-request data, still
 * contribute), and this component bins them into a 2D grid and encodes
 * occupancy on a sequential ramp.
 */
export function DensityPlot({
  points,
  xLogMin,
  xLogMax,
  count,
  palette,
  thresholdTokens,
  metric,
  height = 240,
}: Props) {
  const [hover, setHover] = useState<{ x: number; y: number; n: number } | null>(null)
  // State rather than a ref: the tooltip needs the element to measure against,
  // and a ref would not re-render when it arrives.
  const [frame, setFrame] = useState<HTMLDivElement | null>(null)

  const { bins, maxN, yLo, yHi } = useMemo(() => {
    if (!points.length)
      return { bins: [] as { x: number; y: number; n: number }[], maxN: 0, yLo: 0, yHi: 1 }
    // Determine the y-range for volume metrics: log-scale against the max.
    let lo = Infinity
    let hi = -Infinity
    if (metric.scale === 'volume') {
      for (const p of points) {
        const v = metric.extract(p)
        if (v <= 0) continue
        lo = Math.min(lo, v)
        hi = Math.max(hi, v)
      }
      if (!isFinite(lo)) lo = 0.01
      if (!isFinite(hi)) hi = 1
    } else {
      lo = 0
      hi = 1
    }
    const logYLo = Math.log10(Math.max(lo, 1e-9))
    const logYHi = Math.log10(Math.max(hi, 1e-9))
    const ySpan = Math.max(logYHi - logYLo, 1e-9)

    const grid: Record<string, number> = {}
    let max = 0
    for (const p of points) {
      const xBin = Math.min(BINS - 1, Math.max(0, Math.round(p.x * (BINS - 1))))
      const raw = metric.extract(p)
      let yFrac: number
      if (metric.scale === 'rate') {
        yFrac = raw
      } else {
        yFrac = raw <= 0 ? 0 : (Math.log10(raw) - logYLo) / ySpan
      }
      const yBin = Math.min(BINS - 1, Math.max(0, Math.round(yFrac * (BINS - 1))))
      const key = `${xBin}:${yBin}`
      const n = (grid[key] ?? 0) + p.n
      grid[key] = n
      if (n > max) max = n
    }
    const out = Object.entries(grid).map(([k, n]) => {
      const [x, y] = k.split(':').map(Number)
      return { x, y, n }
    })
    return { bins: out, maxN: Math.max(1, max), yLo: lo, yHi: hi }
  }, [points, metric])

  if (!count) return <div className="empty">Nothing in this range</div>

  const logToFrac = (tokens: number) =>
    (Math.log10(Math.max(tokens, 1)) - xLogMin) / Math.max(xLogMax - xLogMin, 1e-9)

  const thresholdFrac = thresholdTokens ? logToFrac(thresholdTokens) : null
  // Log scale compresses counts sensibly: most bins hold a handful of requests
  // and a few hold thousands.
  const intensity = (n: number) => Math.log10(n + 1) / Math.log10(maxN + 1)

  const xTicks = [1e3, 1e4, 1e5, 1e6].filter(
    (v) => logToFrac(v) >= 0 && logToFrac(v) <= 1,
  )

  // Y-axis tick labels. Rates map linearly; volumes are log-scaled so the
  // bottom tick is yLo and the top is yHi. `frac` runs bottom (0) to top (1),
  // matching how the cells are positioned via `bottom:`.
  const logYLo = Math.log10(Math.max(yLo, 1e-9))
  const logYHi = Math.log10(Math.max(yHi, 1e-9))
  const yTicks =
    metric.scale === 'rate'
      ? [0, 0.5, 1].map((f) => ({ frac: f, label: `${Math.round(f * 100)}%` }))
      : [0, 0.5, 1].map((f) => ({
          frac: f,
          label: metric.format(10 ** (logYLo + f * (logYHi - logYLo))),
        }))

  // Reverse-maps a y-bin index to the metric value it represents, for tooltips.
  const yBinToValue = (yBin: number) =>
    metric.scale === 'rate'
      ? yBin / (BINS - 1)
      : 10 ** (logYLo + (yBin / (BINS - 1)) * (logYHi - logYLo))

  return (
    <div>
      {/* Above the plot and the same height as a legend, so this lines up with
          the charts beside it. */}
      <div className="chart-scale">
        <div className="scale">
          <span>1 request</span>
          <span className="ramp">
            {palette.seq.map((hex, i) => (
              <i key={i} style={{ background: hex }} />
            ))}
          </span>
          <span>{integer(maxN)}</span>
          <span style={{ marginLeft: 8 }}>
            y: {metric.axisLabel} · x: prompt tokens (log) · {integer(count)} requests
          </span>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 6 }}>
        <div
          style={{
            width: 34,
            height,
            position: 'relative',
            fontSize: 10,
            color: 'var(--text-muted)',
          }}
        >
          {yTicks.map((t) => (
            <div
              key={t.frac}
              style={{
                position: 'absolute',
                right: 4,
                top: `${(1 - t.frac) * 100}%`,
                transform: 'translateY(-50%)',
              }}
            >
              {t.label}
            </div>
          ))}
        </div>

        <div style={{ flex: 1, minWidth: 0 }}>
          {/* Two layers on purpose: the cells clip to the plot's rounded box,
              the tooltip must not. */}
          <div
            className="density-frame"
            ref={setFrame}
            style={{ height }}
            onMouseLeave={() => setHover(null)}
          >
            <div className="density-cells">
              {bins.map((b) => (
                <div
                  key={`${b.x}:${b.y}`}
                  style={{
                    position: 'absolute',
                    left: `${(b.x / BINS) * 100}%`,
                    bottom: `${(b.y / BINS) * 100}%`,
                    width: `${100 / BINS}%`,
                    height: `${100 / BINS}%`,
                    background: seqColor(palette, intensity(b.n)),
                  }}
                  onMouseEnter={() => setHover(b)}
                />
              ))}

              {thresholdFrac != null && thresholdFrac > 0 && thresholdFrac < 1 && (
                <div
                  className="density-threshold"
                  style={{ left: `${thresholdFrac * 100}%` }}
                  title="Long-context pricing threshold"
                >
                  <span>long ctx →</span>
                </div>
              )}
            </div>

            {hover && frame && (
              <ChartTooltip
                x={((hover.x + 0.5) / BINS) * frame.clientWidth}
                y={(1 - (hover.y + 0.5) / BINS) * frame.clientHeight}
                container={frame}
              >
                <div className="tooltip-row">
                  <span className="name">Prompt</span>
                  <span className="num">
                    ~
                    {compact(
                      10 ** (xLogMin + (hover.x / (BINS - 1)) * (xLogMax - xLogMin)),
                      1,
                    )}
                  </span>
                </div>
                <div className="tooltip-row">
                  <span className="name">{metric.label}</span>
                  <span className="num">
                    {metric.scale === 'rate'
                      ? pct(yBinToValue(hover.y), 0)
                      : metric.format(yBinToValue(hover.y))}
                  </span>
                </div>
                <div className="tooltip-row">
                  <span className="name">Requests</span>
                  <span className="num">{integer(hover.n)}</span>
                </div>
              </ChartTooltip>
            )}
          </div>

          <div
            style={{
              position: 'relative',
              height: 16,
              fontSize: 10,
              color: 'var(--text-muted)',
            }}
          >
            {xTicks.map((v) => (
              <span
                key={v}
                style={{
                  position: 'absolute',
                  left: `${logToFrac(v) * 100}%`,
                  transform: 'translateX(-50%)',
                  paddingTop: 2,
                }}
              >
                {compact(v, 0)}
              </span>
            ))}
          </div>
        </div>
      </div>

    </div>
  )
}
