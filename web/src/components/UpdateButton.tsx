import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import type { UpdateStatus } from '../lib/types'

/** While an update runs, how often to ask how it is going. */
const POLL_MS = 1500

/**
 * Deploy a new version from the dashboard: pull, rebuild, reinstall, restart.
 *
 * Confirmed first, and the confirmation says what will happen rather than asking
 * "are you sure": the button restarts the service it is served from, which drops
 * every open dashboard including this one.
 *
 * The server is replaced mid-request by design, so a lost connection is a
 * *normal* outcome here, not an error to report. What the panel watches is the
 * transcript the script writes to disk -- that survives the restart, and it is
 * the only place a failure can be seen once the terminal is gone.
 */
export function UpdateButton() {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [watching, setWatching] = useState(false)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      setStatus(await api.updateStatus(signal))
    } catch {
      // Expected while the service is restarting; the next poll finds it back.
    }
  }, [])

  useEffect(() => {
    const ac = new AbortController()
    refresh(ac.signal)
    return () => ac.abort()
  }, [refresh])

  // Only polls while something is happening: an idle dashboard has no reason to
  // ask about updates every second and a half.
  useEffect(() => {
    if (!watching) return
    const timer = window.setInterval(() => refresh(), POLL_MS)
    return () => window.clearInterval(timer)
  }, [watching, refresh])

  // Stop polling once the attempt is over, whether or not it managed to say how
  // it went. An update killed by the very restart it asked for -- the usual fate
  // where systemd-run is unavailable -- writes no outcome at all, and waiting for
  // one leaves the panel saying "Updating…" forever after a *successful* update.
  // `seenRunning` is what keeps that from firing in the gap between the request
  // and the marker, where an attempt is neither running nor finished.
  const seenRunning = useRef(false)
  useEffect(() => {
    if (!watching || !status) return
    if (status.running) seenRunning.current = true
    else if (status.outcome || seenRunning.current) setWatching(false)
  }, [watching, status])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    const onDown = (e: PointerEvent) => {
      if (!panelRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onDown, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown, true)
    }
  }, [open])

  const running = watching || !!status?.running

  async function start() {
    // Guarded against a second click while the first POST is still open: the
    // server claims its lock when the request arrives, so two clicks in quick
    // succession are two updates racing over one checkout.
    if (starting) return
    setError(null)
    setStarting(true)
    setWatching(true)
    seenRunning.current = false
    try {
      setStatus(await api.startUpdate())
    } catch (e) {
      setWatching(false)
      setError(e instanceof Error ? e.message : 'could not start the update')
    } finally {
      setStarting(false)
    }
  }

  return (
    <div className="update-holder">
      {/* aria-disabled rather than disabled, as elsewhere in this bar: a
          disabled button has no tooltip, and the tooltip is where the reason
          lives. */}
      <button
        className={'btn' + (running ? ' primary' : '')}
        type="button"
        aria-disabled={status ? !status.available && !running : true}
        aria-expanded={open}
        onClick={() => {
          if (running) return setOpen(true)
          if (!status?.available) return
          setOpen((v) => !v)
        }}
        title={
          running
            ? 'An update is running — show its progress'
            : (status?.reason ?? 'Pull, rebuild, reinstall and restart the service')
        }
      >
        {running ? 'Updating…' : 'Update'}
      </button>

      {open && (
        <div className="update-panel" ref={panelRef} role="dialog" aria-label="Update">
          {!running && !status?.log && (
            <>
              <p className="update-lede">
                Pull, rebuild, reinstall and restart the service. The dashboard
                will disconnect while it restarts and then offer to reload.
              </p>
              {status?.checkout && <p className="update-path">{status.checkout}</p>}
              <p className="panel-note">
                A local checkout with uncommitted work or its own commits will
                stop the pull, and nothing will be installed.
              </p>
              <div className="update-actions">
                <button className="btn" type="button" onClick={() => setOpen(false)}>
                  Cancel
                </button>
                <button
                  className="btn primary"
                  type="button"
                  onClick={start}
                  aria-disabled={starting}
                >
                  {starting ? 'Starting…' : 'Update now'}
                </button>
              </div>
            </>
          )}

          {(running || !!status?.log) && (
            <>
              <div className="update-head">
                <span className="control">
                  {running
                    ? 'Updating'
                    : status?.outcome === 'failed'
                      ? 'Update failed'
                      : status?.outcome === 'ok'
                        ? 'Last update'
                        : status?.outcome === 'partial'
                          ? // Installed, but this page is still being served by the
                            // old build, and nothing else will say so: the build id
                            // has not changed, so no reload prompt fires.
                            'Installed — restart needed'
                          : // No outcome recorded at all. The log is the only
                            // honest answer.
                            'Update ended — see the log'}
                </span>
                <button className="btn" type="button" onClick={() => setOpen(false)}>
                  Close
                </button>
              </div>
              {/* The transcript verbatim: an update fails for reasons only its
                  own output explains, and paraphrasing them here would lose the
                  one thing worth reading. */}
              <pre className="update-log">{status?.log || 'starting…'}</pre>
              {!running && status && !status.available && status.reason && (
                <p className="panel-note">{status.reason}</p>
              )}
              {!running && status?.available && (
                <div className="update-actions">
                  <button
                    className="btn"
                    type="button"
                    onClick={start}
                    aria-disabled={starting}
                  >
                    {starting ? 'Starting…' : 'Run again'}
                  </button>
                </div>
              )}
            </>
          )}

          {error && <p className="update-error">{error}</p>}
        </div>
      )}
    </div>
  )
}
