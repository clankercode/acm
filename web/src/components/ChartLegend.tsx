import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

/** Rows of names a chart may spend before the rest becomes a count. */
const MAX_ROWS = 2

export interface LegendItem {
  key: string
  label: string
  color: string
  /** Formatted latest value, shown after the name when there is room. */
  value?: string
  hidden?: boolean
}

interface Props {
  items: LegendItem[]
  onToggle: (key: string) => void
  /** Show this series alone. */
  onOnly: (key: string) => void
  onShowAll: () => void
}

/**
 * One legend for every chart, the same height on all of them.
 *
 * Charts here plot up to ten models plus an aggregate, and model names are long:
 * laid out freely, one legend takes a line and its neighbour takes four, so two
 * charts side by side start their plots at different heights and a column of
 * panels never lines up. So the box is fixed at two rows and the names that do
 * not fit become "+N more", which opens the full list -- the overflow is not
 * lost, it moves somewhere with room for it.
 *
 * Packing is measured, not estimated: widths come from the rendered items, since
 * a proportional face and arbitrary model names make any character count wrong.
 */
export function ChartLegend({ items, onToggle, onOnly, onShowAll }: Props) {
  const rowRef = useRef<HTMLDivElement | null>(null)
  const moreRef = useRef<HTMLSpanElement | null>(null)
  const dotsRef = useRef<HTMLSpanElement | null>(null)
  const [fit, setFit] = useState(items.length)
  const [open, setOpen] = useState(false)
  /**
   * Item key -> rendered width.
   *
   * Needed because an item folded away is `display: none`, and a hidden element
   * measures zero. Measuring the row again -- on a resize, say -- would then find
   * the tail free of charge, unfold it, find it too wide, fold it again: a
   * legend that flickers between four names and eleven. Widths are taken while an
   * item is on screen and remembered, and items are `nowrap`, so a width stays
   * true however narrow the panel gets.
   */
  const widths = useRef(new Map<string, number>())

  // Text, not identity: the same series with a new value is a new width.
  const signature = useMemo(
    () => items.map((i) => `${i.key}:${i.label}:${i.value ?? ''}`).join('|'),
    [items],
  )

  const measure = useCallback(() => {
    const row = rowRef.current
    if (!row) return
    const style = getComputedStyle(row)
    const gap = parseFloat(style.columnGap) || 0
    const avail = row.clientWidth
    // Both chips are measured, and room is always kept for one of them: the
    // trailing button is a real flex item, so ignoring it is how a legend that
    // "fits" ends up wrapping to a third row and losing it to the clip.
    const dots = (dotsRef.current?.offsetWidth ?? 0) + gap
    const chip = (moreRef.current?.offsetWidth ?? 0) + gap
    const els = [...row.querySelectorAll<HTMLElement>('[data-legend-item]')]
    if (!els.length || avail <= 0) return

    let unmeasured = false
    const itemWidths = els.map((el) => {
      const key = el.dataset.legendKey ?? ''
      const w = el.offsetWidth
      if (w > 0) {
        widths.current.set(key, w)
        return w
      }
      const cached = widths.current.get(key)
      if (cached == null) {
        unmeasured = true
        return 0
      }
      return cached
    })
    // A name whose width is not known yet: show everything for one frame, which
    // is what makes it measurable, then decide.
    if (unmeasured) {
      setFit(els.length)
      requestAnimationFrame(measureRef.current)
      return
    }

    /** Greedy pack into `rows` lines, each `budget` wide bar the last. */
    const pack = (lastRowBudget: number): number => {
      let row = 0
      let used = 0
      let placed = 0
      for (const w of itemWidths) {
        const budget = row === MAX_ROWS - 1 ? lastRowBudget : avail
        const need = used === 0 ? w : used + gap + w
        if (need <= budget) {
          used = need
          placed++
          continue
        }
        row++
        if (row >= MAX_ROWS) break
        used = w
        // A single item wider than the line still takes the line: it will be
        // clipped by the box rather than dropped, which is better than an
        // empty legend.
        placed++
      }
      return placed
    }

    // The narrow chip if everything fits beside it, the wide one otherwise.
    const all = pack(avail - dots)
    setFit(all >= itemWidths.length ? itemWidths.length : pack(avail - chip))
  }, [])

  // Held in a ref so the rAF above cannot capture a stale closure.
  const measureRef = useRef(measure)
  measureRef.current = measure

  // Before paint, so a legend is never seen four rows tall on its first frame.
  // The remembered widths are deliberately *not* dropped when a value changes:
  // live figures tick every poll, and re-measuring from scratch each time would
  // unfold the whole legend for a frame on every update. Items on screen
  // re-measure anyway; a folded one carrying a slightly stale width is a pixel
  // or two out in a packing decision, which nothing depends on.
  useLayoutEffect(measure, [measure, signature])

  useEffect(() => {
    const row = rowRef.current
    if (!row || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(row)
    return () => ro.disconnect()
  }, [measure])

  const overflow = Math.max(0, items.length - fit)

  return (
    <div className="legend-block">
      <div className="legend-row" ref={rowRef} role="list">
        {items.map((item, i) => (
          <button
            key={item.key}
            data-legend-item
            data-legend-key={item.key}
            type="button"
            role="listitem"
            className="legend-item"
            // Hidden past the fold rather than unmounted: the widths measured
            // above are the widths of these elements.
            style={i < fit ? undefined : { display: 'none' }}
            aria-hidden={i < fit ? undefined : true}
            tabIndex={i < fit ? undefined : -1}
            aria-pressed={!item.hidden}
            onClick={() => onToggle(item.key)}
            title={`Toggle ${item.label}`}
          >
            <span className="swatch" style={{ background: item.color }} />
            {item.label}
            {item.value != null && <span className="val">{item.value}</span>}
          </button>
        ))}

        {/* Never laid out; exists so the chip's width is known before the chip
            is needed. Sized for the largest count it could hold. */}
        <span className="legend-more legend-sizer" ref={moreRef} aria-hidden="true">
          +{items.length} more
        </span>
        <span className="legend-more legend-sizer" ref={dotsRef} aria-hidden="true">
          ⋯
        </span>

        <button
          type="button"
          className="legend-more"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          title={overflow > 0 ? `Show the other ${overflow}` : 'Choose which series are drawn'}
        >
          {overflow > 0 ? `+${overflow} more` : '⋯'}
        </button>
      </div>

      {open && (
        <SeriesPicker
          items={items}
          onToggle={onToggle}
          onOnly={onOnly}
          onShowAll={onShowAll}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  )
}

function SeriesPicker({
  items,
  onToggle,
  onOnly,
  onShowAll,
  onClose,
}: Props & { onClose: () => void }) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    // Pointerdown, not click: a click that lands on another chart should close
    // this and do that, rather than being swallowed.
    const onDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onDown, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown, true)
    }
  }, [onClose])

  const anyHidden = items.some((i) => i.hidden)

  return (
    <div className="series-picker" ref={ref} role="dialog" aria-label="Series">
      <div className="series-picker-head">
        <span className="control">Series</span>
        <button type="button" className="btn" onClick={onShowAll} disabled={!anyHidden}>
          Show all
        </button>
      </div>
      <div className="series-picker-list">
        {items.map((item) => (
          <div className="series-row" key={item.key}>
            <button
              type="button"
              className="series-toggle"
              aria-pressed={!item.hidden}
              onClick={() => onToggle(item.key)}
            >
              <span className="swatch" style={{ background: item.color }} />
              <span className="series-name">{item.label}</span>
              {item.value != null && <span className="val">{item.value}</span>}
            </button>
            {/* The one action a list of checkboxes makes tedious: nine clicks
                to look at one series. */}
            <button
              type="button"
              className="series-only"
              onClick={() => onOnly(item.key)}
              title={`Draw only ${item.label}`}
            >
              only
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
