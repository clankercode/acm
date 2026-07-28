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
  const substantial =
    !!scan &&
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
          <span className={'dot ' + (!connected ? 'down' : busy ? 'busy' : 'live')} />
          {/* One word, in a fixed footprint. The counts belong beside the bar,
              where they can grow a digit without shunting the row along. */}
          {!connected
            ? 'disconnected'
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
              <span className="bad"> · {scan.errors} errors</span>
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
                  {scan!.eta_seconds != null && ` · ${seconds(scan!.eta_seconds)} left`}
                </span>
              )}
            </>
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

        <button
          className="btn"
          type="button"
          onClick={() => {
            setPending(true)
            api.rescan(false)
          }}
          title="Check the corpus for new sessions now"
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
