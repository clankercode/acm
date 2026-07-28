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

/** Identity of a *width*: the same series with a new value is a new width. */
const widthKey = (i: LegendItem) => `${i.key}:${i.label}:${i.value ?? ''}`

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
  const chipRef = useRef<HTMLButtonElement | null>(null)
  const [fit, setFit] = useState(items.length)
  const [open, setOpen] = useState(false)
  /**
   * `key:label:value` -> rendered width, and a looser `key` -> last known width.
   *
   * Needed because an item folded away is `display: none`, and a hidden element
   * measures zero. Measuring the row again -- on a resize, say -- would then find
   * the tail free of charge, unfold it, find it too wide, fold it again: a
   * legend that flickers between four names and eleven. Widths are taken while an
   * item is on screen and remembered, and items are `nowrap`, so a width stays
   * true however narrow the panel gets.
   *
   * Keyed by the text and not just the series because a folded value can grow
   * behind our back -- "$9" to "$1,234.56" -- and the old width would then be
   * used to decide that it fits. The by-key map is the fallback for exactly that
   * case: near enough to pack with for one frame, and re-checked on the next.
   */
  const widths = useRef(new Map<string, number>())
  const lastKnown = useRef(new Map<string, number>())

  const signature = useMemo(() => items.map(widthKey).join('|'), [items])

  const measure = useCallback(() => {
    const row = rowRef.current
    if (!row) return
    const style = getComputedStyle(row)
    const gap = parseFloat(style.columnGap) || 0
    // Fractional widths throughout: offsetWidth rounds to whole pixels, and a
    // dozen roundings in the same direction is a few pixels of overflow, which at
    // a fixed height is a clipped row. The 1px shaved off the line is the same
    // defensiveness about the container's own fractional width.
    const avail = row.getBoundingClientRect().width - 1
    // Both chips are measured, and room is always kept for one of them: the
    // trailing button is a real flex item, so ignoring it is how a legend that
    // "fits" ends up wrapping to a third row and losing it to the clip.
    const dots = (dotsRef.current?.getBoundingClientRect().width ?? 0) + gap
    const chip = (moreRef.current?.getBoundingClientRect().width ?? 0) + gap
    const els = [...row.querySelectorAll<HTMLElement>('[data-legend-item]')]
    if (!els.length || avail <= 0) return

    // Rebuilt from what is on screen now, so keys from series that have since
    // been filtered away do not accumulate for the life of the page.
    const fresh = new Map<string, number>()
    let unmeasured = false
    const itemWidths = els.map((el) => {
      const key = el.dataset.legendWidth ?? ''
      const seriesKey = el.dataset.legendKey ?? ''
      const w = el.getBoundingClientRect().width
      if (w > 0) {
        fresh.set(key, w)
        lastKnown.current.set(seriesKey, w)
        return w
      }
      const cached = widths.current.get(key)
      if (cached != null) {
        fresh.set(key, cached)
        return cached
      }
      // Never measured at this text. Pack with whatever this series last was and
      // verify next frame, rather than unfolding the whole legend for a frame.
      const near = lastKnown.current.get(seriesKey)
      unmeasured = true
      return near ?? 0
    })
    widths.current = fresh

    /** Greedy pack into MAX_ROWS lines; the last one has to leave room for the chip. */
    const pack = (lastRowBudget: number): number => {
      let line = 0
      let used = 0
      let placed = 0
      for (let i = 0; i < itemWidths.length; i++) {
        const w = itemWidths[i]
        const budget = line === MAX_ROWS - 1 ? lastRowBudget : avail
        const need = used === 0 ? w : used + gap + w
        if (need <= budget) {
          used = need
          placed++
          continue
        }
        if (used === 0) {
          // An empty line that still cannot hold this item. Tolerated only for
          // the very first: one clipped name beats an empty legend. Anywhere
          // else, stop -- placing it would push the chip onto a third line,
          // where the fixed height hides it and the folded series become
          // unreachable.
          if (line === 0 && placed === 0) {
            placed++
            used = w
            continue
          }
          break
        }
        line++
        if (line >= MAX_ROWS) break
        // Retried against the new line rather than assumed to fit it, which is
        // how an item wider than the last row's budget used to be placed anyway.
        used = 0
        i--
      }
      return placed
    }

    // The narrow chip if everything fits beside it, the wide one otherwise.
    const all = pack(avail - dots)
    setFit(all >= itemWidths.length ? itemWidths.length : pack(avail - chip))
    if (unmeasured) requestAnimationFrame(measureRef.current)
  }, [])

  // Held in a ref so the rAF above cannot capture a stale closure.
  const measureRef = useRef(measure)
  measureRef.current = measure

  // Before paint, so a legend is never seen four rows tall on its first frame.
  useLayoutEffect(measure, [measure, signature])

  useEffect(() => {
    const row = rowRef.current
    if (!row || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(row)
    return () => ro.disconnect()
  }, [measure])

  const overflow = Math.max(0, items.length - fit)
  const hiddenCount = items.filter((i) => i.hidden).length
  // Nothing to choose between with one series, and nothing at all with none: the
  // height is still reserved so neighbouring panels line up, but a chip opening
  // an empty dialog is furniture pretending to be a control.
  const showChip = items.length > 1

  return (
    <div className="legend-block">
      <div className="legend-row" ref={rowRef}>
        {items.map((item, i) => (
          <button
            key={item.key}
            data-legend-item
            data-legend-key={item.key}
            data-legend-width={widthKey(item)}
            type="button"
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
            {item.value != null && (
              <span className="val" title={`latest ${item.label}`}>
                {item.value}
              </span>
            )}
          </button>
        ))}

        {/* Never laid out; exists so the chip's width is known before the chip is
            needed. Sized for the largest count it could hold, which over-reserves
            by a digit at most -- the true count is not known until the packing
            this measurement feeds has already run. */}
        <span className="legend-more legend-sizer" ref={moreRef} aria-hidden="true">
          +{items.length} more
        </span>
        <span className="legend-more legend-sizer" ref={dotsRef} aria-hidden="true">
          ⋯
        </span>

        {showChip && (
          <button
            type="button"
            className="legend-more"
            ref={chipRef}
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
            title={
              overflow > 0 ? `Show the other ${overflow}` : 'Choose which series are drawn'
            }
          >
            {/* The hidden count is spelled out because folding and hiding look
                identical otherwise: a series both hidden and folded away would
                vanish from the chart under a chip that only said "+2 more". */}
            {overflow > 0 ? `+${overflow} more` : 'series'}
            {hiddenCount > 0 && <span className="legend-off">{hiddenCount} off</span>}
          </button>
        )}
      </div>

      {open && (
        <SeriesPicker
          items={items}
          onToggle={onToggle}
          onOnly={onOnly}
          onShowAll={onShowAll}
          trigger={chipRef}
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
  trigger,
}: Props & { onClose: () => void; trigger: React.RefObject<HTMLButtonElement | null> }) {
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Claimed, so the key does not also close a panel this one sits inside.
      e.preventDefault()
      e.stopPropagation()
      onClose()
      trigger.current?.focus()
    }
    // Pointerdown, not click: a click that lands on another chart should close
    // this and do that, rather than being swallowed.
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node
      if (ref.current?.contains(target)) return
      // The chip that opened this is not "outside" it. Without this exception,
      // pressing it while open closes the picker here and its own click handler
      // then toggles the freshly-false state straight back to open -- a button
      // that visibly cannot be used to close what it opens.
      if (trigger.current?.contains(target)) return
      onClose()
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onDown, true)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown, true)
    }
  }, [onClose, trigger])

  const anyHidden = items.some((i) => i.hidden)

  return (
    <div className="series-picker" ref={ref} role="dialog" aria-modal="true" aria-label="Series">
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
            {/* The one action a list of checkboxes makes tedious: nine clicks to
                look at one series. Pointless with a single series, where it can
                only mean "draw what is already drawn". */}
            {items.length > 1 && (
              <button
                type="button"
                className="series-only"
                onClick={() => onOnly(item.key)}
                title={`Draw only ${item.label}`}
              >
                only
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
