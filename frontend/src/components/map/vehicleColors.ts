/**
 * Fixed, high-contrast color palette assigning one distinct color per
 * vehicle (by its numeric database id), reused consistently for its live
 * marker and every polyline segment of its route.
 */
const VEHICLE_COLOR_PALETTE: readonly string[] = [
  '#4f8dfd', // accent blue
  '#34d399', // emerald
  '#f5b942', // amber
  '#c084fc', // violet
  '#fb7185', // rose
  '#22d3ee', // cyan
  '#a3e635', // lime
  '#fb923c', // orange
]

export function getVehicleColor(vehicleId: number): string {
  const index = ((vehicleId % VEHICLE_COLOR_PALETTE.length) + VEHICLE_COLOR_PALETTE.length) % VEHICLE_COLOR_PALETTE.length
  return VEHICLE_COLOR_PALETTE[index] as string
}
