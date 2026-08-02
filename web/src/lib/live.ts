import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { AppState, Filters, ScanState } from './types'

/**
 * Connection to the server's event stream.
 *
 * Two event kinds arrive. `scan` is progress only and lands often, driving the
 * progress bar. `data` carries a generation counter and the running totals, and
 * only fires when stored rows actually changed -- that counter is what tells the
 * charts to refetch, so a quiet poll cycle costs nothing.
 */
/**
 * Opt out of the live stream with `?live=0`.
 *
 * Renders a one-shot snapshot from `/api/state`. Screenshot tools and print
 * need this: an open EventSource is a network request that never completes, so
 * a headless browser waiting for the page to settle waits forever.
 */
export function liveDisabled(): boolean {
  return new URLSearchParams(window.location.search).get('live') === '0'
}

export function useLiveState() {
  const [state, setState] = useState<AppState | null>(null)
  const [scan, setScan] = useState<ScanState | null>(null)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The id of a server build that is not the one this page loaded from, or null
  // while they still agree. Carries the id rather than a flag so that a second
  // upgrade is distinguishable from the first: a prompt the user dismissed has
  // to come back when the server moves on again.
  const [newBuild, setNewBuild] = useState<string | null>(null)
  const build = useRef<string | null>(null)

  useEffect(() => {
    let source: EventSource | null = null
    let retry: number | undefined
    let closed = false
    let backoff = 1000

    /**
     * Take a full snapshot, from the first fetch or from a stream (re)connect.
     *
     * The build id is compared rather than stored per connect: `just update`
     * replaces the wheel and restarts the unit, so a tab that reconnects to a
     * newer server is holding asset URLs that server no longer has. The stream
     * dropping is the cue to look, and the id is what confirms it.
     */
    const adopt = (payload: AppState) => {
      const id = payload.build?.id ?? null
      if (build.current === null) build.current = id
      else if (id !== null && id !== build.current) setNewBuild(id)
      setState(payload)
      setScan(payload.scan)
    }

    const connect = () => {
      if (closed) return
      source = new EventSource('/api/stream')

      source.addEventListener('open', () => {
        setConnected(true)
        setError(null)
        backoff = 1000
      })

      source.addEventListener('hello', (e) => {
        adopt(JSON.parse((e as MessageEvent).data) as AppState)
      })

      source.addEventListener('scan', (e) => {
        setScan(JSON.parse((e as MessageEvent).data) as ScanState)
      })

      source.addEventListener('data', (e) => {
        const payload = JSON.parse((e as MessageEvent).data) as Pick<
          AppState,
          'generation' | 'totals' | 'quality' | 'dimensions'
        >
        setState((prev) => (prev ? { ...prev, ...payload } : prev))
      })

      source.addEventListener('error', () => {
        setConnected(false)
        source?.close()
        if (closed) return
        // EventSource retries on its own, but only for transport hiccups; an
        // explicit reconnect also recovers from a server restart.
        retry = window.setTimeout(connect, backoff)
        backoff = Math.min(backoff * 2, 15000)
      })
    }

    // Hydrate immediately so the page is never blank while the stream opens.
    api
      .state()
      .then((s) => {
        adopt(s)
        if (liveDisabled()) setConnected(true)
      })
      .catch((e: Error) => setError(e.message))

    if (!liveDisabled()) connect()
    return () => {
      closed = true
      window.clearTimeout(retry)
      source?.close()
    }
  }, [])

  return { state, scan, connected, error, newBuild }
}

/**
 * Fetch that re-runs when the filters or the data generation change.
 *
 * Keeps the previous value visible while refetching, so a live update refreshes
 * numbers in place instead of blanking every chart.
 */
export function useQuery<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
  enabled = true,
): { data: T | null; loading: boolean; error: string | null; refetch: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState<string | null>(null)
  // Lets a caller re-run after a mutation it made itself, without waiting for
  // the scanner's generation counter to notice.
  const [nonce, setNonce] = useState(0)
  const ref = useRef(fetcher)
  ref.current = fetcher

  useEffect(() => {
    if (!enabled) {
      setLoading(false)
      return
    }
    const controller = new AbortController()
    let live = true
    setLoading(true)
    ref
      .current(controller.signal)
      .then((value) => {
        if (!live) return
        setData(value)
        setError(null)
      })
      .catch((e: Error) => {
        if (!live || e.name === 'AbortError') return
        setError(e.message)
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, nonce])

  return { data, loading, error, refetch: () => setNonce((n) => n + 1) }
}

/** Serialises filters into a dependency key. */
export function filterKey(f: Filters): string {
  return JSON.stringify([
    f.start,
    f.end,
    f.origins,
    f.sources,
    f.models,
    f.providers,
    f.repos,
    f.subagent,
  ])
}

/**
 * Whether the output-token surfaces are on show.
 *
 * Off by default and remembered, because it is a standing interest rather than a
 * per-visit one: someone watching generation cost wants it every time they open
 * the page, and someone watching cache behaviour never wants the extra columns.
 * `?output=1` overrides the stored choice so a screenshot or a shared link can
 * pin either view.
 */
export function useOutputView(): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState<boolean>(() => {
    const forced = new URLSearchParams(window.location.search).get('output')
    if (forced != null) return forced !== '0'
    return localStorage.getItem('acm-output') === '1'
  })
  useEffect(() => {
    localStorage.setItem('acm-output', on ? '1' : '0')
  }, [on])
  return [on, setOn]
}

export type ThemeChoice = 'system' | 'light' | 'dark'

export function useTheme(): [ThemeChoice, (t: ThemeChoice) => void, number] {
  const [choice, setChoice] = useState<ThemeChoice>(() => {
    // `?theme=` wins over the stored preference, so a link can pin a theme and
    // a screenshot can request one.
    const forced = new URLSearchParams(window.location.search).get('theme')
    if (forced === 'light' || forced === 'dark' || forced === 'system') return forced
    return (localStorage.getItem('acm-theme') as ThemeChoice) || 'system'
  })
  // Bumped whenever the resolved theme changes, so charts re-read CSS variables.
  const [epoch, setEpoch] = useState(0)

  useEffect(() => {
    const root = document.documentElement
    if (choice === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', choice)
    localStorage.setItem('acm-theme', choice)
    setEpoch((n) => n + 1)
  }, [choice])

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setEpoch((n) => n + 1)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const set = useCallback((t: ThemeChoice) => setChoice(t), [])
  return [choice, set, epoch]
}

/** Width of an element, tracked so charts can be sized imperatively. */
export function useElementWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [width, setWidth] = useState(0)

  useEffect(() => {
    const node = ref.current
    if (!node) return
    const observer = new ResizeObserver((entries) => {
      const w = Math.floor(entries[0].contentRect.width)
      setWidth((prev) => (Math.abs(prev - w) > 1 ? w : prev))
    })
    observer.observe(node)
    setWidth(Math.floor(node.getBoundingClientRect().width))
    return () => observer.disconnect()
  }, [])

  return [ref, width] as const
}
