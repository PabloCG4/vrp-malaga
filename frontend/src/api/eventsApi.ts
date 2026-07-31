/**
 * REST client for the live simulation event-injection endpoints (`routers/live_simulation.py`).
 *
 * These calls lazily start the workday's `LiveSimulationSession` on the
 * backend if it is not already running; a WebSocket connection is not a
 * prerequisite for injecting a disruption, only for observing its effect.
 */

import { apiRequest } from './httpClient'
import type {
  EligibleUrgentOrderNode,
  EventInjectionAck,
  TrafficIncidentInjectionRequest,
  UrgentOrderInjectionRequest,
} from '../types/domain'

/** `GET /api/v1/workdays/{id}/events/urgent-order-nodes` - nodes an urgent order may legally target. */
export function listEligibleUrgentOrderNodes(
  workdayId: number,
  signal?: AbortSignal,
): Promise<EligibleUrgentOrderNode[]> {
  return apiRequest<EligibleUrgentOrderNode[]>(`/api/v1/workdays/${workdayId}/events/urgent-order-nodes`, {
    ...(signal !== undefined && { signal }),
  })
}

/** `POST /api/v1/workdays/{id}/events/traffic` - inject a real-time street closure. */
export function injectTrafficIncident(
  workdayId: number,
  payload: TrafficIncidentInjectionRequest,
): Promise<EventInjectionAck> {
  return apiRequest<EventInjectionAck>(`/api/v1/workdays/${workdayId}/events/traffic`, {
    method: 'POST',
    body: payload,
  })
}

/** `POST /api/v1/workdays/{id}/events/urgent-order` - inject a same-day urgent VRPPD order pair. */
export function injectUrgentOrder(
  workdayId: number,
  payload: UrgentOrderInjectionRequest,
): Promise<EventInjectionAck> {
  return apiRequest<EventInjectionAck>(`/api/v1/workdays/${workdayId}/events/urgent-order`, {
    method: 'POST',
    body: payload,
  })
}
