import { useEffect, useState } from 'react'

import type { ContextBreakdown } from '@/types/hermes'

interface ContextBreakdownOptions {
  busy: boolean
  enabled: boolean
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  sessionId: null | string
  turnEpoch: number
}

interface CachedBreakdown {
  breakdown: ContextBreakdown
  turnEpoch: number
}

const MAX_CACHED_BREAKDOWNS = 8

/** The focused session's context breakdown, fetched as soon as the statusbar
 *  gauge is on screen rather than when its popover opens.
 *
 *  The backend only reports measured context occupancy (`last_prompt_tokens`)
 *  once a turn has run in THIS process, so a resumed session reports none —
 *  which is why turning the gauge on used to do nothing at all until you sent
 *  a message. `session.context_breakdown` estimates the same figure from the
 *  live system prompt + tools + transcript, so it answers for a session that
 *  hasn't spoken yet. It is a read-only chars/4 pass: no provider call, no
 *  prompt-cache impact.
 *
 *  Refetches when the focused session changes and when a turn ends (the
 *  transcript just grew). The small per-session cache is intentionally retained
 *  during a turn: it is the last trustworthy fallback until a session-scoped
 *  live occupancy frame for the new turn arrives. Each entry carries the turn
 *  epoch it describes, so the statusbar resolver can replace it exactly when a
 *  newer source becomes authoritative. */
export function useContextBreakdown({ busy, enabled, requestGateway, sessionId, turnEpoch }: ContextBreakdownOptions) {
  const [fetchedBySession, setFetchedBySession] = useState<Map<string, CachedBreakdown>>(() => new Map())
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    // Mid-turn the transcript changes on every delta and the gateway already
    // streams measured usage, so a new estimate would be both stale and
    // wasteful. Keep the cached pre-turn snapshot as fallback instead.
    if (!enabled || !sessionId || busy) {
      setLoading(false)

      return
    }

    let cancelled = false
    setLoading(true)

    void requestGateway<ContextBreakdown>('session.context_breakdown', { session_id: sessionId })
      .then(breakdown => {
        if (!cancelled && breakdown) {
          setFetchedBySession(current => {
            const next = new Map(current)
            next.delete(sessionId)
            next.set(sessionId, { breakdown, turnEpoch })

            while (next.size > MAX_CACHED_BREAKDOWNS) {
              const oldest = next.keys().next().value

              if (!oldest) {
                break
              }

              next.delete(oldest)
            }

            return next
          })
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [busy, enabled, requestGateway, sessionId, turnEpoch])

  const fetched = sessionId ? (fetchedBySession.get(sessionId) ?? null) : null

  return {
    breakdown: fetched?.breakdown ?? null,
    breakdownEpoch: fetched?.turnEpoch ?? null,
    loading
  }
}
