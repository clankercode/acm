import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { bytes, compact, seconds } from '../lib/format'
import type { ThemeChoice } from '../lib/live'
import type { ColorScale } from '../lib/palette'
import type { AppState, Filters, ScanState } from '../lib/types'
import { sourceLabel } from '../lib/types'
import { shortModel, repoLabel } from '../lib/format'

export const RANGES = [
  { key: '24h', label: '24h', ms: 24 * 3600e3 },
  { key: '7d', label: '7d', ms: 7 * 86400e3 },
  { key: '30d', label: '30d', ms: 30 * 86400e3 },
  { key: 'all', label: 'All', ms: null },
] as const

export type RangeKey = (typeof RANGES)[number]['key']

export const BUCKETS = [
  { key: '10m', label: '10m' },
  { key: 'hour', label: '1h' },
  { key: '6h', label: '6h' },
  { key: 'day', label: '1d' },
] as const

interface Props {
  state: AppState | null
  scan: ScanState | null
  connected: boolean
  range: RangeKey
  onRange: (r: RangeKey) => void
  bucket: string
  onBucket: (b: string) => void
  filters: Filters
  onFilters: (f: Filters) => void
  theme: ThemeChoice
  onTheme: (t: ThemeChoice) => void
  showOutput: boolean
  onShowOutput: (on: boolean) => void
  colors: ColorScale
  sourceColors: ColorScale
}

export function TopBar(props: Props) {
  const { state, scan, connected, filters, onFilters, colors, sourceColors } = props
  const dims = state?.dimensions

  // A progress bar is for a scan you would wait on. The corpora are live, so a
  // pass picking up a few appended lines runs every couple of seconds forever;
  // reporting those is what made the header strobe. Real catch-up work -- a
  // first scan, or the machine having been off -- is many files or many bytes.
  // Optimistic, because the round trip plus the next broadcast is long enough
  // for a click to feel ignored. Dropped as soon as the server agrees -- or as
  // soon as the request fails -- rather than lying. With `?live=0` no broadcast
  // ever arrives, so there it stands until the page is reloaded; that mode
  // exists for screenshots, which have nothing to reconcile with.
  const [pauseWish, setPauseWish] = useState<boolean | null>(null)
  const paused = pauseWish ?? !!scan?.paused
  useEffect(() => {
    if (pauseWish != null && scan?.paused === pauseWish) setPauseWish(null)
  }, [pauseWish, scan?.paused])

  const substantial =
    !!scan &&
    !paused &&
    scan.phase !== 'idle' &&
    scan.phase !== 'tailing' &&
    (scan.files_total >= 10 ||
      scan.bytes_total >= 8e6 ||
      // Announced only when discovery is slow enough to look like a hang.
      scan.phase === 'discovering')
  const [pending, setPending] = useState(false)
  const busy = useSustained(substantial)

  // A manual refresh gets an answer even when the pass is too small to chart.
  useEffect(() => {
    if (!pending) return
    if (busy) return setPending(false)
    const timer = window.setTimeout(() => setPending(false), 1400)
    return () => window.clearTimeout(timer)
  }, [pending, busy])

  const pct = !scan
    ? 0
    : scan.bytes_total > 0
      ? Math.min(100, (scan.bytes_done / scan.bytes_total) * 100)
      : scan.files_total > 0
        ? Math.min(100, (scan.files_done / scan.files_total) * 100)
        : 0
  // Latched, because the indicator outlives the pass by design: the phase is
  // back to "tailing" for the last moments it is on screen.
  const verb = useRef('scanning')
  if (substantial && scan) verb.current = scan.phase === 'updating' ? 'updating' : 'scanning'

  const activeFilters =
    filters.origins.length +
    filters.sources.length +
    filters.models.length +
    filters.providers.length +
    filters.repos.length +
    (filters.subagent !== 'all' ? 1 : 0)

  return (
    <header className="topbar">
      <div className="topbar-row">
        <div className="brand">
          Agent Cache Monitor{' '}
          <span>· {state?.dimensions.requests.toLocaleString() ?? '--'} requests</span>
        </div>

        <span className="status" title={scan?.current_file ?? undefined}>
          <span
            className={
              'dot ' +
              (!connected ? 'down' : paused ? 'paused' : busy ? 'busy' : 'live')
            }
          />
          {/* One word, in a fixed footprint. The counts belong beside the bar,
              where they can grow a digit without shunting the row along. */}
          {!connected
            ? 'disconnected'
            : paused
              ? 'paused'
              : !busy
                ? 'live'
                : scan!.phase === 'discovering'
                  ? 'discovering'
                  : verb.current}
        </span>

        {/* Corpus totals, not the last pass's counters, and shown whether or not
            a pass is running -- a quiet poll cycle legitimately reads zero, and
            blanking these mid-scan made the header jump. */}
        {state?.quality && (
          <span
            className="scan-meta"
            title="Token events read across the corpus, and how many were replayed copies"
          >
            {compact(state.quality.raw_token_events, 1)} events ·{' '}
            {compact(state.quality.deduped_requests, 1)} requests ·{' '}
            {state.quality.replay_ratio.toFixed(1)}× replay
            {scan && scan.errors > 0 && (
              <>
                {' · '}
                <ErrorPopover scan={scan} />
              </>
            )}
          </span>
        )}

        {/* The gap between the identity block and the controls is where scan
            progress goes, so a pass in flight adds a line rather than pushing
            anything around. Empty the rest of the time. */}
        <div className="scanslot">
          {busy ? (
            <>
              {scan!.files_total > 0 && (
                <span className="scan-meta">
                  {scan!.files_done}/{scan!.files_total} files
                </span>
              )}
              <div
                className="scanbar"
                role="progressbar"
                aria-valuenow={Math.round(pct)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Scan progress"
              >
                <div className="scanbar-fill" style={{ width: `${pct}%` }} />
              </div>
              {scan!.bytes_total > 0 && (
                <span className="scan-meta scan-rate">
                  {bytes(scan!.bytes_done)} / {bytes(scan!.bytes_total)} ·{' '}
                  {(scan!.bytes_per_sec / 1e6).toFixed(0)} MB/s
                  {/* Windowed, unlike the MB/s beside it, which is the pass
                      average. Rows rather than token events, because most lines
                      carry no token counts and an event rate reads zero through
                      thousands of files that are being consumed perfectly well.
                      Titled with the file it is currently reading, since that is
                      what the number describes. */}
                  <span
                    title={
                      'Rows of session history read per second, averaged over the' +
                      ' last few seconds' +
                      (scan!.current_file ? `\n\nreading ${scan!.current_file}` : '')
                    }
                  >
                    {' · '}
                    {compact(scan!.rows_per_sec, 1)} rows/s
                  </span>
                  {scan!.eta_seconds != null && ` · ${seconds(scan!.eta_seconds)} left`}
                </span>
              )}
            </>
          ) : (
            paused && scan?.rebuild_pending ? (
              // Pausing part-way through a from-scratch rebuild leaves the
              // charts honestly near-empty. Saying so beats letting it read as
              // a machine with no history.
              <span className="scan-meta">rebuild incomplete · resume to finish</span>
            ) : (
              pending && (
              <div
                className="scanbar indeterminate"
                role="progressbar"
                aria-label="Checking for new sessions"
              >
                <div className="scanbar-fill" />
              </div>
              )
            )
          )}
        </div>

        <div className="seg" role="group" aria-label="Theme">
          {(['system', 'light', 'dark'] as ThemeChoice[]).map((t) => (
            <button
              key={t}
              type="button"
              aria-pressed={props.theme === t}
              onClick={() => props.onTheme(t)}
              title={`${t} theme`}
            >
              {t === 'system' ? 'Auto' : t === 'light' ? 'Light' : 'Dark'}
            </button>
          ))}
        </div>

        {/* Pause is a resource control, not a view control: a cold scan reads
            for minutes at full disk, and the answer to wanting the machine back
            has to be better than killing the server. Resuming carries on from
            the stored cursors, so nothing is re-read. */}
        {/* No aria-pressed: the label is the state. "Resume, pressed" reads as
            "resume is on", which is the opposite of what is true. */}
        <button
          className={'btn' + (paused ? ' primary' : '')}
          type="button"
          onClick={() => {
            const next = !paused
            setPauseWish(next)
            api.setPaused(next).catch(() => setPauseWish(null))
            if (!next) setPending(true)
          }}
          title={
            paused
              ? 'Resume scanning, carrying on where the paused pass stopped'
              : 'Stop scanning until resumed. Reading stops within a file or two'
          }
        >
          {paused ? 'Resume' : 'Pause'}
        </button>

        {/* aria-disabled rather than disabled: a disabled button leaves the tab
            order and fires no mouse events, so the one place that says why it
            cannot be used -- its own tooltip -- becomes unreachable. */}
        <button
          className="btn"
          type="button"
          aria-disabled={paused}
          onClick={() => {
            if (paused) return
            setPending(true)
            api.rescan(false).catch(() => {})
          }}
          title={
            paused
              ? 'Scanning is paused — resume first'
              : 'Check the corpus for new sessions now'
          }
        >
          Refresh
        </button>
      </div>

      {/* Scope comes before the rest: which agents and which machines you are
          looking at changes what every other control means. */}
      {(dims?.sources?.length ?? 0) > 1 && (
        <div className="topbar-row">
          <ScopeRail
            label="Client"
            options={dims!.sources}
            selected={filters.sources}
            onChange={(sources) => onFilters({ ...filters, sources })}
            render={sourceLabel}
            colorOf={(s) => sourceColors.get(s)}
          />
          {(dims?.origins?.length ?? 0) > 1 && (
            <ScopeRail
              label="Machine"
              options={dims!.origins}
              selected={filters.origins}
              onChange={(origins) => onFilters({ ...filters, origins })}
              render={(o) => (o === '' ? state?.local_label || 'this machine' : o)}
            />
          )}
        </div>
      )}

      <div className="topbar-row">
        <span className="control">Range</span>
        <div className="seg" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              aria-pressed={props.range === r.key}
              onClick={() => props.onRange(r.key)}
            >
              {r.label}
            </button>
          ))}
        </div>

        <span className="control">Bucket</span>
        <div className="seg" role="group" aria-label="Bucket size">
          {BUCKETS.map((b) => (
            <button
              key={b.key}
              type="button"
              aria-pressed={props.bucket === b.key}
              onClick={() => props.onBucket(b.key)}
            >
              {b.label}
            </button>
          ))}
        </div>

        {/* A view control, not a filter: nothing is excluded by turning it off,
            so it sits with range and bucket rather than with the pickers, and it
            is not counted by "Clear". One switch for every output surface at
            once -- columns, KPI, charts, calendar -- because a page where each
            of those had its own toggle is a page nobody can put back. */}
        <span className="control">View</span>
        <div className="seg" role="group" aria-label="Output token columns">
          <button
            type="button"
            aria-pressed={props.showOutput}
            onClick={() => props.onShowOutput(!props.showOutput)}
            title={
              props.showOutput
                ? 'Hide the output-token columns, charts and KPI'
                : 'Show output tokens: what was generated, what it cost, and $/Mtok out'
            }
          >
            Output
          </button>
        </div>

        <MultiPicker
          label="Model"
          options={dims?.models ?? []}
          selected={filters.models}
          onChange={(models) => onFilters({ ...filters, models })}
          render={shortModel}
          colorOf={(m) => colors.get(m)}
        />

        <MultiPicker
          label="Route"
          options={dims?.providers ?? []}
          selected={filters.providers}
          onChange={(providers) => onFilters({ ...filters, providers })}
          render={(p) => (p === '' ? 'direct' : p)}
        />

        <MultiPicker
          label="Repo"
          options={dims?.repos ?? []}
          selected={filters.repos}
          onChange={(repos) => onFilters({ ...filters, repos })}
          render={repoLabel}
        />

        <div className="seg" role="group" aria-label="Agent kind">
          {(['all', 'main', 'sub'] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={filters.subagent === v}
              onClick={() => onFilters({ ...filters, subagent: v })}
            >
              {v === 'all' ? 'All agents' : v === 'main' ? 'Main' : 'Subagents'}
            </button>
          ))}
        </div>

        {activeFilters > 0 && (
          <button
            className="btn"
            type="button"
            onClick={() =>
              onFilters({
                ...filters,
                origins: [],
                sources: [],
                models: [],
                providers: [],
                repos: [],
                subagent: 'all',
              })
            }
          >
            Clear {activeFilters}
          </button>
        )}
      </div>
    </header>
  )
}

/**
 * The error count, with what went wrong a hover away.
 *
 * A count on its own is unactionable -- eleven errors could be one bad reader
 * or eleven bad files -- and the whole point of surfacing it in the header is
 * that a scan which silently skipped files is a scan whose totals are short.
 * Grouping is done server-side by message, so what lands here is already kinds
 * rather than sightings.
 *
 * Opens on hover and on keyboard focus, and clicking pins it open: reading a
 * stack of paths with the mouse held still over a 3-line target is a fight
 * nobody should have with a tooltip.
 */
function ErrorPopover({ scan }: { scan: ScanState }) {
  const [hovered, setHovered] = useState(false)
  const [pinned, setPinned] = useState(false)
  const box = useRef<HTMLSpanElement | null>(null)
  const open = hovered || pinned

  useEffect(() => {
    if (!pinned) return
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setPinned(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setPinned(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [pinned])

  const groups = scan.error_groups ?? []
  // The group list is capped server-side, so the counts can legitimately sum to
  // less than the total. Saying so beats a dropdown that quietly disagrees with
  // the number that opened it.
  const itemised = groups.reduce((n, g) => n + g.count, 0)
  const unitemised = Math.max(scan.errors - itemised, 0)

  return (
    <span
      className="errpop"
      ref={box}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        type="button"
        className="bad errpop-trigger"
        aria-expanded={open}
        aria-haspopup="dialog"
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        onClick={() => setPinned((v) => !v)}
      >
        {scan.errors} {scan.errors === 1 ? 'error' : 'errors'}
      </button>
      {open && (
        <div className="errpop-menu" role="dialog" aria-label="Scan errors">
          <div className="errpop-head">
            {scan.errors} {scan.errors === 1 ? 'error' : 'errors'} this pass ·{' '}
            {groups.length} {groups.length === 1 ? 'kind' : 'kinds'}
          </div>
          {groups.length === 0 && <div className="errpop-item">no detail recorded</div>}
          {groups.map((g) => (
            <div className="errpop-item" key={g.message}>
              <div className="errpop-msg">
                <span className="errpop-count">{g.count}×</span> {g.message}
              </div>
              {g.last_file && (
                <div className="errpop-file" title={g.last_file}>
                  {g.sources.length > 0 && (
                    <span className="errpop-source">{g.sources.join(', ')}</span>
                  )}
                  {g.last_file.split('/').pop()}
                </div>
              )}
            </div>
          ))}
          {unitemised > 0 && (
            <div className="errpop-item errpop-more">
              + {unitemised} more, in kinds beyond the {groups.length} listed
            </div>
          )}
          <div className="errpop-foot">
            A failed file keeps its old cursor, so its bytes are missing from the
            totals and the next pass retries it. A kind that survives every pass
            is a reader bug rather than a transient.
          </div>
        </div>
      )}
    </span>
  )
}

/**
 * True once `active` has held for `delay`, and for `linger` after it drops.
 *
 * An incremental pass over a few appended lines is finished in milliseconds, so
 * drawing a progress bar for one frame is noise rather than information. The
 * linger keeps a burst of back-to-back passes from strobing.
 */
function useSustained(active: boolean, delay = 250, linger = 500): boolean {
  const [shown, setShown] = useState(false)
  useEffect(() => {
    if (active === shown) return
    const timer = window.setTimeout(() => setShown(active), active ? delay : linger)
    return () => window.clearTimeout(timer)
  }, [active, shown, delay, linger])
  return shown
}

interface RailProps {
  label: string
  options: string[]
  selected: string[]
  onChange: (next: string[]) => void
  render: (value: string) => string
  colorOf?: (value: string) => string
}

/**
 * All / one / some, in one glance.
 *
 * An empty selection means everything, so "All" is a real state rather than a
 * separate mode -- clicking the last active chip off returns to it instead of
 * showing nothing.
 */
function ScopeRail({ label, options, selected, onChange, render, colorOf }: RailProps) {
  const all = selected.length === 0
  const toggle = (value: string) =>
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value],
    )

  return (
    <div className="rail" role="group" aria-label={label}>
      <span className="control">{label}</span>
      <button
        type="button"
        className="chip"
        aria-pressed={all}
        onClick={() => onChange([])}
      >
        All
      </button>
      {options.map((value) => (
        <button
          key={value || '(local)'}
          type="button"
          className="chip"
          aria-pressed={!all && selected.includes(value)}
          onClick={() => toggle(value)}
          // Shift-click isolates: the common move once more than two exist.
          onAuxClick={(e) => {
            if (e.button === 1) onChange([value])
          }}
          title={`${render(value)} — click to toggle`}
        >
          {colorOf && <span className="swatch" style={{ background: colorOf(value) }} />}
          {render(value)}
        </button>
      ))}
    </div>
  )
}

interface PickerProps {
  label: string
  options: string[]
  selected: string[]
  onChange: (next: string[]) => void
  render: (value: string) => string
  colorOf?: (value: string) => string
}

function MultiPicker({ label, options, selected, onChange, render, colorOf }: PickerProps) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const toggle = (value: string) => {
    onChange(
      selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value],
    )
  }

  return (
    <div className="picker" ref={box}>
      <button
        className="btn"
        type="button"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((v) => !v)}
      >
        {label}
        {selected.length > 0 ? ` · ${selected.length}` : ''} ▾
      </button>
      {open && (
        <div className="picker-menu" role="listbox" aria-multiselectable="true">
          {options.length === 0 && <div className="picker-item">nothing yet</div>}
          {options.map((value) => (
            <label className="picker-item" key={value || '(direct)'}>
              <input
                type="checkbox"
                checked={selected.includes(value)}
                onChange={() => toggle(value)}
              />
              {colorOf && <span className="swatch" style={{ background: colorOf(value) }} />}
              {render(value)}
            </label>
          ))}
        </div>
      )}
    </div>
  )
}
