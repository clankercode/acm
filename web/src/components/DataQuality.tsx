import { bytes, compact, integer, pct, usd } from '../lib/format'
import { sourceLabel, type Quality, type ScanState, type SourceInfo } from '../lib/types'

const ANOMALY_LABELS: Record<string, string> = {
  total_mismatch: 'total ≠ input + output',
  reasoning_gt_output: 'reasoning > output',
  cached_gt_input: 'cached > input',
  cum_regression: 'cumulative counter went backwards',
  parse_error: 'unparseable lines',
  cache_ttl_mismatch: 'cache TTL split ≠ cache write total',
  synthetic_message: 'synthetic (non-API) assistant messages',
  missing_message_id: 'no message id to deduplicate on',
  missing_response_id: 'no response id to deduplicate on',
  multi_call_turn: 'extra API calls folded into a turn (Grok)',
}

interface Props {
  quality: Quality | null
  scan: ScanState | null
  sessionsDir: string
  sources: SourceInfo[]
}

/**
 * What the scan could not fully account for.
 *
 * The replay ratio belongs here rather than buried: the corpus repeats each
 * request several times over, and if that number ever drifts toward 1.0 the
 * deduplication has stopped working and every figure on the page is inflated.
 */
export function DataQuality({ quality, scan, sessionsDir, sources }: Props) {
  if (!quality) return <div className="empty">Loading…</div>

  const anomalies = Object.entries(quality.anomalies).filter(([, n]) => n > 0)
  const anomalyRate = quality.deduped_requests
    ? (quality.anomalies.total_mismatch ?? 0) / quality.deduped_requests
    : 0
  const roots = new Map(sources.map((s) => [s.name, s.root]))
  const audits = quality.sources.filter((s) => s.audit && s.audit.requests > 0)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <dl className="kv">
        <dt>Token events</dt>
        <dd>
          {integer(quality.raw_token_events)} read →{' '}
          {integer(quality.deduped_requests)} real requests across{' '}
          {quality.sources.length} client{quality.sources.length === 1 ? '' : 's'}
        </dd>
        <dt>Duplicate ratio</dt>
        <dd>
          {quality.replay_ratio.toFixed(2)}× ({integer(quality.replayed_events)} duplicate
          records discarded)
        </dd>
        {scan && (
          <>
            <dt>Last pass</dt>
            <dd>
              {scan.elapsed.toFixed(1)}s · {(scan.bytes_per_sec / 1e6).toFixed(0)} MB/s
            </dd>
          </>
        )}
      </dl>

      <div>
        <div className="panel-note" style={{ marginBottom: 4 }}>
          Per client — every one of these repeats itself, and each does it differently
        </div>
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th>Client</th>
                <th className="num">Files</th>
                <th className="num">Bytes</th>
                <th className="num">Records</th>
                <th className="num">Requests</th>
                <th className="num">Duplicate</th>
                <th>Root</th>
              </tr>
            </thead>
            <tbody>
              {quality.sources.map((s) => (
                <tr key={s.source}>
                  <td>{sourceLabel(s.source)}</td>
                  <td className="num">{integer(s.files)}</td>
                  <td className="num">{bytes(s.bytes)}</td>
                  <td className="num">{integer(s.raw_token_events)}</td>
                  <td className="num">{integer(s.requests)}</td>
                  <td className="num">{s.replay_ratio.toFixed(2)}×</td>
                  <td className="dim mono">
                    {roots.get(s.source) ?? (s.source === 'codex' ? sessionsDir : '')}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="note">
        Codex rewrites a thread's whole history into a new file whenever a session
        resumes or a subagent forks, stamping the copies with the moment of the
        rewrite; Claude Code writes a fresh line for every content block as a
        response streams in, each repeating the usage so far. Both are collapsed on
        an identifier the provider guarantees — cumulative token counters for Codex,
        the API message id for Claude Code — keeping the earliest sighting in the
        first case and the most complete one in the second. Summing naively would
        overstate the corpus by {quality.replay_ratio.toFixed(1)}×.
      </p>

      {audits.length > 0 && (
        <div>
          <div className="panel-note" style={{ marginBottom: 4 }}>
            Checked against the client's own cost figure, over the requests it priced
            itself
          </div>
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th>Client</th>
                  <th className="num">Requests</th>
                  <th className="num">Ours</th>
                  <th className="num">Theirs</th>
                  <th className="num">Ratio</th>
                </tr>
              </thead>
              <tbody>
                {audits.map((s) => {
                  const a = s.audit!
                  const off = a.ratio != null && Math.abs(a.ratio - 1) > 0.01
                  return (
                    <tr key={s.source}>
                      <td>{sourceLabel(s.source)}</td>
                      <td className="num">{integer(a.requests)}</td>
                      <td className="num">{usd(a.ours)}</td>
                      <td className="num">{usd(a.theirs)}</td>
                      <td className={'num ' + (off ? 'bad' : 'good')}>
                        {a.ratio == null ? '--' : `${a.ratio.toFixed(4)}×`}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {anomalies.length > 0 && (
        <div>
          <div className="panel-note" style={{ marginBottom: 4 }}>
            Reported inconsistencies (recorded as found, never silently corrected)
          </div>
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th>Anomaly</th>
                  <th>Events</th>
                  <th>Share of requests</th>
                </tr>
              </thead>
              <tbody>
                {anomalies.map(([kind, n]) => (
                  <tr key={kind}>
                    <td>{ANOMALY_LABELS[kind] ?? kind}</td>
                    <td>{integer(n)}</td>
                    <td>
                      {pct(quality.deduped_requests ? n / quality.deduped_requests : 0, 2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {quality.unpriced_models.length > 0 && (
        <div className="callout">
          <span>
            <strong>Unpriced models.</strong> These appear in the corpus with no rate
            configured, so their tokens are counted but contribute nothing to cost:{' '}
            {quality.unpriced_models
              .map((m) => `${m.model} (${compact(m.input_tokens)} tokens)`)
              .join(', ')}
            .
          </span>
        </div>
      )}

      {quality.estimated_pricing.length > 0 && (
        <div className="callout">
          <span>
            <strong>Estimated rates.</strong> {quality.estimated_pricing.join(', ')} —
            these use inferred figures or have no published long-context tier, so their
            cost is indicative rather than exact.
          </span>
        </div>
      )}

      {anomalyRate > 0.02 && (
        <div className="callout">
          <span>
            <strong>Arithmetic drift.</strong> {pct(anomalyRate, 1)} of requests report a
            total that does not equal input plus output. Token counts are stored as
            reported; the discrepancy is upstream.
          </span>
        </div>
      )}

      {scan?.last_error && (
        <div className="callout">
          <span>
            <strong>Last scan error.</strong> {scan.last_error}
          </span>
        </div>
      )}
    </div>
  )
}
