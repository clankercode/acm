import { compact, delta, deltaPoints, integer, pct, rate, usd } from '../lib/format'
import type { Totals } from '../lib/types'

interface Props {
  totals: Totals | null
  /** Same window, immediately before the current one, for the trend chips. */
  previous: Totals | null
}

/**
 * The headline row.
 *
 * "Effective rate" leads because it is the one number that answers "is caching
 * working": total spend divided by input tokens processed, in dollars per
 * million. It folds cache hit rate, context tier and model mix together, and
 * lower is always better regardless of how much work was done.
 */
export function KpiStrip({ totals, previous }: Props) {
  if (!totals) {
    return (
      <div className="kpis">
        {Array.from({ length: 6 }, (_, i) => (
          <div className="kpi" key={i}>
            <div className="kpi-label">&nbsp;</div>
            <div className="kpi-value">--</div>
            <div className="kpi-sub">&nbsp;</div>
          </div>
        ))}
      </div>
    )
  }

  const effDelta = previous ? delta(totals.effective_rate, previous.effective_rate) : null
  const cacheDelta = previous ? deltaPoints(totals.cache_rate, previous.cache_rate) : null
  const costDelta = previous ? delta(totals.cost, previous.cost) : null

  return (
    <div className="kpis">
      <Kpi
        label="Effective rate"
        value={`${rate(totals.effective_rate)}/M`}
        lead
        sub={
          <>
            {pct(totals.efficiency, 0)} of list ({rate(totals.list_rate)}/M)
            {effDelta?.text && (
              <>
                {' · '}
                <span className={`kpi-delta ${effDelta.cls}`}>{effDelta.text}</span>
              </>
            )}
          </>
        }
        title="Total cost divided by input tokens processed. Lower is better."
      />

      <Kpi
        label="Cache hit rate"
        value={pct(totals.cache_rate, 2)}
        sub={
          <>
            {compact(totals.cached_tokens)} of {compact(totals.input_tokens)} cached
            {cacheDelta?.text && (
              <>
                {' · '}
                <span className={`kpi-delta ${cacheDelta.cls}`}>{cacheDelta.text}</span>
              </>
            )}
          </>
        }
        title="Cached prompt tokens as a share of all prompt tokens."
      />

      <Kpi
        label="Cost"
        value={usd(totals.cost)}
        sub={
          <>
            {integer(totals.requests)} requests
            {costDelta?.text && (
              <>
                {' · '}
                <span className={`kpi-delta ${costDelta.cls}`}>{costDelta.text}</span>
              </>
            )}
          </>
        }
        title="Pay-as-you-go equivalent at current published rates."
      />

      <Kpi
        label="Saved by caching"
        value={usd(totals.saved)}
        sub={`${pct(totals.saved_fraction, 1)} off ${usd(totals.uncached_cost)} uncached`}
        title="What the same tokens would have cost with no cache hits, minus what they did cost."
      />

      <Kpi
        label="Input tokens"
        value={compact(totals.input_tokens)}
        sub={`${compact(totals.fresh_tokens)} fresh · ${compact(totals.output_tokens)} out`}
        title="Prompt tokens processed, cached and fresh combined."
      />

      <Kpi
        label="Avg context"
        value={compact(totals.avg_context, 1)}
        sub={`${compact(totals.reasoning_tokens)} reasoning tokens`}
        title="Mean prompt size per request. Growth here drives cost even at a steady cache rate."
      />
    </div>
  )
}

function Kpi({
  label,
  value,
  sub,
  lead,
  title,
}: {
  label: string
  value: string
  sub: React.ReactNode
  lead?: boolean
  title?: string
}) {
  return (
    <div className="kpi" title={title}>
      <div className="kpi-label">{label}</div>
      <div className={'kpi-value' + (lead ? ' lead' : '')}>{value}</div>
      <div className="kpi-sub">{sub}</div>
    </div>
  )
}
