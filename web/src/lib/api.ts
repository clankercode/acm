import type {
  AppState,
  BreakdownRow,
  CalendarDay,
  EventMarker,
  Filters,
  HeatCell,
  ImportPreview,
  Machine,
  Pricing,
  ReferenceStatus,
  ScatterGrid,
  SeriesResponse,
  SessionDetail,
  SessionRow,
  Totals,
  UpdateStatus,
} from './types'

function query(filters: Filters, extra: Record<string, string | number> = {}) {
  const p = new URLSearchParams()
  if (filters.start != null) p.set('start', String(Math.round(filters.start)))
  if (filters.end != null) p.set('end', String(Math.round(filters.end)))
  // "" is this machine, which an empty query value cannot express.
  for (const o of filters.origins) p.append('origin', o === '' ? 'local' : o)
  for (const s of filters.sources) p.append('source', s)
  for (const m of filters.models) p.append('model', m)
  // "" means direct routing, which an empty query value cannot express.
  for (const v of filters.providers) p.append('provider', v === '' ? 'direct' : v)
  for (const r of filters.repos) p.append('repo', r)
  if (filters.subagent !== 'all') p.set('subagent', filters.subagent)
  for (const [k, v] of Object.entries(extra)) p.set(k, String(v))
  return p.toString()
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal, headers: { accept: 'application/json' } })
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${path}`)
  return res.json() as Promise<T>
}

export const api = {
  state: (signal?: AbortSignal) => get<AppState>('/api/state', signal),

  totals: (f: Filters, signal?: AbortSignal) =>
    get<Totals>(`/api/totals?${query(f)}`, signal),

  series: (
    f: Filters,
    opts: { bucket: string; group?: string; limit_groups?: number },
    signal?: AbortSignal,
  ) =>
    get<SeriesResponse>(
      `/api/series?${query(f, {
        bucket: opts.bucket,
        ...(opts.group ? { group: opts.group } : {}),
        ...(opts.limit_groups ? { limit_groups: opts.limit_groups } : {}),
      })}`,
      signal,
    ),

  breakdown: (f: Filters, dimension: string, signal?: AbortSignal) =>
    get<BreakdownRow[]>(`/api/breakdown/${dimension}?${query(f)}`, signal),

  heatmap: (f: Filters, tzOffsetMinutes: number, signal?: AbortSignal) =>
    get<{ cells: HeatCell[] }>(
      `/api/heatmap?${query(f, { tz_offset: tzOffsetMinutes })}`,
      signal,
    ),

  /** Whole history by local day; the calendar paginates months itself. */
  calendar: (f: Filters, tzOffsetMinutes: number, signal?: AbortSignal) =>
    get<{ days: CalendarDay[] }>(
      `/api/calendar?${query(f, { tz_offset: tzOffsetMinutes })}`,
      signal,
    ),

  scatter: (f: Filters, bins: number, signal?: AbortSignal) =>
    get<ScatterGrid>(`/api/scatter?${query(f, { bins })}`, signal),

  events: (f: Filters, signal?: AbortSignal) =>
    get<EventMarker[]>(`/api/events?${query(f)}`, signal),

  sessions: (f: Filters, signal?: AbortSignal) =>
    get<{ total: number; rows: SessionRow[] }>(`/api/sessions?${query(f)}`, signal),

  session: (id: string, signal?: AbortSignal) =>
    get<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`, signal),

  pricing: (signal?: AbortSignal) => get<Pricing>('/api/pricing', signal),

  async savePricing(models: Record<string, unknown>): Promise<Pricing> {
    const res = await fetch('/api/pricing', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ models }),
    })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  },

  /** Throws while scanning is paused (409), so callers must say what they do then. */
  async rescan(full: boolean): Promise<void> {
    const res = await fetch(`/api/rescan?full=${full}`, { method: 'POST' })
    if (!res.ok) throw new Error(await res.text())
  },

  /** Stop or restart the scan loop. The in-flight pass is cancelled, not awaited. */
  async setPaused(paused: boolean): Promise<boolean> {
    const res = await fetch(`/api/scan/${paused ? 'pause' : 'resume'}`, { method: 'POST' })
    if (!res.ok) throw new Error(await res.text())
    return ((await res.json()) as { paused: boolean }).paused
  },

  // -- self-update --------------------------------------------------------

  updateStatus: (signal?: AbortSignal) => get<UpdateStatus>('/api/update', signal),

  /**
   * Pull, rebuild, reinstall and restart. Refused (403) from anywhere but the
   * machine running the monitor, and (409) when one is already in flight.
   *
   * The server it is asked of is the server it replaces, so the last thing this
   * starts is the death of the connection it was asked over. Callers should poll
   * the status and expect the failures that come with that.
   */
  async startUpdate(): Promise<UpdateStatus> {
    const res = await fetch('/api/update', { method: 'POST' })
    if (!res.ok) throw new Error(((await res.json()) as { detail?: string }).detail ?? 'failed')
    return res.json()
  },

  // -- reference prices ---------------------------------------------------

  reference: (signal?: AbortSignal) =>
    get<ReferenceStatus>('/api/reference-prices', signal),

  async refreshReference(): Promise<ReferenceStatus> {
    const res = await fetch('/api/reference-prices/refresh', { method: 'POST' })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  },

  // -- machines -----------------------------------------------------------

  machines: (signal?: AbortSignal) =>
    get<{ local_label: string; machines: Machine[] }>('/api/machines', signal),

  /** Streams the bundle straight to a file; it never enters React state. */
  downloadExport(label: string, origins: string[]) {
    const p = new URLSearchParams({ label })
    for (const o of origins) p.append('origin', o === '' ? 'local' : o)
    window.location.href = `/api/export?${p.toString()}`
  },

  async previewImport(bundle: unknown): Promise<ImportPreview> {
    const res = await fetch('/api/import/preview', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(bundle),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? (await res.text()))
    return res.json()
  },

  async commitImport(bundle: unknown, label: string): Promise<{ machines: Machine[] }> {
    const res = await fetch('/api/import', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ bundle, label }),
    })
    if (!res.ok) throw new Error((await res.json()).detail ?? (await res.text()))
    return res.json()
  },

  async renameMachine(origin: string, label: string) {
    await fetch(`/api/machines/${encodeURIComponent(origin)}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ label }),
    })
  },

  async deleteMachine(origin: string) {
    await fetch(`/api/machines/${encodeURIComponent(origin)}`, { method: 'DELETE' })
  },
}
