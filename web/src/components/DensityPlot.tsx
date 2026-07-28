import { useMemo, useState } from 'react'
import { compact, integer } from '../lib/format'
import { seqColor, type Palette } from '../lib/palette'
import type { ScatterGrid } from '../lib/types'
import { ChartTooltip } from './ChartTooltip'

interface Props {
  grid: ScatterGrid
  palette: Palette
  /** Marks the long-context boundary, where the input rate doubles. */
  thresholdTokens?: number
  height?: number
}

/**
 * Per-request cache rate against prompt size, as a density field.
 *
 * A scatter of 100k points is a solid block, so occupancy is binned and encoded
 * on a single-hue sequential ramp. The x axis is log-scaled because prompt
 * sizes span three orders of magnitude.
 */
export function DensityPlot({ grid, palette, thresholdTokens, height = 240 }: Props) {
  const [hover, setHover] = useState<{ x: number; y: number; n: number } | null>(null)
  // State rather than a ref: the tooltip needs the element to measure against,
  // and a ref would not re-render when it arrives.
  const [frame, setFrame] = useState<HTMLDivElement | null>(null)

  const { cells, maxN } = useMemo(() => {
    const max = Math.max(1, ...grid.bins.map((b) => b.n))
    return { cells: grid.bins, maxN: max }
  }, [grid])

  if (!grid.count) return <div className="empty">Nothing in this range</div>

  const size = grid.size
  const logToFrac = (tokens: number) =>
    (Math.log10(Math.max(tokens, 1)) - grid.x_log_min) /
    Math.max(grid.x_log_max - grid.x_log_min, 1e-9)

  const thresholdFrac = thresholdTokens ? logToFrac(thresholdTokens) : null
  // Log scale compresses counts sensibly: most bins hold a handful of requests
  // and a few hold thousands.
  const intensity = (n: number) => Math.log10(n + 1) / Math.log10(maxN + 1)

  const xTicks = [1e3, 1e4, 1e5, 1e6].filter(
    (v) => logToFrac(v) >= 0 && logToFrac(v) <= 1,
  )

  return (
    <div>
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
          {[0, 0.5, 1].map((f) => (
            <div
              key={f}
              style={{
                position: 'absolute',
                right: 4,
                top: `${(1 - f) * 100}%`,
                transform: 'translateY(-50%)',
              }}
            >
              {Math.round(f * 100)}%
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
              {cells.map((b) => (
                <div
                  key={`${b.x}:${b.y}`}
                  style={{
                    position: 'absolute',
                    left: `${(b.x / size) * 100}%`,
                    bottom: `${(b.y / size) * 100}%`,
                    width: `${100 / size}%`,
                    height: `${100 / size}%`,
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
                x={((hover.x + 0.5) / size) * frame.clientWidth}
                y={(1 - (hover.y + 0.5) / size) * frame.clientHeight}
                container={frame}
              >
                <div className="tooltip-row">
                  <span className="name">Prompt</span>
                  <span className="num">
                    ~
                    {compact(
                      10 **
                        (grid.x_log_min +
                          (hover.x / (size - 1)) * (grid.x_log_max - grid.x_log_min)),
                      1,
                    )}
                  </span>
                </div>
                <div className="tooltip-row">
                  <span className="name">Cache</span>
                  <span className="num">
                    {Math.round((hover.y / (size - 1)) * 100)}%
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

      <div className="scale" style={{ marginTop: 4 }}>
        <span>1 request</span>
        <span className="ramp">
          {palette.seq.map((hex, i) => (
            <i key={i} style={{ background: hex }} />
          ))}
        </span>
        <span>{integer(maxN)}</span>
        <span style={{ marginLeft: 8 }}>
          y: cache rate · x: prompt tokens (log) · {integer(grid.count)} requests
        </span>
      </div>
    </div>
  )
}
