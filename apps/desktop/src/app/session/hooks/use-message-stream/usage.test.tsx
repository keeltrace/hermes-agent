import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $currentUsage } from '@/store/session'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'session-1'
// $currentUsage mirrors the primary session; ClientSessionState.usage drives
// the same status bar when a secondary tile is focused.
const BASELINE = { calls: 2, input: 500, output: 40, total: 540 }

let stream: MessageStreamHarness
let sessionStates = new Map<string, ClientSessionState>()

function mountStream() {
  stream = renderMessageStream(SID, { states: sessionStates })
}

describe('useMessageStream status-bar usage scoping', () => {
  beforeEach(() => {
    sessionStates = new Map([[SID, { ...createClientSessionState(), usage: { ...BASELINE } }]])
    $currentUsage.set({ ...BASELINE })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('merges a live session.usage tick from the focused session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { context_percent: 42, input: 1200, total: 1280 } },
        session_id: SID,
        type: 'session.usage'
      })
    )

    // Merge, not replace: fields absent from the tick keep their prior values.
    expect($currentUsage.get()).toEqual({ ...BASELINE, context_percent: 42, input: 1200, total: 1280 })
    expect(sessionStates.get(SID)?.usage).toEqual({
      ...BASELINE,
      context_percent: 42,
      input: 1200,
      total: 1280
    })
    expect(sessionStates.get(SID)?.contextUsage).toEqual({ context_percent: 42 })
    expect(sessionStates.get(SID)?.contextUsageEpoch).toBe(0)
  })

  it('tags only context-bearing usage with the session current turn generation', () => {
    // The stream harness owns an intentionally minimal updateSessionState fake;
    // epoch advancement itself is tested at stampContextTurnEpoch. Seed the
    // already-live generation here and test this layer's responsibility:
    // attributing context measurements to that generation.
    sessionStates.set(SID, {
      ...createClientSessionState(),
      busy: true,
      contextTurnEpoch: 1,
      usage: { ...BASELINE }
    })
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          usage: {
            context_max: 272_000,
            context_percent: 42,
            context_used: 114_240
          }
        },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect(sessionStates.get(SID)?.contextUsage).toEqual({
      context_max: 272_000,
      context_percent: 42,
      context_used: 114_240
    })
    expect(sessionStates.get(SID)?.contextUsageEpoch).toBe(1)

    // Model the next generation at the session-cache boundary. The prior
    // occupancy remains available as fallback but is deliberately stale.
    sessionStates.set(SID, {
      ...sessionStates.get(SID)!,
      busy: true,
      contextTurnEpoch: 2
    })

    // A totals-only tick is not current prompt occupancy and must not bless the
    // prior turn's context snapshot as if it belonged to this turn.
    act(() =>
      stream.handleEvent({
        payload: { usage: { input: 2000, total: 2090 } },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect(sessionStates.get(SID)?.contextUsageEpoch).toBe(1)

    act(() =>
      stream.handleEvent({
        payload: { usage: { context_percent: 51, context_used: 138_720 } },
        session_id: SID,
        type: 'session.usage'
      })
    )

    expect(sessionStates.get(SID)?.contextUsageEpoch).toBe(2)
    expect(sessionStates.get(SID)?.contextUsage).toEqual({
      context_percent: 51,
      context_used: 138_720
    })
  })

  it('caches a background session.usage tick without overwriting the primary status bar', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { usage: { input: 9999, total: 9999 } },
        session_id: 'background-session',
        type: 'session.usage'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
    expect(sessionStates.get('background-session')?.usage).toEqual({
      calls: 0,
      input: 9999,
      output: 0,
      total: 9999
    })
  })

  it('applies message.complete usage from the focused session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: {
          text: 'done',
          usage: {
            calls: 3,
            context_max: 272_000,
            context_percent: 51,
            context_used: 138_720,
            input: 1500,
            output: 90,
            total: 1590
          }
        },
        session_id: SID,
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual({
      calls: 3,
      context_max: 272_000,
      context_percent: 51,
      context_used: 138_720,
      input: 1500,
      output: 90,
      total: 1590
    })
    expect(sessionStates.get(SID)?.contextUsage).toEqual({
      context_max: 272_000,
      context_percent: 51,
      context_used: 138_720
    })
  })

  it('ignores message.complete usage from a background session', () => {
    mountStream()

    act(() =>
      stream.handleEvent({
        payload: { text: 'done', usage: { calls: 9, input: 9999, output: 999, total: 9999 } },
        session_id: 'background-session',
        type: 'message.complete'
      })
    )

    expect($currentUsage.get()).toEqual(BASELINE)
  })
})
