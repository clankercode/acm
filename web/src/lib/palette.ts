/**
 * Colour assignment.
 *
 * Slots come from a validated categorical palette. The first six hues are the
 * strongly separated set (checked for CVD separation and surface contrast in
 * both themes); four more follow as a second tier, less distinct from each
 * other but preferable to the alternative, which was a busy month's seventh
 * model vanishing into a grey band nobody can read a number off. Two rules
 * matter:
 *
 * 1. Hues are assigned in fixed order and never cycled. An eleventh series
 *    folds into "Other" rather than reusing a hue.
 * 2. Colour follows the entity, not its current rank. The ordering comes from
 *    the unfiltered global model list, so narrowing a filter never repaints the
 *    series that survive it.
 */

export const SLOT_COUNT = 10

const SLOT_VARS = Array.from({ length: SLOT_COUNT }, (_, i) => `--series-${i + 1}`)
const OTHER_VAR = '--series-other'

export const SEQ_VARS = [
  '--seq-0',
  '--seq-1',
  '--seq-2',
  '--seq-3',
  '--seq-4',
  '--seq-5',
  '--seq-6',
  '--seq-7',
]

function readVar(name: string): string {
  if (typeof window === 'undefined') return '#888888'
  const value = getComputedStyle(document.documentElement).getPropertyValue(name)
  return value.trim() || '#888888'
}

/** Resolved hexes for the current theme. Recomputed when the theme changes. */
export interface Palette {
  slots: string[]
  other: string
  seq: string[]
  grid: string
  axis: string
  text: string
  textMuted: string
  surface: string
}

export function readPalette(): Palette {
  return {
    slots: SLOT_VARS.map(readVar),
    other: readVar(OTHER_VAR),
    seq: SEQ_VARS.map(readVar),
    grid: readVar('--grid'),
    axis: readVar('--axis'),
    text: readVar('--text-primary'),
    textMuted: readVar('--text-muted'),
    surface: readVar('--surface-1'),
  }
}

/**
 * Key -> slot map built from the global ordering.
 * Keys past the last slot share the muted "other" colour, matching the server's
 * own folding of the long tail into an `other` group.
 *
 * `drawn` is the set of keys a chart actually plots, and it matters: the global
 * ordering is all-time, so a model that is fourth this week can be thirtieth
 * overall and would draw in the tail grey -- next to three other models in the
 * same grey, and next to "Other" in the same grey again. Passing the drawn keys
 * allocates the slots among them, in global order, so the ten lines on a chart
 * get the ten hues. The cost is that changing a filter can repaint a series;
 * without it, the alternative was four series no one can tell apart.
 */
export class ColorScale {
  private index = new Map<string, number>()

  constructor(orderedKeys: string[], private palette: Palette, drawn?: Iterable<string>) {
    const only = drawn == null ? null : new Set(drawn)
    let slot = 0
    orderedKeys.forEach((key) => {
      if (this.index.has(key)) return
      if (only && !only.has(key)) return
      this.index.set(key, slot++)
    })
    // Anything drawn but absent from the global ordering still needs a hue: the
    // orderings are fetched separately, so a brand new model can reach a chart
    // one poll before it reaches the dimension list.
    if (only) {
      for (const key of only) if (!this.index.has(key)) this.index.set(key, slot++)
    }
  }

  get(key: string): string {
    if (key === 'other' || key === 'Other') return this.palette.other
    const i = this.index.get(key)
    if (i == null || i >= SLOT_COUNT) return this.palette.other
    return this.palette.slots[i]
  }

  /** True when the key has its own hue rather than the shared "other" grey. */
  hasOwnHue(key: string): boolean {
    const i = this.index.get(key)
    return i != null && i < SLOT_COUNT
  }
}

/** Sequential ramp lookup for a 0..1 magnitude. */
export function seqColor(palette: Palette, t: number): string {
  if (!isFinite(t)) return palette.seq[0]
  const clamped = Math.max(0, Math.min(1, t))
  const i = Math.round(clamped * (palette.seq.length - 1))
  return palette.seq[i]
}

/** Fill for an area under a line: the series hue, heavily diluted. */
export function fill(hex: string, alpha: number): string {
  return `color-mix(in srgb, ${hex} ${Math.round(alpha * 100)}%, transparent)`
}
