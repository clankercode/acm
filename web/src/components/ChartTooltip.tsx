import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'

/**
 * A tooltip that stays inside its chart.
 *
 * Every ad-hoc version of this got the same thing wrong: it anchored the box by
 * an edge that can leave the plot. Positioning by `bottom` puts the *bottom*
 * edge at the cursor and lets the box grow upward off the top -- which is
 * exactly where a density plot of a 93%-cached corpus puts all its mass, so the
 * tooltip was unreadable precisely where it was needed. Guessing a fixed height
 * to clamp against has the same failure mode whenever the content is taller
 * than the guess.
 *
 * So: always anchor top-left, measure what was actually rendered, then flip and
 * clamp against the real container box.
 */

const MARGIN = 8
/** Keeps the box clear of the cursor so it never covers the mark being read. */
const CURSOR_GAP = 14

interface Props {
  /** Anchor in container coordinates, px from the container's top-left. */
  x: number
  y: number
  /** The positioned ancestor to stay inside. */
  container: HTMLElement | null
  children: ReactNode
}

export function ChartTooltip({ x, y, container, children }: Props) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)

  useLayoutEffect(() => {
    const node = ref.current
    if (!node || !container) return
    const w = node.offsetWidth
    const h = node.offsetHeight
    const bounds = container.getBoundingClientRect()

    // Prefer the right of the cursor; flip only when the box would not fit.
    let left = x + CURSOR_GAP
    if (left + w > bounds.width - MARGIN) {
      left = x - CURSOR_GAP - w
    }
    left = Math.max(MARGIN, Math.min(left, bounds.width - w - MARGIN))

    // Centre on the cursor vertically, then clamp. When the box is taller than
    // the container, pinning the top is the only way to keep the first row --
    // the one naming the series -- on screen.
    let top = y - h / 2
    top = Math.max(MARGIN, Math.min(top, bounds.height - h - MARGIN))
    if (h + MARGIN * 2 > bounds.height) top = MARGIN

    setPos({ left, top })
  }, [x, y, container, children])

  return (
    <div
      ref={ref}
      className="tooltip"
      style={{
        left: pos?.left ?? x + CURSOR_GAP,
        top: pos?.top ?? y,
        // Suppress the first paint at the unmeasured position, which would
        // otherwise show as a one-frame jump on every cursor move.
        visibility: pos ? 'visible' : 'hidden',
      }}
    >
      {children}
    </div>
  )
}
