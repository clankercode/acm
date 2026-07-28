import { useEffect, useMemo, useState } from 'react'
import { api } from './lib/api'
import { compact, pct, rate, repoLabel, shortModel, usd } from './lib/format'
import { filterKey, useLiveState, useOutputView, useQuery, useTheme } from './lib/live'
import { ColorScale, SLOT_COUNT, readPalette } from './lib/palette'
import {
  EMPTY_FILTERS,
  sourceLabel,
  type Filters,
  type SeriesResponse,
} from './lib/types'
import { BreakdownTable } from './components/BreakdownTable'
import { Calendar, type CalendarMetric } from './components/Calendar'
import { DataQuality } from './components/DataQuality'
import { DensityPlot } from './components/DensityPlot'
import { Heatmap } from './components/Heatmap'
import { KpiStrip } from './components/KpiStrip'
import { Machines } from './components/Machines'
import { PricingEditor } from './components/PricingEditor'
import { ReferencePrices } from './components/ReferencePrices'
import { SessionExplorer } from './components/SessionExplorer'
import { TimeChart, type TimeSeries } from './components/TimeChart'
import { RANGES, TopBar, type RangeKey } from './components/TopBar'

/** Derived per-bucket metrics. Nulls propagate so idle gaps stay gaps. */
function metric(
  cols: {
    cost: (number | null)[]
    input: (number | null)[]
    cached: (number | null)[]
    written: (number | null)[]
    uncached: (number | null)[]
    n: (number | null)[]
    output?: (number | null)[]
    cost_output?: (number | null)[]
  },
  kind:
    | 'eff'
    | 'output'
    | 'outcost'
    | 'cache'
    | 'cost'
    | 'saved'
    | 'input'
    | 'cached'
    | 'written'
    | 'fresh'
    | 'reqs'
    | 'ctx',
): (number | null)[] {
  const n = cols.cost.length
  const out: (number | null)[] = new Array(n).fill(null)
  for (let i = 0; i < n; i++) {
    const cost = cols.cost[i]
    const input = cols.input[i]
    const cached = cols.cached[i]
    const written = cols.written?.[i] ?? 0
    const unc = cols.uncached[i]
    const reqs = cols.n[i]
    const generated = cols.output?.[i] ?? null
    const outCost = cols.cost_output?.[i] ?? null
    switch (kind) {
      case 'eff':
        if (cost != null && input) out[i] = cost / (input / 1e6)
        break
      case 'output':
        out[i] = generated
        break
      case 'outcost':
        out[i] = outCost
        break
      case 'cache':
        if (cached != null && input) out[i] = (cached / input) * 100
        break
      case 'cost':
        out[i] = cost
        break
      case 'saved':
        if (cost != null && unc != null) out[i] = unc - cost
        break
      case 'input':
        out[i] = input
        break
      case 'cached':
        out[i] = cached
        break
      case 'written':
        out[i] = written
        break
      case 'fresh':
        // Cache writes are neither cached nor charged at the plain input rate,
        // so they come out of the fresh band as well.
        if (input != null) out[i] = input - (cached ?? 0) - (written ?? 0)
        break
      case 'reqs':
        out[i] = reqs
        break
      case 'ctx':
        if (input != null && reqs) out[i] = input / reqs
        break
    }
  }
  return out
}

const CALENDAR_LABELS: Record<CalendarMetric, string> = {
  input_tokens: 'prompt tokens',
  cost: 'spend',
  cache_rate: 'cache hit rate',
  output_tokens: 'output tokens',
}

function toSeries(
  data: SeriesResponse | null,
  colors: ColorScale,
  kind: Parameters<typeof metric>[1],
  opts: { area?: boolean; label?: (key: string) => string } = {},
): TimeSeries[] {
  if (!data) return []
  const label = opts.label ?? shortModel
  return data.groups.map((g) => ({
    key: g.key,
    label: g.key === 'other' ? 'Other' : label(g.key),
    color: colors.get(g.key),
    values: metric(g, kind),
    area: opts.area,
  }))
}

export default function App() {
  const { state, scan, connected, newBuild } = useLiveState()
  const [theme, setTheme, themeEpoch] = useTheme()
  const [showOutput, setShowOutput] = useOutputView()
  const [range, setRange] = useState<RangeKey>('7d')
  const [bucket, setBucket] = useState('hour')
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [heatMetric, setHeatMetric] = useState<'cache_rate' | 'effective_rate'>('cache_rate')
  const [calMetric, setCalMetric] = useState<CalendarMetric>('input_tokens')

  const palette = useMemo(() => readPalette(), [themeEpoch])
  const generation = state?.generation ?? 0

  // Colour is keyed to the global model ordering, so filtering never repaints
  // the series that survive.
  const colors = useMemo(
    () => new ColorScale(state?.dimensions.models ?? [], palette),
    [state?.dimensions.models, palette],
  )
  const repoColors = useMemo(
    () => new ColorScale(state?.dimensions.repos ?? [], palette),
    [state?.dimensions.repos, palette],
  )
  const sourceColors = useMemo(
    () => new ColorScale(state?.dimensions.sources ?? [], palette),
    [state?.dimensions.sources, palette],
  )
  const multiClient = (state?.dimensions.sources?.length ?? 0) > 1

  const lastTs = state?.dimensions.last_ts ?? null
  const window = useMemo(() => {
    const spec = RANGES.find((r) => r.key === range)!
    if (spec.ms == null || lastTs == null) return { start: null, end: null }
    // Anchored to the newest data, not to now, so an idle machine still shows
    // its most recent activity rather than an empty chart.
    return { start: lastTs - spec.ms, end: null }
  }, [range, lastTs])

  const active: Filters = useMemo(
    () => ({ ...filters, start: window.start, end: window.end }),
    [filters, window],
  )

  const previousWindow: Filters | null = useMemo(() => {
    const spec = RANGES.find((r) => r.key === range)!
    if (spec.ms == null || window.start == null) return null
    return { ...filters, start: window.start - spec.ms, end: window.start }
  }, [filters, window, range])

  const key = filterKey(active)

  const totals = useQuery((s) => api.totals(active, s), [key, generation])
  const previous = useQuery(
    (s) => api.totals(previousWindow!, s),
    [previousWindow ? filterKey(previousWindow) : 'none', generation],
    previousWindow != null,
  )
  // Capped at the palette's slot count so the tail folds into one "Other" line
  // server-side. Asking for more just draws N identical greys, which reads as
  // several distinct series and is worse than an honest aggregate.
  const byModel = useQuery(
    (s) => api.series(active, { bucket, group: 'model', limit_groups: SLOT_COUNT }, s),
    [key, bucket, generation],
  )
  const overall = useQuery((s) => api.series(active, { bucket }, s), [key, bucket, generation])
  const modelRows = useQuery((s) => api.breakdown(active, 'model', s), [key, generation])
  const repoRows = useQuery((s) => api.breakdown(active, 'repo', s), [key, generation])
  const baseRows = useQuery((s) => api.breakdown(active, 'base_model', s), [key, generation])
  const providerRows = useQuery((s) => api.breakdown(active, 'provider', s), [key, generation])
  const sourceRows = useQuery(
    (s) => api.breakdown(active, 'source', s),
    [key, generation],
    multiClient,
  )
  const bySource = useQuery(
    (s) => api.series(active, { bucket, group: 'source' }, s),
    [key, bucket, generation],
    multiClient,
  )
  const byRepo = useQuery(
    (s) => api.series(active, { bucket, group: 'repo', limit_groups: SLOT_COUNT }, s),
    [key, bucket, generation],
  )
  const events = useQuery((s) => api.events(active, s), [key, generation])
  const heat = useQuery(
    (s) => api.heatmap(active, -new Date().getTimezoneOffset(), s),
    [key, generation],
  )
  // Deliberately keyed off the filters minus the time range: the calendar
  // paginates months itself, so a range change must not refetch it.
  const cal = useQuery(
    (s) => api.calendar({ ...active, start: null, end: null }, -new Date().getTimezoneOffset(), s),
    [filterKey({ ...filters, start: null, end: null }), generation],
  )
  const scatter = useQuery((s) => api.scatter(active, 36, s), [key, generation])
  const pricing = useQuery((s) => api.pricing(s), [generation, themeEpoch])

  const t = byModel.data?.t ?? overall.data?.t ?? []
  const bucketSeconds = byModel.data?.bucket_seconds ?? 3600

  const markers = useMemo(
    () =>
      (events.data ?? [])
        .filter((e) => e.kind === 'context_compacted' || e.kind === 'turn_aborted')
        .map((e) => ({ t: Math.round(e.ts / 1000), kind: e.kind })),
    [events.data],
  )

  // Dashed ceilings showing what each model would cost with no caching at all.
  const listRateLines = useMemo(() => {
    const rows = modelRows.data ?? []
    return rows
      .filter((r) => colors.hasOwnHue(r.key) && r.list_rate > 0)
      .slice(0, 4)
      .map((r) => ({
        value: r.list_rate,
        color: colors.get(r.key),
        label: `${shortModel(r.key)} list`,
      }))
  }, [modelRows.data, colors])

  const effSeries = toSeries(byModel.data, colors, 'eff')
  const cacheSeries = toSeries(byModel.data, colors, 'cache')
  const costSeries = toSeries(byModel.data, colors, 'cost', { area: true })
  const reqSeries = toSeries(byModel.data, colors, 'reqs', { area: true })

  const effByClient = toSeries(bySource.data, sourceColors, 'eff', { label: sourceLabel })
  const costByClient = toSeries(bySource.data, sourceColors, 'cost', {
    area: true,
    label: sourceLabel,
  })

  const volumeSeries: TimeSeries[] = useMemo(() => {
    const total = overall.data?.total
    if (!total) return []
    const written = metric(total, 'written')
    const series: TimeSeries[] = [
      {
        key: 'cached',
        label: 'Cached',
        color: palette.slots[2],
        values: metric(total, 'cached'),
        area: true,
      },
    ]
    // Only Anthropic reports and bills cache writes. Showing an always-zero
    // band for everyone else would be a permanent flat line saying nothing.
    if (written.some((v) => (v ?? 0) > 0)) {
      series.push({
        key: 'written',
        label: 'Written to cache (1.25–2× rate)',
        color: palette.slots[3],
        values: written,
        area: true,
      })
    }
    series.push({
      key: 'fresh',
      label: 'Fresh (billed at full rate)',
      color: palette.slots[1],
      values: metric(total, 'fresh'),
      area: true,
    })
    return series
  }, [overall.data, palette])

  const savedSeries: TimeSeries[] = useMemo(() => {
    const total = overall.data?.total
    if (!total) return []
    const saved = metric(total, 'saved')
    let running = 0
    const cumulative = saved.map((v) => {
      if (v == null) return null
      running += v
      return running
    })
    return [
      {
        key: 'saved',
        label: 'Cumulative saved',
        color: palette.slots[0],
        values: cumulative,
        area: true,
      },
    ]
  }, [overall.data, palette])

  const ctxSeries: TimeSeries[] = useMemo(() => {
    const total = overall.data?.total
    if (!total) return []
    return [
      {
        key: 'ctx',
        label: 'Mean prompt size',
        color: palette.slots[4],
        values: metric(total, 'ctx'),
      },
    ]
  }, [overall.data, palette])

  /**
   * Where the money actually goes.
   *
   * The four bands sum to total spend, and which one dominates is genuinely
   * not guessable from the cache rate: discounted reads at enormous volume
   * routinely outweigh the tokens billed at full price. Drawn in billing order
   * — cheapest rate at the bottom — so the stack reads as a rate ladder.
   */
  const costSplitSeries: TimeSeries[] = useMemo(() => {
    const total = overall.data?.total
    if (!total) return []
    const bands: [keyof typeof total, string, string][] = [
      ['cost_cached', 'Cache reads', palette.slots[2]],
      ['cost_fresh', 'Fresh input', palette.slots[0]],
      ['cost_written', 'Cache writes', palette.slots[3]],
      ['cost_output', 'Output', palette.slots[1]],
    ]
    return bands
      .filter(([key]) => (total[key] as (number | null)[]).some((v) => (v ?? 0) > 0))
      .map(([key, label, color]) => ({
        key: String(key),
        label,
        color,
        values: total[key] as (number | null)[],
        area: true,
      }))
  }, [overall.data, palette])

  /**
   * What the long-context tier actually costs.
   *
   * Two bands that sum to total spend: what the same tokens would have cost at
   * standard rates, and the markup on top. Stacking them rather than drawing
   * the surcharge alone keeps it in proportion -- a big number is only alarming
   * next to the bill it came out of.
   */
  const surchargeSeries: TimeSeries[] = useMemo(() => {
    const total = overall.data?.total
    if (!total) return []
    const surcharge = total.surcharge ?? []
    const base = total.cost.map((c, i) =>
      c == null ? null : c - (surcharge[i] ?? 0),
    )
    return [
      {
        key: 'standard',
        label: 'At standard rates',
        color: palette.slots[2],
        values: base,
        area: true,
      },
      {
        key: 'surcharge',
        label: 'Long-context surcharge',
        color: palette.slots[3],
        values: surcharge,
        area: true,
      },
    ]
  }, [overall.data, palette])

  // Volume and spend rather than $/Mtok out: output is never cached and has no
  // context tier, so its rate is the model's list price and a chart of it is a
  // chart of model mix -- which the two stacks below already show, in the units
  // that matter.
  const outputSeries = toSeries(byModel.data, colors, 'output', { area: true })
  const outputCostSeries = toSeries(byModel.data, colors, 'outcost', { area: true })

  const repoSeries = toSeries(byRepo.data, repoColors, 'cost', {
    area: true,
    label: repoLabel,
  })

  const routeInsight = useMemo(() => findRoutingGap(modelRows.data ?? []), [modelRows.data])

  const defaultThreshold = pricing.data?.default_threshold ?? 200000

  // Turning the output view off must not leave the calendar on a metric whose
  // button has just gone, with the heading as the only clue.
  useEffect(() => {
    if (!showOutput && calMetric === 'output_tokens') setCalMetric('input_tokens')
  }, [showOutput, calMetric])

  useEffect(() => {
    document.title = totals.data
      ? `${rate(totals.data.effective_rate)}/M · Agent Cache Monitor`
      : 'Agent Cache Monitor'
  }, [totals.data])

  return (
    <div className="app">
      <TopBar
        state={state}
        scan={scan}
        connected={connected}
        range={range}
        onRange={setRange}
        bucket={bucket}
        onBucket={setBucket}
        filters={filters}
        onFilters={setFilters}
        theme={theme}
        onTheme={setTheme}
        showOutput={showOutput}
        onShowOutput={setShowOutput}
        colors={colors}
        sourceColors={sourceColors}
      />

      <main className="main">
        <KpiStrip totals={totals.data} previous={previous.data} showOutput={showOutput} />

        {routeInsight && (
          <div className="callout">
            <span>
              <strong>{shortModel(routeInsight.routed.key)}</strong> is caching at{' '}
              {pct(routeInsight.routed.cache_rate, 1)} against{' '}
              {pct(routeInsight.direct.cache_rate, 1)} for{' '}
              <strong>{shortModel(routeInsight.direct.key)}</strong> — the same
              underlying model. That costs {rate(routeInsight.routed.effective_rate)}/M
              versus {rate(routeInsight.direct.effective_rate)}/M, or{' '}
              {routeInsight.multiple.toFixed(1)}× as much per token processed.
              Routing the same traffic direct would have saved about{' '}
              {usd(routeInsight.wasted)} in this window.
            </span>
          </div>
        )}

        {multiClient && (
          <>
            <div className="grid two">
              <section className="panel">
                <div className="panel-head">
                  <h2 className="panel-title">Effective rate by client</h2>
                  <span className="panel-note">
                    what each coding agent pays per million prompt tokens
                  </span>
                </div>
                <div className="panel-body">
                  <TimeChart
                    t={bySource.data?.t ?? t}
                    series={effByClient}
                    palette={palette}
                    bucketSeconds={bucketSeconds}
                    format={(v) => (v == null ? '--' : `$${v.toFixed(v < 1 ? 3 : 2)}`)}
                    unit="$/Mtok in"
                    yMin={0}
                    syncKey="ccm"
                  />
                </div>
              </section>

              <section className="panel">
                <div className="panel-head">
                  <h2 className="panel-title">Spend by client</h2>
                  <span className="panel-note">per {bucketLabel(bucketSeconds)}</span>
                </div>
                <div className="panel-body">
                  <TimeChart
                    t={bySource.data?.t ?? t}
                    series={costByClient}
                    palette={palette}
                    bucketSeconds={bucketSeconds}
                    format={(v) => usd(v)}
                    unit="USD"
                    stacked
                    syncKey="ccm"
                  />
                </div>
              </section>
            </div>

            <section className="panel">
              <div className="panel-head">
                <h2 className="panel-title">By client</h2>
                <span className="panel-note">
                  every client costed at the same list prices, so the rates compare
                </span>
              </div>
              <div className="panel-body">
                <BreakdownTable
                  rows={sourceRows.data ?? []}
                  label={sourceLabel}
                  showOutput={showOutput}
                  colors={sourceColors}
                  onSelect={(k) =>
                    setFilters((f) => ({
                      ...f,
                      sources: f.sources.includes(k)
                        ? f.sources.filter((s) => s !== k)
                        : [...f.sources, k],
                    }))
                  }
                />
              </div>
            </section>
          </>
        )}

        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Effective rate by model</h2>
            <span className="panel-note">
              USD per million input tokens processed · lower is better · dashed lines
              are undiscounted list price
            </span>
          </div>
          <div className="panel-body">
            <TimeChart
              t={t}
              series={effSeries}
              palette={palette}
              height={280}
              bucketSeconds={bucketSeconds}
              format={(v) => (v == null ? '--' : `$${v.toFixed(v < 1 ? 3 : 2)}`)}
              unit="$/Mtok in"
              markers={markers}
              refLines={listRateLines}
              syncKey="ccm"
              yMin={0}
            />
          </div>
        </section>

        {/* Width follows importance: cache rate and prompt volume are the two
            that explain the headline, so they get more of the row than the
            three supporting charts below them. */}
        <div className="grid">
          <section className="panel span-7">
            <div className="panel-head">
              <h2 className="panel-title">Cache hit rate by model</h2>
              <span className="panel-note">
                baseline ticks mark compaction or abort
              </span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={cacheSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => (v == null ? '--' : `${v.toFixed(0)}%`)}
                unit="%"
                yMin={0}
                yMax={100}
                markers={markers}
                syncKey="ccm"
              />
            </div>
          </section>

          <section className="panel span-5">
            <div className="panel-head">
              <h2 className="panel-title">Prompt tokens processed</h2>
              <span className="panel-note">cached versus billed at the full rate</span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={volumeSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => compact(v)}
                unit="tokens"
                stacked
                syncKey="ccm"
              />
            </div>
          </section>

          <section className="panel span-4">
            <div className="panel-head">
              <h2 className="panel-title">Spend by model</h2>
              <span className="panel-note">per {bucketLabel(bucketSeconds)}</span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={costSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => usd(v)}
                unit="USD"
                stacked
                syncKey="ccm"
              />
            </div>
          </section>

          <section className="panel span-4">
            <div className="panel-head">
              <h2 className="panel-title">Cumulative saved by caching</h2>
              <span className="panel-note">against the zero-cache counterfactual</span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={savedSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => usd(v)}
                unit="USD"
                syncKey="ccm"
              />
            </div>
          </section>

          <section className="panel span-4">
            <div className="panel-head">
              <h2 className="panel-title">Requests by model</h2>
              <span className="panel-note">per {bucketLabel(bucketSeconds)}</span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={reqSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => (v == null ? '--' : compact(v, 0))}
                unit="requests"
                stacked
                syncKey="ccm"
              />
            </div>
          </section>

          <section className="panel span-4">
            <div className="panel-head">
              <h2 className="panel-title">Mean prompt size</h2>
              <span className="panel-note">
                context growth raises cost even at a steady cache rate
              </span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={ctxSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => compact(v)}
                unit="tokens"
                refLines={[
                  {
                    value: defaultThreshold,
                    color: palette.textMuted,
                    label: 'long-context threshold',
                  },
                ]}
                syncKey="ccm"
              />
            </div>
          </section>

          <section className="panel span-4">
            <div className="panel-head">
              <h2 className="panel-title">Where the money goes</h2>
              <span className="panel-note">
                spend by what it was billed as · bands sum to total cost
              </span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={costSplitSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => usd(v)}
                unit="USD"
                stacked
                syncKey="ccm"
              />
            </div>
          </section>

          {showOutput && (
            <>
              <section className="panel span-4">
                <div className="panel-head">
                  <h2 className="panel-title">Output tokens by model</h2>
                  <span className="panel-note">
                    generated, reasoning included · per {bucketLabel(bucketSeconds)}
                  </span>
                </div>
                <div className="panel-body">
                  <TimeChart
                    t={t}
                    series={outputSeries}
                    palette={palette}
                    bucketSeconds={bucketSeconds}
                    format={(v) => compact(v)}
                    unit="tokens"
                    stacked
                    syncKey="ccm"
                  />
                </div>
              </section>

              <section className="panel span-4">
                <div className="panel-head">
                  <h2 className="panel-title">Output spend by model</h2>
                  <span className="panel-note">
                    {totals.data
                      ? `${pct(
                          totals.data.cost ? totals.data.cost_output / totals.data.cost : 0,
                          0,
                        )} of spend in this window`
                      : 'the part of the bill caching cannot touch'}
                  </span>
                </div>
                <div className="panel-body">
                  <TimeChart
                    t={t}
                    series={outputCostSeries}
                    palette={palette}
                    bucketSeconds={bucketSeconds}
                    format={(v) => usd(v)}
                    unit="USD"
                    stacked
                    syncKey="ccm"
                  />
                </div>
              </section>
            </>
          )}

          <section className="panel span-4">
            <div className="panel-head">
              <h2 className="panel-title">Spend by project</h2>
              <span className="panel-note">per {bucketLabel(bucketSeconds)}</span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={byRepo.data?.t ?? t}
                series={repoSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => usd(v)}
                unit="USD"
                stacked
                syncKey="ccm"
              />
            </div>
          </section>
        </div>

        <div className="grid">
          <section className="panel span-7">
            <div className="panel-head">
              <h2 className="panel-title">Daily {CALENDAR_LABELS[calMetric]}</h2>
              <div className="panel-head-tail">
                <span className="panel-note">
                  all history, not the selected range
                </span>
                <div className="seg" role="group" aria-label="Calendar metric">
                  <button
                    type="button"
                    aria-pressed={calMetric === 'input_tokens'}
                    onClick={() => setCalMetric('input_tokens')}
                  >
                    Tokens
                  </button>
                  <button
                    type="button"
                    aria-pressed={calMetric === 'cost'}
                    onClick={() => setCalMetric('cost')}
                  >
                    Spend
                  </button>
                  <button
                    type="button"
                    aria-pressed={calMetric === 'cache_rate'}
                    onClick={() => setCalMetric('cache_rate')}
                  >
                    Cache
                  </button>
                  {showOutput && (
                    <button
                      type="button"
                      aria-pressed={calMetric === 'output_tokens'}
                      onClick={() => setCalMetric('output_tokens')}
                    >
                      Output
                    </button>
                  )}
                </div>
              </div>
            </div>
            <div className="panel-body">
              <Calendar
                days={cal.data?.days ?? []}
                palette={palette}
                metric={calMetric}
              />
            </div>
          </section>

          {/* The surcharge is invisible on an invoice -- it arrives as a higher
              per-token rate, not a line item -- and it is the one part of the
              bill that shrinks by changing behaviour rather than by changing
              model. */}
          <section className="panel span-5">
            <div className="panel-head">
              <h2 className="panel-title">The long-context surcharge</h2>
              <span className="panel-note">
                {totals.data && totals.data.long_surcharge > 0
                  ? `${usd(totals.data.long_surcharge)} of ${usd(
                      totals.data.cost,
                    )} — ${pct(
                      totals.data.long_surcharge / totals.data.cost,
                      1,
                    )} of spend — paid purely for prompts over the threshold`
                  : 'no prompts crossed a long-context threshold in this window'}
              </span>
            </div>
            <div className="panel-body">
              <TimeChart
                t={t}
                series={surchargeSeries}
                palette={palette}
                bucketSeconds={bucketSeconds}
                format={(v) => usd(v)}
                unit="USD"
                stacked
                syncKey="ccm"
              />
            </div>
          </section>
        </div>

        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">By model</h2>
            <span className="panel-note">
              routed variants are priced at their underlying model's rates
            </span>
          </div>
          <div className="panel-body">
            <BreakdownTable
              rows={modelRows.data ?? []}
              label={shortModel}
              showOutput={showOutput}
              colors={colors}
              flag={(r) => (r.cache_rate < 0.85 ? 'poor cache' : null)}
              onSelect={(k) =>
                setFilters((f) => ({
                  ...f,
                  models: f.models.includes(k) ? f.models.filter((m) => m !== k) : [...f.models, k],
                }))
              }
            />
          </div>
        </section>

        {/* Two half-width tables until the output group is on, at which point
            eleven numeric columns no longer fit half a row and each takes a full
            one -- the alternative is two panels that scroll sideways to reach the
            columns you just asked for. */}
        <div className={showOutput ? 'grid' : 'grid two'}>
          <section className={'panel' + (showOutput ? ' span-12' : '')}>
            <div className="panel-head">
              <h2 className="panel-title">By route</h2>
              <span className="panel-note">direct versus proxied</span>
            </div>
            <div className="panel-body">
              <BreakdownTable
                rows={providerRows.data ?? []}
                label={(k) => (k === '' ? 'direct' : k)}
                showOutput={showOutput}
              />
            </div>
          </section>

          <section className={'panel' + (showOutput ? ' span-12' : '')}>
            <div className="panel-head">
              <h2 className="panel-title">By underlying model</h2>
              <span className="panel-note">routes collapsed together</span>
            </div>
            <div className="panel-body">
              <BreakdownTable
                rows={baseRows.data ?? []}
                label={shortModel}
                showOutput={showOutput}
              />
            </div>
          </section>
        </div>

        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">By repository</h2>
          </div>
          <div className="panel-body">
            <BreakdownTable
              rows={repoRows.data ?? []}
              label={repoLabel}
              showOutput={showOutput}
              colors={repoColors}
              onSelect={(k) =>
                setFilters((f) => ({
                  ...f,
                  repos: f.repos.includes(k) ? f.repos.filter((r) => r !== k) : [...f.repos, k],
                }))
              }
            />
          </div>
        </section>

        <div className="grid two">
          <section className="panel">
            <div className="panel-head">
              <h2 className="panel-title">
                {heatMetric === 'cache_rate' ? 'Cache rate' : 'Effective rate'} by hour and
                weekday
              </h2>
              <div className="seg" role="group" aria-label="Heatmap metric">
                <button
                  type="button"
                  aria-pressed={heatMetric === 'cache_rate'}
                  onClick={() => setHeatMetric('cache_rate')}
                >
                  Cache
                </button>
                <button
                  type="button"
                  aria-pressed={heatMetric === 'effective_rate'}
                  onClick={() => setHeatMetric('effective_rate')}
                >
                  $/Mtok
                </button>
              </div>
            </div>
            <div className="panel-body">
              <Heatmap
                cells={heat.data?.cells ?? []}
                palette={palette}
                metric={heatMetric}
              />
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h2 className="panel-title">Cache rate against prompt size</h2>
              <span className="panel-note">
                where caching falls off as context grows
              </span>
            </div>
            <div className="panel-body">
              {scatter.data ? (
                <DensityPlot
                  grid={scatter.data}
                  palette={palette}
                  thresholdTokens={defaultThreshold}
                />
              ) : (
                <div className="empty">Loading…</div>
              )}
            </div>
          </section>
        </div>

        <SessionExplorer
          filters={active}
          generation={generation}
          colors={colors}
          palette={palette}
          showOutput={showOutput}
        />

        {/* Full width: eight numeric columns do not fit a half-width panel
            without the last one falling off the edge. */}
        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Machines</h2>
            <span className="panel-note">
              carry these stats to another PC, or pool a team's
            </span>
          </div>
          <div className="panel-body">
{/* A nudge, not the recompute itself: both endpoints already refresh the
                derived state server-side before they answer, so a refusal while
                scanning is paused costs nothing. */}
            <Machines generation={generation} onChanged={() => nudgeScan()} />
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Pricing</h2>
            <span className="panel-note">
              USD per million tokens · short and long context tiers
            </span>
          </div>
          <div className="panel-body">
{/* A nudge, not the recompute itself: both endpoints already refresh the
                derived state server-side before they answer, so a refusal while
                scanning is paused costs nothing. */}
            <PricingEditor pricing={pricing.data} onSaved={() => nudgeScan()} />
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Reference prices</h2>
            <span className="panel-note">
              the configured table, held against models.dev
            </span>
          </div>
          <div className="panel-body">
            <ReferencePrices generation={generation} />
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Data quality</h2>
            <span className="panel-note">what the scan could not fully account for</span>
          </div>
          <div className="panel-body">
            <DataQuality
              quality={state?.quality ?? null}
              scan={scan}
              sessionsDir={state?.sessions_dir ?? ''}
              sources={state?.sources ?? []}
            />
          </div>
        </section>
      </main>

      <UpdatePrompt build={newBuild} version={state?.build?.version} />
    </div>
  )
}

/**
 * "The server was upgraded -- reload."
 *
 * Left to the operator rather than reloading automatically: a tab that reloads
 * itself throws away whatever was on screen, and an upgrade lands the moment
 * `just update` restarts the unit, which is exactly when someone may be reading
 * a chart.
 *
 * Dismissible for the same reason, and keyed on the build that prompted it, so
 * dismissing this upgrade does not also silence the next one -- a tab left open
 * for a week would otherwise go quiet after the first "Later".
 */
function UpdatePrompt({ build, version }: { build: string | null; version?: string }) {
  const [dismissed, setDismissed] = useState<string | null>(null)
  const showing = build !== null && dismissed !== build
  // The live region is mounted whether or not it has anything to say: a region
  // that appears at the same moment as its text is frequently not announced,
  // because there was nothing there to observe a change to.
  return (
    <div role="status" aria-live="polite">
      {showing && (
        <div className="updatebar">
          <span>
            <strong>Update installed</strong>
            {version ? ` · version ${version}` : ''} — this page is running the old
            build.
          </span>
          <button
            className="btn primary"
            type="button"
            onClick={() => location.reload()}
          >
            Reload
          </button>
          <button
            className="btn"
            type="button"
            onClick={() => setDismissed(build)}
            title="Keep the old build for now"
          >
            Later
          </button>
        </div>
      )}
    </div>
  )
}

/** Ask for a scan pass, tolerating the refusal a paused server answers with. */
function nudgeScan() {
  void api.rescan(false).catch(() => {})
}

function bucketLabel(seconds: number): string {
  if (seconds >= 86400) return 'day'
  if (seconds >= 3600) return `${seconds / 3600}h`
  return `${seconds / 60}m`
}

/**
 * Finds the worst case of the same underlying model caching materially better
 * on one route than another, and prices the gap.
 */
function findRoutingGap(rows: { key: string; cache_rate: number; effective_rate: number; input_tokens: number; cost: number }[]) {
  const byBase = new Map<string, typeof rows>()
  for (const r of rows) {
    const base = r.key.includes('/') ? r.key.split('/')[1] : r.key
    const list = byBase.get(base) ?? []
    list.push(r)
    byBase.set(base, list)
  }
  let best: {
    routed: (typeof rows)[number]
    direct: (typeof rows)[number]
    multiple: number
    wasted: number
  } | null = null
  for (const group of byBase.values()) {
    if (group.length < 2) continue
    const direct = group.find((r) => !r.key.includes('/'))
    if (!direct || direct.effective_rate <= 0) continue
    for (const routed of group) {
      if (routed === direct) continue
      if (routed.cache_rate >= direct.cache_rate - 0.05) continue
      const multiple = routed.effective_rate / direct.effective_rate
      const wasted = routed.cost - (routed.input_tokens / 1e6) * direct.effective_rate
      if (multiple > 1.15 && (!best || multiple > best.multiple)) {
        best = { routed, direct, multiple, wasted }
      }
    }
  }
  return best
}
