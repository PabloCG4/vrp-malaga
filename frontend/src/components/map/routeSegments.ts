/**
 * Derives the polyline segments of one vehicle's route, and which of them
 * the vehicle has already committed to (dimmed) versus not yet driven
 * (vibrant/opaque).
 *
 * Prefers multi-point street coordinates from `GET .../route-geometry`
 * (CostMatrix.path_between). Falls back to Euclidean stop-to-stop segments
 * when geometry has not loaded yet, so the map never goes blank.
 *
 * "Already traversed" uses `VehicleTelemetry.locked_prefix_length` (or a
 * forced full lock for COMPLETED plans): the leg arriving at the Nth
 * customer-facing stop is considered traversed once the vehicle has locked
 * in at least N stops.
 */

import type { RouteStop } from '../../types/domain'
import type { RouteLegGeometry } from '../../types/geometry'
import type { NetworkNode } from '../../types/network'

export type LatLngTuple = [number, number]

export interface RouteSegment {
  key: string
  positions: LatLngTuple[]
  isTraversed: boolean
}

function customerOrdinalAtSequence(orderedStops: RouteStop[], sequenceOrder: number): number {
  let customerOrdinal = 0
  for (const stop of orderedStops) {
    if (stop.stop_type === 'CUSTOMER_DELIVERY' || stop.stop_type === 'DEPOT_PICKUP') {
      customerOrdinal += 1
    }
    if (stop.sequence_order === sequenceOrder) {
      return customerOrdinal
    }
  }
  return customerOrdinal
}

/** Build street-following segments from backend geometry legs when available. */
export function buildVehicleRouteSegmentsFromGeometry(
  vehicleId: number,
  routeStops: RouteStop[] | null | undefined,
  legs: RouteLegGeometry[] | null | undefined,
  lockedPrefixLength: number,
): RouteSegment[] {
  const orderedStops = (routeStops ?? [])
    .filter((stop) => stop?.vehicle_id === vehicleId)
    .sort((a, b) => (a.sequence_order ?? 0) - (b.sequence_order ?? 0))

  const vehicleLegs = (legs ?? [])
    .filter((leg) => leg?.vehicle_id === vehicleId)
    .sort((a, b) => (a.from_sequence_order ?? 0) - (b.from_sequence_order ?? 0))

  return vehicleLegs
    .filter((leg) => Array.isArray(leg.coordinates) && leg.coordinates.length >= 2)
    .map((leg) => ({
      key: `${vehicleId}-${leg.from_sequence_order}-${leg.to_sequence_order}`,
      positions: leg.coordinates
        .filter((pair): pair is [number, number] => Array.isArray(pair) && pair.length >= 2)
        .map(([lat, lon]) => [lat, lon] as LatLngTuple),
      isTraversed: customerOrdinalAtSequence(orderedStops, leg.to_sequence_order ?? 0) <= lockedPrefixLength,
    }))
    .filter((segment) => segment.positions.length >= 2)
}

/** Fallback: straight Euclidean segments between consecutive stop coordinates. */
export function buildVehicleRouteSegmentsFallback(
  vehicleId: number,
  routeStops: RouteStop[] | null | undefined,
  getNode: (nodeId: number) => NetworkNode | undefined,
  lockedPrefixLength: number,
): RouteSegment[] {
  const orderedStops = (routeStops ?? [])
    .filter((stop) => stop?.vehicle_id === vehicleId)
    .sort((a, b) => (a.sequence_order ?? 0) - (b.sequence_order ?? 0))

  let customerOrdinal = 0
  const points: { node: NetworkNode; ordinal: number }[] = []
  for (const stop of orderedStops) {
    const node = getNode(stop.node_id)
    if (node === undefined) {
      continue
    }
    if (stop.stop_type === 'CUSTOMER_DELIVERY' || stop.stop_type === 'DEPOT_PICKUP') {
      customerOrdinal += 1
    }
    points.push({ node, ordinal: customerOrdinal })
  }

  const segments: RouteSegment[] = []
  for (let index = 0; index < points.length - 1; index += 1) {
    const from = points[index]
    const to = points[index + 1]
    if (from === undefined || to === undefined) {
      continue
    }
    segments.push({
      key: `${vehicleId}-${index}`,
      positions: [
        [from.node.latitude, from.node.longitude],
        [to.node.latitude, to.node.longitude],
      ],
      isTraversed: to.ordinal <= lockedPrefixLength,
    })
  }
  return segments
}

/** Prefer geometry legs; fall back to Euclidean stop-to-stop segments. */
export function buildVehicleRouteSegments(
  vehicleId: number,
  routeStops: RouteStop[] | null | undefined,
  getNode: (nodeId: number) => NetworkNode | undefined,
  lockedPrefixLength: number,
  geometryLegs: RouteLegGeometry[] | null | undefined,
): RouteSegment[] {
  const safeLegs = geometryLegs ?? null
  if (safeLegs !== null && safeLegs.some((leg) => leg?.vehicle_id === vehicleId)) {
    return buildVehicleRouteSegmentsFromGeometry(vehicleId, routeStops, safeLegs, lockedPrefixLength)
  }
  return buildVehicleRouteSegmentsFallback(vehicleId, routeStops, getNode, lockedPrefixLength)
}
