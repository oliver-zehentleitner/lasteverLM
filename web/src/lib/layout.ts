/** The share of the right column the viewer takes above the chat. */
export const DEFAULT_VIEWER_RATIO = 0.5
export const MIN_VIEWER_RATIO = 0.2
export const MAX_VIEWER_RATIO = 0.85
const KEY = 'lasteverlm.viewerRatio'

export function clampRatio(r: number): number {
  if (!Number.isFinite(r)) return DEFAULT_VIEWER_RATIO
  return Math.min(MAX_VIEWER_RATIO, Math.max(MIN_VIEWER_RATIO, r))
}

/** Pointer position inside the column, as a ratio. */
export function ratioFromPointer(clientY: number, top: number, height: number): number {
  return height > 0 ? clampRatio((clientY - top) / height) : DEFAULT_VIEWER_RATIO
}

/** The stored ratio of this browser. Storage can be missing or throw
 *  (private window, blocked site data); the default is used then. */
export function readRatio(storage: Pick<Storage, 'getItem'> | undefined): number {
  try {
    const v = storage?.getItem(KEY)
    return v === null || v === undefined ? DEFAULT_VIEWER_RATIO : clampRatio(Number(v))
  } catch {
    return DEFAULT_VIEWER_RATIO
  }
}

export function writeRatio(storage: Pick<Storage, 'setItem' | 'removeItem'> | undefined, r: number | null): void {
  try {
    if (r === null) storage?.removeItem(KEY)
    else storage?.setItem(KEY, String(clampRatio(r)))
  } catch {
    // not remembered; the layout still works
  }
}

/** `window.localStorage` itself can throw when site data is blocked. */
export function browserStorage(): Storage | undefined {
  try {
    return window.localStorage
  } catch {
    return undefined
  }
}

/** Quick sizes for the chat, as the share of the right column it gets;
 *  each is just a viewer ratio, so presets, the divider and the stored
 *  ratio stay one mechanism. */
export const CHAT_PRESETS = [
  { label: '1x', chat: 0.15 },
  { label: '2x', chat: 0.3 },
  { label: '3x', chat: 0.45 },
] as const

export function presetRatio(chat: number): number {
  return clampRatio(1 - chat)
}

/** The preset the ratio matches (within half a percent), for highlighting;
 *  a drag moves off it. */
export function activePreset(ratio: number): (typeof CHAT_PRESETS)[number]['label'] | null {
  return CHAT_PRESETS.find((p) => Math.abs(presetRatio(p.chat) - ratio) < 0.005)?.label ?? null
}

/** Column widths in pixels between the notebook list, the left and the
 *  right column; null means the stylesheet's default. */
export const SIDEBAR_WIDTH = { key: 'lasteverlm.sidebarWidth', min: 160, max: 420 }
// the minimum is the 3 x 2 output-format grid: 27rem at the 14px root size
export const LEFT_WIDTH = { key: 'lasteverlm.leftWidth', min: 27 * 14, max: 720 }
/** what the right column keeps at least while the left one is dragged */
export const MIN_RIGHT_WIDTH = 360

export function clampWidth(px: number, min: number, max: number): number {
  return Math.round(Math.min(max, Math.max(min, px)))
}

export function readWidth(storage: Pick<Storage, 'getItem'> | undefined, key: string, min: number, max: number): number | null {
  try {
    const v = storage?.getItem(key)
    if (v === null || v === undefined) return null
    const n = Number(v)
    return Number.isFinite(n) ? clampWidth(n, min, max) : null
  } catch {
    return null
  }
}

export function writeWidth(storage: Pick<Storage, 'setItem' | 'removeItem'> | undefined, key: string, px: number | null): void {
  try {
    if (px === null) storage?.removeItem(key)
    else storage?.setItem(key, String(Math.round(px)))
  } catch {
    // not remembered; the layout still works
  }
}

/** The width below which the columns stack (the 900px breakpoint in
 *  styles.css): the chat is then below the fold, so it neither takes the
 *  focus on its own nor scrolls the page to itself. */
export const STACKED_QUERY = '(max-width: 900px)'

export function isStacked(): boolean {
  return matches(STACKED_QUERY)
}

/** The phone width (the 640px breakpoint in styles.css): the header's
 *  links leave the top bar for the profile menu, so the bar stays one line. */
export const PHONE_QUERY = '(max-width: 640px)'

export function isPhone(): boolean {
  return matches(PHONE_QUERY)
}

function matches(query: string): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(query).matches
}
