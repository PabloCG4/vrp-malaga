/**
 * REST client for the static workday plan endpoints (`routers/workdays.py`).
 */

import { apiRequest } from './httpClient'
import type { WorkdayOptimizationResult, WorkdayPlanDetail, WorkdayPlanSummary } from '../types/domain'

/** `GET /api/v1/workdays` - every workday plan, most recently scheduled first. */
export function listWorkdays(signal?: AbortSignal): Promise<WorkdayPlanSummary[]> {
  return apiRequest<WorkdayPlanSummary[]>('/api/v1/workdays', { ...(signal !== undefined && { signal }) })
}

/** `GET /api/v1/workdays/{id}` - one plan with its orders, active fleet, and planned route stops. */
export function getWorkday(workdayId: number, signal?: AbortSignal): Promise<WorkdayPlanDetail> {
  return apiRequest<WorkdayPlanDetail>(`/api/v1/workdays/${workdayId}`, { ...(signal !== undefined && { signal }) })
}

/**
 * `POST /api/v1/workdays/{id}/optimize` - trigger 1-click static dispatch.
 *
 * Only valid for a `DRAFT` plan; the backend transitions it to `ACTIVE` and
 * persists the resulting route stops. Rejects with `ApiError` (404/409/422)
 * on an unknown, already-active/completed, or unservable plan.
 */
export function optimizeWorkday(workdayId: number): Promise<WorkdayOptimizationResult> {
  return apiRequest<WorkdayOptimizationResult>(`/api/v1/workdays/${workdayId}/optimize`, { method: 'POST' })
}
