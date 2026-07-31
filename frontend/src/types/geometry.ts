/**
 * TypeScript mirrors of `api/schemas/geometry.py`
 * (`GET /api/v1/workdays/{id}/route-geometry`).
 *
 * Coordinates are `[latitude, longitude]` pairs in Leaflet order, matching
 * the street-network polyline reconstructed by `CostMatrix.path_between`.
 */

export interface RouteLegGeometry {
  vehicle_id: number
  from_sequence_order: number
  to_sequence_order: number
  from_node_id: number
  to_node_id: number
  coordinates: [number, number][]
}

export interface WorkdayRouteGeometry {
  workday_plan_id: number
  legs: RouteLegGeometry[]
}
