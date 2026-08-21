import { describe, expect, it } from 'vitest'

import type { ContextBreakdown, UsageStats } from '@/types/hermes'

import { type ContextUsageSnapshot, resolveContextGaugeUsage, stampContextTurnEpoch } from './context-usage-source'

const baseUsage: UsageStats = {
  calls: 3,
  context_max: 999_999,
  context_percent: 99,
  context_used: 888_888,
  input: 500,
  output: 50,
  total: 550
}

const breakdown: ContextBreakdown = {
  categories: [],
  context_max: 272_000,
  context_percent: 40,
  context_used: 108_800,
  estimated_total: 108_800,
  model: 'test-model'
}

const live: ContextUsageSnapshot = {
  context_max: 272_000,
  context_percent: 55,
  context_used: 149_600
}

function resolve(overrides: Partial<Parameters<typeof resolveContextGaugeUsage>[0]> = {}): UsageStats {
  return resolveContextGaugeUsage({
    baseUsage,
    busy: false,
    contextBreakdown: null,
    contextBreakdownEpoch: null,
    contextTurnEpoch: 1,
    contextUsage: null,
    contextUsageEpoch: null,
    ...overrides
  })
}

describe('stampContextTurnEpoch', () => {
  it('increments exactly once on an idle-to-busy transition', () => {
    const idle = { busy: false, contextTurnEpoch: 7 }
    const started = stampContextTurnEpoch(idle, { ...idle, busy: true })

    expect(started.contextTurnEpoch).toBe(8)
    expect(stampContextTurnEpoch(started, { ...started, busy: true }).contextTurnEpoch).toBe(8)
  })

  it('does not advance the generation when a turn settles', () => {
    const live = { busy: true, contextTurnEpoch: 3 }

    expect(stampContextTurnEpoch(live, { ...live, busy: false }).contextTurnEpoch).toBe(3)
  })
})

describe('resolveContextGaugeUsage', () => {
  it('never leaks merged global context fields when the focused session has no scoped source', () => {
    expect(resolve()).toEqual({ calls: 3, input: 500, output: 50, total: 550 })
  })

  it('keeps the last trusted breakdown when a turn starts before live occupancy arrives', () => {
    expect(
      resolve({
        busy: true,
        contextBreakdown: breakdown,
        contextBreakdownEpoch: 0,
        contextTurnEpoch: 1
      })
    ).toMatchObject({
      context_max: 272_000,
      context_percent: 40,
      context_used: 108_800
    })
  })

  it('switches to live usage only when it belongs to the current turn epoch', () => {
    expect(
      resolve({
        busy: true,
        contextBreakdown: breakdown,
        contextBreakdownEpoch: 0,
        contextTurnEpoch: 1,
        contextUsage: live,
        contextUsageEpoch: 1
      })
    ).toMatchObject({
      context_max: 272_000,
      context_percent: 55,
      context_used: 149_600
    })
  })

  it('keeps final live occupancy after settle while the authoritative refetch is still stale', () => {
    expect(
      resolve({
        busy: false,
        contextBreakdown: breakdown,
        contextBreakdownEpoch: 0,
        contextTurnEpoch: 1,
        contextUsage: live,
        contextUsageEpoch: 1
      })
    ).toMatchObject({
      context_percent: 55,
      context_used: 149_600
    })
  })

  it('hands authority back to a breakdown fetched for the completed turn', () => {
    expect(
      resolve({
        busy: false,
        contextBreakdown: breakdown,
        contextBreakdownEpoch: 1,
        contextTurnEpoch: 1,
        contextUsage: live,
        contextUsageEpoch: 1
      })
    ).toMatchObject({
      context_percent: 40,
      context_used: 108_800
    })
  })

  it('uses the newest session-scoped fallback on the next turn, not an older breakdown', () => {
    expect(
      resolve({
        busy: true,
        contextBreakdown: breakdown,
        contextBreakdownEpoch: 0,
        contextTurnEpoch: 2,
        contextUsage: live,
        contextUsageEpoch: 1
      })
    ).toMatchObject({
      context_percent: 55,
      context_used: 149_600
    })
  })
})
