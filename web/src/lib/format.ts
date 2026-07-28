const NBSP = ' ' // thin space, keeps "12.3 M" from splitting across lines

export function compact(n: number | null | undefined, digits = 2): string {
  if (n == null || !isFinite(n)) return '--'
  const abs = Math.abs(n)
  if (abs >= 1e12) return `${(n / 1e12).toFixed(digits)}${NBSP}T`
  if (abs >= 1e9) return `${(n / 1e9).toFixed(digits)}${NBSP}B`
  if (abs >= 1e6) return `${(n / 1e6).toFixed(digits)}${NBSP}M`
  if (abs >= 1e3) return `${(n / 1e3).toFixed(abs >= 1e4 ? 0 : 1)}${NBSP}k`
  return n.toFixed(0)
}

export function integer(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '--'
  return Math.round(n).toLocaleString()
}

export function usd(n: number | null | undefined, digits?: number): string {
  if (n == null || !isFinite(n)) return '--'
  const abs = Math.abs(n)
  const d = digits ?? (abs >= 1000 ? 0 : abs >= 1 ? 2 : 4)
  return `$${n.toLocaleString(undefined, {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  })}`
}

/** The headline metric, always at four decimals so small moves stay visible. */
export function rate(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '--'
  return `$${n.toFixed(4)}`
}

export function pct(n: number | null | undefined, digits = 1): string {
  if (n == null || !isFinite(n)) return '--'
  return `${(n * 100).toFixed(digits)}%`
}

export function bytes(n: number | null | undefined): string {
  if (n == null || !isFinite(n)) return '--'
  const units = ['B', 'kB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)}${NBSP}${units[i]}`
}

export function duration(ms: number | null | undefined): string {
  if (ms == null || !isFinite(ms) || ms <= 0) return '--'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${NBSP}${s % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h${NBSP}${m % 60}m`
  return `${Math.floor(h / 24)}d${NBSP}${h % 24}h`
}

export function seconds(s: number | null | undefined): string {
  if (s == null || !isFinite(s)) return '--'
  if (s < 60) return `${s.toFixed(0)}s`
  return duration(s * 1000)
}

const dtf = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const dtfDay = new Intl.DateTimeFormat(undefined, {
  weekday: 'short',
  month: 'short',
  day: 'numeric',
})

const dtfFull = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'medium',
})

export function timeLabel(epochMs: number, bucketSeconds: number): string {
  const d = new Date(epochMs)
  return bucketSeconds >= 86400 ? dtfDay.format(d) : dtf.format(d)
}

export function fullTime(epochMs: number | null | undefined): string {
  if (epochMs == null) return '--'
  return dtfFull.format(new Date(epochMs))
}

export function shortModel(model: string): string {
  return model.replace(/^([a-z]+)\//, '$1:')
}

export function repoLabel(repo: string | null | undefined): string {
  if (!repo) return 'unknown'
  const cleaned = repo.replace(/\.git$/, '')
  const parts = cleaned.split('/').filter(Boolean)
  return parts[parts.length - 1] || cleaned
}

/** Relative change where a rise is bad (cost, effective rate). */
export function delta(current: number, previous: number): {
  text: string
  cls: 'up' | 'down' | 'flat'
} {
  if (!isFinite(previous) || previous === 0) return { text: '', cls: 'flat' }
  const change = (current - previous) / previous
  if (Math.abs(change) < 0.005) return { text: 'flat', cls: 'flat' }
  const sign = change > 0 ? '+' : ''
  // Past a few multiples the exact percentage stops meaning anything; a
  // multiplier reads better than "+1,240%".
  const text =
    Math.abs(change) >= 3
      ? `${change > 0 ? '' : '-'}${(Math.abs(change) + (change > 0 ? 1 : 0)).toFixed(1)}x`
      : `${sign}${(change * 100).toFixed(Math.abs(change) >= 1 ? 0 : 1)}%`
  return { text, cls: change > 0 ? 'up' : 'down' }
}

/**
 * Change in a rate expressed in percentage points, where a fall is bad.
 *
 * Cache rate belongs on this scale: going from 97% to 85% is a 12 point drop,
 * which is how people reason about it. Expressed as relative change in the miss
 * rate the same move reads "+400%", which is true and useless.
 */
export function deltaPoints(current: number, previous: number): {
  text: string
  cls: 'up' | 'down' | 'flat'
} {
  if (!isFinite(previous)) return { text: '', cls: 'flat' }
  const points = (current - previous) * 100
  if (Math.abs(points) < 0.1) return { text: 'flat', cls: 'flat' }
  const sign = points > 0 ? '+' : ''
  return {
    text: `${sign}${points.toFixed(1)} pp`,
    // A rising cache rate is good, so the "good" class is the rising one here.
    cls: points > 0 ? 'down' : 'up',
  }
}
