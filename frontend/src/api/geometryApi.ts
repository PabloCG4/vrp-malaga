/** REST client for street-following route geometry (`GET .../route-geometry`). */

import { apiRequest } from './httpClient'
import type { WorkdayRouteGeometry } from '../types/geometry'

/** `GET /api/v1/workdays/{id}/route-geometry` - multi-point street polylines per consecutive route-stop pair. */
export function getWorkdayRouteGeometry(workdayId: number, signal?: AbortSignal): Promise<WorkdayRouteGeometry> {
  return apiRequest<WorkdayRouteGeometry>(`/api/v1/workdays/${workdayId}/route-geometry`, {
    ...(signal !== undefined && { signal }),
  })
}
