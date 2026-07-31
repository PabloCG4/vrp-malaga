/**
 * String-literal mirrors of the backend's Python enumerations
 * (`backend/src/db/enums.py` and `backend/src/simulation/fleet_tracker.py`).
 *
 * Plain `enum` is intentionally avoided (the project's `erasableSyntaxOnly`
 * TypeScript setting forbids constructs that require runtime support) in
 * favor of string literal union types plus a `satisfies`-checked tuple of
 * every value, which is enough to both type-check exhaustively and drive a
 * `<select>` option list without any generated runtime code.
 */

/** Lifecycle status of a `WorkdayPlan`. */
export type WorkdayStatus = 'DRAFT' | 'ACTIVE' | 'COMPLETED'

export const WORKDAY_STATUSES = ['DRAFT', 'ACTIVE', 'COMPLETED'] as const satisfies readonly WorkdayStatus[]

/** Role a `RouteStop` plays within its vehicle's visiting sequence. */
export type RouteStopType = 'DEPOT_START' | 'DEPOT_PICKUP' | 'CUSTOMER_DELIVERY' | 'DEPOT_END'

/** Kind of dynamic disruption recorded by a `SimulationEvent`. */
export type SimulationEventType = 'TRAFFIC_INCIDENT' | 'URGENT_ORDER'

/** Operational status of a vehicle at a given simulated instant (`FleetTracker.VehicleStatus`). */
export type VehicleStatus = 'driving' | 'waiting' | 'serving' | 'on_break' | 'idle_at_depot' | 'finished'
