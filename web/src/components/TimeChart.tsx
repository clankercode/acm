import { useEffect, useMemo, useRef, useState } from 'react'
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'
import { useElementWidth } from '../lib/live'
import { fill, type Palette } from '../lib/palette'
import { timeLabel } from '../lib/format'
import { ChartTooltip } from './ChartTooltip'
import { ChartLegend } from './ChartLegend'

export interface TimeSeries {
  key: string
  label: string
  color: string
  values: (number | null)[]
  /** Stacked areas for part-to-whole; lines otherwise. Never mixed in one chart. */
  area?: boolean
  dashed?: boolean
}

export interface Marker {
  /** Epoch seconds. */
  t: number
  kind: string
}

interface Props {
  /** Bucket start times, epoch seconds. */
  t: number[]
  series: TimeSeries[]
  palette: Palette
  height?: number
  /** Formats a value for the axis, the tooltip and the legend. */
  format: (v: number | null) => string
  /** Axis label; the chart carries exactly one measure, so one axis. */
  unit?: string
  bucketSeconds: number
  stacked?: boolean
  markers?: Marker[]
  /** Constant reference lines, e.g. undiscounted list price per model. */
  refLines?: { value: number; color: string; label: string }[]
  yMin?: number
  yMax?: number
  /** Shared cursor group; charts in the same group track together. */
  syncKey?: string
  onHoverIndex?: (i: number | null) => void
}

/**
 * Cumulative sums for a stacked area, returned in reverse draw order.
 *
 * Each band is the running total up to and including its series, so band i
 * fully contains band i-1. They must therefore be painted largest first, with
 * each smaller band drawn over the one beneath it -- the visible strip between
 * two adjacent bands is what shows a series' own contribution. Painting them in
 * ascending order instead leaves only the topmost band visible, in the last
 * series' colour.
 */
function stackValues(series: TimeSeries[]): { values: (number | null)[]; series: TimeSeries }[] {
  const bands: { values: (number | null)[]; series: TimeSeries }[] = []
  let running: (number | null)[] | null = null
  for (const s of series) {
    const next = s.values.map((v, i) => {
      const base = running?.[i]
      if (v == null && base == null) return null
      return (base ?? 0) + (v ?? 0)
    })
    bands.push({ values: next, series: s })
    running = next
  }
  return bands.reverse()
}

export function TimeChart({
  t,
  series,
  palette,
  height = 210,
  format,
  unit,
  bucketSeconds,
  stacked = false,
  markers,
  refLines,
  yMin,
  yMax,
  syncKey,
  onHoverIndex,
}: Props) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>()
  const holder = useRef<HTMLDivElement | null>(null)
  const plot = useRef<uPlot | null>(null)
  const [hover, setHover] = useState<{ i: number; left: number; top: number } | null>(null)
  const [hidden, setHidden] = useState<Set<string>>(new Set())

  const visible = useMemo(
    () => series.filter((s) => !hidden.has(s.key)),
    [series, hidden],
  )

  /** Series in canvas draw order, which for a stack is largest band first. */
  const drawn = useMemo(
    () =>
      stacked
        ? stackValues(visible).map((b) => ({ spec: b.series, values: b.values }))
        : visible.map((s) => ({ spec: s, values: s.values })),
    [visible, stacked],
  )

  const data = useMemo(
    () => [t, ...drawn.map((d) => d.values)] as uPlot.AlignedData,
    [t, drawn],
  )

  useEffect(() => {
    if (!holder.current || width <= 0) return

    const opts: uPlot.Options = {
      width,
      height,
      padding: [10, 12, 0, 0],
      cursor: {
        x: true,
        y: false,
        points: { size: 7, width: 2, fill: () => palette.surface },
        drag: { x: true, y: false, setScale: false },
        ...(syncKey ? { sync: { key: syncKey, setSeries: false } } : {}),
      },
      legend: { show: false },
      scales: {
        x: { time: true },
        y: {
          // Reference lines never widen the range. Letting a list-price ceiling
          // set the top would squash the actual data -- which is heavily
          // discounted by caching -- into the bottom sliver of the plot.
          range: (_u, dataMin, dataMax) => {
            const lo = yMin ?? Math.min(dataMin, 0)
            const hi = yMax ?? dataMax
            if (!isFinite(lo) || !isFinite(hi) || lo === hi) return [0, 1]
            return [lo, hi + (hi - lo) * 0.08]
          },
        },
      },
      axes: [
        {
          stroke: palette.textMuted,
          grid: { stroke: palette.grid, width: 1 },
          ticks: { stroke: palette.axis, width: 1, size: 4 },
          font: '11px system-ui, sans-serif',
          // Tall enough for uPlot's two-tier time axis; at 30 the date row
          // underneath the times is clipped.
          size: 44,
          space: 70,
        },
        {
          stroke: palette.textMuted,
          grid: { stroke: palette.grid, width: 1 },
          ticks: { show: false },
          font: '11px system-ui, sans-serif',
          size: 52,
          label: unit,
          labelSize: unit ? 16 : 0,
          labelFont: '10px system-ui, sans-serif',
          values: (_u, splits) => splits.map((v) => format(v)),
        },
      ],
      series: [
        {},
        ...drawn.map(({ spec }): uPlot.Series => ({
          label: spec.label,
          // A 2px surface-coloured edge separates adjacent stacked bands
          // instead of a border drawn around each one.
          stroke: stacked ? palette.surface : spec.color,
          width: stacked ? 2 : 2,
          fill: spec.area ? (stacked ? spec.color : fill(spec.color, 0.14)) : undefined,
          dash: spec.dashed ? [4, 3] : undefined,
          points: { show: false },
          spanGaps: false,
        })),
      ],
      hooks: {
        draw: [
          (u) => {
            const ctx = u.ctx
            const { left, top, width: w, height: h } = u.bbox

            // Only draw a reference line that lands inside the plotted range.
            // One outside it would otherwise be pinned to an edge and read as
            // data.
            for (const r of refLines ?? []) {
              const y = u.valToPos(r.value, 'y', true)
              if (!isFinite(y) || y < top || y > top + h) continue
              ctx.save()
              ctx.strokeStyle = r.color
              ctx.globalAlpha = 0.5
              ctx.lineWidth = 1
              ctx.setLineDash([3, 4])
              ctx.beginPath()
              ctx.moveTo(left, y)
              ctx.lineTo(left + w, y)
              ctx.stroke()
              ctx.restore()
            }

            // Event markers are a rug along the baseline, not full-height
            // rules. There can be hundreds of compactions in a week, and
            // full-height rules turn into an opaque curtain over the data.
            if (markers?.length) {
              ctx.save()
              ctx.strokeStyle = palette.textMuted
              ctx.globalAlpha = 0.55
              ctx.lineWidth = 1
              const tick = 7
              let last = -Infinity
              for (const m of markers) {
                const x = Math.round(u.valToPos(m.t, 'x', true)) + 0.5
                if (x < left || x > left + w) continue
                // Collapse marks closer than a pixel so a dense burst reads as
                // a solid band of the right width rather than overdrawing.
                if (x - last < 1) continue
                last = x
                ctx.beginPath()
                ctx.moveTo(x, top + h)
                ctx.lineTo(x, top + h - tick)
                ctx.stroke()
              }
              ctx.restore()
            }
          },
        ],
        setCursor: [
          (u) => {
            const i = u.cursor.idx
            if (i == null || u.cursor.left == null || u.cursor.left < 0) {
              setHover(null)
              onHoverIndex?.(null)
              return
            }
            setHover({ i, left: u.cursor.left, top: u.cursor.top ?? 0 })
            onHoverIndex?.(i)
          },
        ],
      },
    }

    const instance = new uPlot(opts, data, holder.current)
    plot.current = instance
    return () => {
      instance.destroy()
      plot.current = null
    }
    // Rebuilt on theme/shape changes; data-only updates go through setData below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [width, height, palette, drawn.map((d) => d.spec.key + d.spec.color).join('|'), stacked, unit, markers, refLines])

  useEffect(() => {
    plot.current?.setData(data)
  }, [data])

  const hasData = t.length > 0 && series.some((s) => s.values.some((v) => v != null))

  const tooltip = hover && hasData ? renderTooltip(hover.i) : null

  function renderTooltip(i: number) {
    const rows = series
      .filter((s) => !hidden.has(s.key))
      .map((s) => ({ s, v: s.values[i] }))
      .filter((r) => r.v != null)
      .sort((a, b) => (b.v as number) - (a.v as number))
    if (!rows.length) return null
    const total = stacked ? rows.reduce((acc, r) => acc + (r.v as number), 0) : null
    return (
      <ChartTooltip x={hover!.left} y={hover!.top} container={wrapRef.current}>
        <div className="tooltip-time">{timeLabel(t[i] * 1000, bucketSeconds)}</div>
        {rows.slice(0, 9).map(({ s, v }) => (
          <div className="tooltip-row" key={s.key}>
            <span className="swatch" style={{ background: s.color }} />
            <span className="name">{s.label}</span>
            <span className="num">{format(v as number)}</span>
          </div>
        ))}
        {total != null && rows.length > 1 && (
          <div className="tooltip-row tooltip-total">
            <span className="name">Total</span>
            <span className="num">{format(total)}</span>
          </div>
        )}
      </ChartTooltip>
    )
  }

  const legendItems = useMemo(
    () =>
      series.map((s) => {
        const last = lastValue(s.values)
        return {
          key: s.key,
          label: s.label,
          color: s.color,
          value: last == null ? undefined : format(last),
          hidden: hidden.has(s.key),
        }
      }),
    // `format` is a fresh closure on most renders, so it is deliberately not a
    // dependency: it is keyed to the chart's unit, which does not change under a
    // mounted chart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [series, hidden],
  )

  const toggle = (key: string) =>
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })

  return (
    <div>
      {/* Drawn even for a single series, and always the same height: two charts
          side by side whose legends differ in length would otherwise start their
          plots at different heights. */}
      <ChartLegend
        items={legendItems}
        onToggle={toggle}
        onOnly={(key) => setHidden(new Set(series.filter((s) => s.key !== key).map((s) => s.key)))}
        onShowAll={() => setHidden(new Set())}
      />
      <div className="chart-wrap" ref={wrapRef}>
        {hasData ? (
          <>
            <div ref={holder} />
            {tooltip}
          </>
        ) : (
          <div className="empty" style={{ minHeight: height }}>
            No data in this range
          </div>
        )}
      </div>
    </div>
  )
}

function lastValue(values: (number | null)[]): number | null {
  for (let i = values.length - 1; i >= 0; i--) {
    if (values[i] != null) return values[i] as number
  }
  return null
}
