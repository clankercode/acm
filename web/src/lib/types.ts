export interface Totals {
  requests: number
  input_tokens: number
  cached_tokens: number
  /** Prompt tokens stored into the cache. Anthropic bills these above list. */
  cache_write_tokens: number
  cache_write_1h_tokens: number
  fresh_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cache_rate: number
  cache_write_rate: number
  cost: number
  /** The four components sum to `cost`. Which one dominates is not guessable. */
  cost_fresh: number
  cost_cached: number
  cost_written: number
  cost_output: number
  uncached_cost: number
  /** Prompt tokens billed at long-context rates, and what that markup cost. */
  long_tokens: number
  long_fraction: number
  long_surcharge: number
  saved: number
  saved_fraction: number
  /** USD per million input tokens processed. The headline efficiency metric. */
  effective_rate: number
  list_rate: number
  efficiency: number
  avg_context: number
  unpriced_tokens: number
}

export interface SourceScan {
  name: string
  label: string
  files_total: number
  files_done: number
  bytes_total: number
  bytes_done: number
  raw_events: number
  new_requests: number
  errors: number
}

export interface ScanState {
  phase: 'idle' | 'discovering' | 'scanning' | 'updating' | 'tailing'
  files_total: number
  files_done: number
  files_changed: number
  bytes_total: number
  bytes_done: number
  raw_events: number
  new_requests: number
  current_file: string | null
  current_source: string | null
  elapsed: number
  bytes_per_sec: number
  eta_seconds: number | null
  duplicate_fraction: number
  errors: number
  last_error: string | null
  sources: SourceScan[]
}

export interface Dimensions {
  /** Machines whose data is in the store. "" is this one. */
  origins: string[]
  sources: string[]
  models: string[]
  providers: string[]
  base_models: string[]
  repos: string[]
  first_ts: number | null
  last_ts: number | null
  requests: number
  imported_requests: number
}

/** Our cost against the client's own, over the requests it priced itself. */
export interface CostAudit {
  requests: number
  ours: number
  theirs: number
  ratio: number | null
}

export interface SourceQuality {
  source: string
  files: number
  failed_files: number
  bytes: number
  raw_token_events: number
  requests: number
  replay_ratio: number
  first_ts: number | null
  last_ts: number | null
  cost: number
  saved: number
  input_tokens: number
  cache_rate: number
  cache_write_rate: number
  effective_rate: number
  audit: CostAudit | null
}

export interface Quality {
  files: number
  failed_files: number
  bytes: number
  raw_token_events: number
  deduped_requests: number
  replay_ratio: number
  replayed_events: number
  anomalies: Record<string, number>
  unpriced_models: { model: string; input_tokens: number; requests: number }[]
  estimated_pricing: string[]
  sources: SourceQuality[]
}

export interface ModelRate {
  input: number
  cached_input: number
  output: number
  cache_write: number
  cache_write_1h: number
  long_input: number
  long_cached_input: number
  long_output: number
  threshold: number
  has_long_tier: boolean
  charges_cache_writes: boolean
  long_tier_unknown: boolean
  estimated: boolean
  source: string | null
}

export interface Pricing {
  path: string
  default_threshold: number
  models: Record<string, ModelRate>
  unpriced: string[]
}

export interface SourceInfo {
  name: string
  label: string
  root: string
}

export interface Machine {
  origin: string
  label: string
  local: boolean
  machine: string | null
  imported_at: number | null
  exported_at: number | null
  /** Every machine that contributed, including through an earlier import. */
  contributors: string[]
  requests: number
  input_tokens: number
  cost: number
  cache_rate: number
  effective_rate: number
}

export interface ImportPreview {
  label: string
  suggested_label: string
  collision: boolean
  machine: string | null
  exported_at: number | null
  tool_version: string | null
  contributors: string[]
  buckets: number
  sessions: number
  summary: {
    requests?: number
    input_tokens?: number
    sessions?: number
    clients?: string[]
    first_ts?: number | null
    last_ts?: number | null
  }
}

export interface ReferenceField {
  field: string
  ours: number
  theirs: number | null
  tokens: number
  /** `inert` means the rates differ in a category with no observed tokens. */
  state: 'match' | 'differs' | 'inert' | 'unlisted'
}

export interface ReferenceModel {
  model: string
  estimated: boolean
  observed_tokens: number
  provider: string | null
  offers?: number
  fields: ReferenceField[]
  status: 'match' | 'differs' | 'inert' | 'unlisted'
}

export interface ReferenceStatus {
  url: string
  fetched_at: number | null
  available: boolean
  providers: number
  error: string | null
  models: ReferenceModel[]
}

export interface AppState {
  generation: number
  scan: ScanState
  totals: Totals
  quality: Quality
  dimensions: Dimensions
  pricing: Pricing
  reference: Omit<ReferenceStatus, 'models'>
  local_label: string
  sessions_dir: string
  sources: SourceInfo[]
  server_time: number
}

export type Nums = (number | null)[]

export interface SeriesColumns {
  n: Nums
  input: Nums
  cached: Nums
  written: Nums
  output: Nums
  reasoning: Nums
  cost: Nums
  cost_fresh: Nums
  cost_cached: Nums
  cost_written: Nums
  cost_output: Nums
  uncached: Nums
  /** What crossing the long-context threshold added, and the tokens that did. */
  surcharge: Nums
  long_input: Nums
}

export interface SeriesGroup extends SeriesColumns {
  key: string
}

export interface SeriesResponse {
  bucket_seconds: number
  /** Bucket start times, epoch seconds, contiguous with nulls for idle gaps. */
  t: number[]
  groups: SeriesGroup[]
  total: SeriesColumns | null
}

export interface BreakdownRow extends Totals {
  key: string
}

export interface SessionRow extends Totals {
  rollout_id: string
  source: string
  session_id: string
  cwd: string | null
  repo: string
  branch: string | null
  agent_role: string | null
  agent_nickname: string | null
  depth: number | null
  is_subagent: boolean
  cli_version: string | null
  path: string
  model: string | null
  models: string[]
  first_ts: number
  last_ts: number
  duration_ms: number
}

export interface SessionDetail {
  meta: Record<string, unknown> | null
  totals: Totals
  requests: {
    ts: number
    model: string | null
    input: number
    cached: number
    output: number
    reasoning: number
    cache_rate: number
    cost: number
    ctx_window: number | null
    quota_percent: number | null
  }[]
  events: { ts: number; kind: string }[]
}

export interface HeatCell extends Totals {
  day: number
  hour: number
}

export interface CalendarDay extends Totals {
  /** Days since the epoch, in the viewer's local time. */
  day: number
  top: { key: string; cost: number }[]
}

export interface ScatterGrid {
  bins: { x: number; y: number; n: number }[]
  x_log_min: number
  x_log_max: number
  size: number
  count: number
  max_input: number
}

export interface EventMarker {
  ts: number
  kind: string
  rollout_id: string | null
}

export interface Filters {
  start: number | null
  end: number | null
  origins: string[]
  sources: string[]
  models: string[]
  providers: string[]
  repos: string[]
  subagent: 'all' | 'main' | 'sub'
}

export const EMPTY_FILTERS: Filters = {
  start: null,
  end: null,
  origins: [],
  sources: [],
  models: [],
  providers: [],
  repos: [],
  subagent: 'all',
}

export const SOURCE_LABELS: Record<string, string> = {
  codex: 'Codex',
  claude: 'Claude Code',
  pi: 'Pi',
  opencode: 'OpenCode',
  grok: 'Grok',
}

export const sourceLabel = (name: string) => SOURCE_LABELS[name] ?? name
