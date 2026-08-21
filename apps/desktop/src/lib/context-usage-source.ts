import type { ContextBreakdown, UsageStats } from '@/types/hermes'

export type ContextUsageSnapshot = Pick<UsageStats, 'context_max' | 'context_percent' | 'context_used'>

interface ContextTurnState {
  busy: boolean
  contextTurnEpoch?: number
}

/** Stamp exactly one new generation on the authoritative idle→busy edge.
 * Repeated live events for the same turn leave the generation unchanged. */
export function stampContextTurnEpoch<T extends ContextTurnState>(previous: T, updated: T): T {
  return !previous.busy && updated.busy
    ? { ...updated, contextTurnEpoch: (previous.contextTurnEpoch ?? 0) + 1 }
    : updated
}

const finiteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

/** Keep only an actual occupancy measurement. context_max by itself is a
 * denominator, not proof that this usage frame measured the current prompt. */
export function contextUsageSnapshot(usage: Partial<UsageStats> | null | undefined): ContextUsageSnapshot | null {
  if (!usage || (!finiteNumber(usage.context_used) && !finiteNumber(usage.context_percent))) {
    return null
  }

  return {
    ...(finiteNumber(usage.context_max) ? { context_max: usage.context_max } : {}),
    ...(finiteNumber(usage.context_percent) ? { context_percent: usage.context_percent } : {}),
    ...(finiteNumber(usage.context_used) ? { context_used: usage.context_used } : {})
  }
}

function stripContextFields(usage: UsageStats): UsageStats {
  const base = { ...usage }
  delete base.context_max
  delete base.context_percent
  delete base.context_used

  return base
}

function overlayContext(base: UsageStats, source: Partial<UsageStats> | null): UsageStats {
  const snapshot = contextUsageSnapshot(source)

  return snapshot ? { ...base, ...snapshot } : base
}

interface ContextGaugeUsageOptions {
  baseUsage: UsageStats
  busy: boolean
  contextBreakdown: ContextBreakdown | null
  contextBreakdownEpoch: null | number
  contextTurnEpoch: number
  contextUsage: ContextUsageSnapshot | null
  contextUsageEpoch: null | number
}

function newestFallback({
  contextBreakdown,
  contextBreakdownEpoch,
  contextUsage,
  contextUsageEpoch
}: Pick<
  ContextGaugeUsageOptions,
  'contextBreakdown' | 'contextBreakdownEpoch' | 'contextUsage' | 'contextUsageEpoch'
>): Partial<UsageStats> | null {
  if (!contextBreakdown) {
    return contextUsage
  }

  if (!contextUsage) {
    return contextBreakdown
  }

  const breakdownEpoch = contextBreakdownEpoch ?? -1
  const usageEpoch = contextUsageEpoch ?? -1

  return usageEpoch > breakdownEpoch ? contextUsage : contextBreakdown
}

/**
 * Resolve the context gauge from provenance, not merely from `busy`.
 *
 * - Global usage contributes cumulative token totals only; its context fields
 *   are stripped because that atom merges and can retain another session's
 *   occupancy after a switch.
 * - During a turn, a session-scoped usage frame wins only when its turn epoch
 *   matches the session's current epoch.
 * - Until that happens, retain the newest trustworthy session-scoped fallback.
 * - After settle, keep the final live measurement until the authoritative
 *   context_breakdown refetch for that same epoch lands.
 */
export function resolveContextGaugeUsage(options: ContextGaugeUsageOptions): UsageStats {
  const {
    baseUsage,
    busy,
    contextBreakdown,
    contextBreakdownEpoch,
    contextTurnEpoch,
    contextUsage,
    contextUsageEpoch
  } = options

  const base = stripContextFields(baseUsage)
  const currentLive = contextUsage && contextUsageEpoch === contextTurnEpoch ? contextUsage : null
  const currentBreakdown = contextBreakdown && contextBreakdownEpoch === contextTurnEpoch ? contextBreakdown : null

  if (busy) {
    return overlayContext(base, currentLive ?? newestFallback(options))
  }

  return overlayContext(base, currentBreakdown ?? currentLive ?? newestFallback(options))
}
