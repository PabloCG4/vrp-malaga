/**
 * TypeScript mirrors of the Pydantic v2 read schemas exposed by the Control
 * Tower REST API (`backend/src/api/schemas/`).
 *
 * Field names and optionality intentionally match the backend's JSON output
 * verbatim (including `snake_case`), rather than being adapted to a
 * JavaScript naming convention, so that the wire shape and the TypeScript
 * shape can always be compared side by side without a translation layer.
 * Dates/timestamps are typed as `string` (ISO-8601, as serialized by
 * Pydantic), not `Date`, since no consumer in this block parses them yet.
 */

import type { RouteStopType, SimulationEventType, WorkdayStatus } from './enums'

/** Mirrors `api/schemas/driver.DriverRead`. */
export interface Driver {
  id: number
  full_name: string
  license_number: string
  max_continuous_driving_seconds: number
  is_active: boolean
  created_at: string
}

/** Mirrors `api/schemas/vehicle.VehicleRead`. */
export interface Vehicle {
  id: number
  license_plate: string
  capacity_kg: number
  default_driver_id: number | null
  is_active: boolean
  created_at: string
}

/** Mirrors `api/schemas/order.OrderRead`. */
export interface Order {
  id: number
  workday_plan_id: number
  customer_name: string
  node_id: number
  latitude: number
  longitude: number
  demand_kg: number
  service_time_seconds: number
  time_window_start_seconds: number
  time_window_end_seconds: number
  is_urgent: boolean
  is_pickup_stop: boolean
  paired_order_id: number | null
  created_at: string
}

/** Mirrors `api/schemas/route_stop.RouteStopRead`. */
export interface RouteStop {
  id: number
  workday_plan_id: number
  vehicle_id: number
  order_id: number | null
  sequence_order: number
  stop_type: RouteStopType
  node_id: number
  planned_arrival_seconds: number
  actual_arrival_seconds: number | null
  departure_seconds: number | null
}

/** Mirrors `api/schemas/simulation_event.SimulationEventRead`. */
export interface SimulationEventRecord {
  id: number
  workday_plan_id: number
  event_type: SimulationEventType
  trigger_minute: number
  payload_json: Record<string, unknown>
  created_at: string
}

/** Mirrors `api/schemas/workday_plan.WorkdayPlanRead` (list/summary view, no nested collections). */
export interface WorkdayPlanSummary {
  id: number
  workday_date: string
  status: WorkdayStatus
  total_cost: number
  total_distance_km: number
  execution_time_ms: number
  created_at: string
  updated_at: string
}

/** Mirrors `api/schemas/workday_plan.WorkdayPlanDetailRead` (detail view, with orders/fleet/route stops/events). */
export interface WorkdayPlanDetail extends WorkdayPlanSummary {
  orders: Order[]
  route_stops: RouteStop[]
  vehicles: Vehicle[]
  /** May be omitted by older API responses; the store normalizes missing values to `[]`. */
  simulation_events?: SimulationEventRecord[]
}

/** Mirrors `api/schemas/optimization.WorkdayOptimizationResult`. */
export interface WorkdayOptimizationResult {
  workday_plan: WorkdayPlanDetail
  route_stop_count: number
  iterations_completed: number
  elapsed_seconds: number
  is_feasible: boolean
}

/** Mirrors `api/schemas/live_simulation.EligibleUrgentOrderNode`. */
export interface EligibleUrgentOrderNode {
  node_id: number
  latitude: number
  longitude: number
}

/** Mirrors `api/schemas/live_simulation.EventInjectionAck`. */
export interface EventInjectionAck {
  workday_plan_id: number
  event_type: SimulationEventType
  trigger_minute: number
  order_id: string | null
  message: string
}

/** Mirrors `api/schemas/live_simulation.TrafficIncidentInjectionRequest` (POST body). */
export interface TrafficIncidentInjectionRequest {
  first_node: number
  second_node: number
  reopen_after_minutes?: number | null
  description?: string
}

/** Mirrors `api/schemas/live_simulation.UrgentOrderInjectionRequest` (POST body). */
export interface UrgentOrderInjectionRequest {
  delivery_node: number
  demand: number
  order_id?: string | null
  pickup_service_time_seconds?: number
  delivery_service_time_seconds?: number
  deadline_minutes_after_trigger?: number
  description?: string
}
