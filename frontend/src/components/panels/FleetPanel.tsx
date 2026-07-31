import { useSimulationStore } from '../../store/simulationStore'
import type { VehicleTelemetry } from '../../types/telemetry'
import type { Order, RouteStop, Vehicle } from '../../types/domain'

const STATUS_LABELS: Record<string, string> = {
  driving: 'Driving',
  waiting: 'Waiting',
  serving: 'Serving customer',
  on_break: 'EU rest break',
  idle_at_depot: 'Idle at depot',
  finished: 'Finished',
}

const STATUS_DOT_COLORS: Record<string, string> = {
  driving: 'bg-accent',
  serving: 'bg-success',
  waiting: 'bg-warning',
  on_break: 'bg-warning',
  idle_at_depot: 'bg-text-muted',
  finished: 'bg-text-muted',
}

/** Number of delivery/pickup stops (excluding depot start/end) this vehicle's plan includes. */
function countCustomerStops(routeStops: RouteStop[] | null | undefined, vehicleId: number): number {
  return (routeStops ?? []).filter(
    (stop) =>
      stop?.vehicle_id === vehicleId &&
      (stop.stop_type === 'CUSTOMER_DELIVERY' || stop.stop_type === 'DEPOT_PICKUP'),
  ).length
}

function findActiveCustomerName(
  orders: Order[] | null | undefined,
  activeCustomerId: string | null,
): string | null {
  if (activeCustomerId === null) {
    return null
  }
  return (
    (orders ?? []).find((order) => String(order.id) === activeCustomerId)?.customer_name ??
    `Customer ${activeCustomerId}`
  )
}

interface FleetCardProps {
  vehicle: Vehicle
  telemetry: VehicleTelemetry | undefined
  totalCustomerStops: number
  activeCustomerName: string | null
  isCompletedPlan: boolean
}

function FleetCard({ vehicle, telemetry, totalCustomerStops, activeCustomerName, isCompletedPlan }: FleetCardProps) {
  const status = isCompletedPlan ? 'finished' : (telemetry?.status ?? 'idle_at_depot')
  const completedStops = isCompletedPlan ? totalCustomerStops : (telemetry?.locked_prefix_length ?? 0)
  const progressPercent =
    totalCustomerStops > 0
      ? Math.min(100, Math.round((completedStops / totalCustomerStops) * 100))
      : isCompletedPlan
        ? 100
        : 0

  return (
    <div className="rounded-lg border border-border bg-surface-alt p-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-text-heading">{vehicle.license_plate}</span>
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-text-muted">
          <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT_COLORS[status] ?? 'bg-text-muted'}`} />
          {STATUS_LABELS[status] ?? status}
        </span>
      </div>

      <p className="mt-1 text-xs text-text-muted">
        {isCompletedPlan
          ? 'Workday completed'
          : activeCustomerName
            ? `Heading to / serving ${activeCustomerName}`
            : 'No active customer'}
      </p>

      <div className="mt-2.5">
        <div className="mb-1 flex items-center justify-between text-[11px] text-text-muted">
          <span>Stops completed</span>
          <span>
            {completedStops} / {totalCustomerStops} ({progressPercent}%)
          </span>
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface">
          <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${progressPercent}%` }} />
        </div>
      </div>
    </div>
  )
}

/** Live per-vehicle status cards: operational state, current customer, and route completion progress. */
export function FleetPanel() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const clock = useSimulationStore((state) => state.clock)
  const vehiclesById = useSimulationStore((state) => state.vehiclesById) ?? {}
  const isCompletedPlan = activePlan?.status === 'COMPLETED'
  const vehicles = activePlan?.vehicles ?? []
  const routeStops = activePlan?.route_stops ?? []
  const orders = activePlan?.orders ?? []

  return (
    <section className="panel">
      <div className="flex items-center justify-between">
        <h2 className="panel-title">Fleet Telemetry</h2>
        {clock != null && !isCompletedPlan && (
          <span className="font-mono text-xs text-text-muted">
            min {clock.current_minute ?? 0}
            {clock.is_finished ? ' · finished' : ''}
          </span>
        )}
        {isCompletedPlan && <span className="font-mono text-xs text-text-muted">100% complete</span>}
      </div>

      {activePlan === null && <p className="text-sm text-text-muted">Select a workday plan to see fleet telemetry.</p>}

      {activePlan !== null && vehicles.length === 0 && (
        <p className="text-sm text-text-muted">No active vehicles assigned to this plan.</p>
      )}

      {activePlan !== null && clock === null && activePlan.status === 'DRAFT' && (
        <p className="text-sm text-text-muted">This plan is still a draft; optimize it to begin live telemetry.</p>
      )}

      {activePlan !== null && clock === null && activePlan.status === 'ACTIVE' && (
        <p className="text-sm text-text-muted">Connecting to the live simulation…</p>
      )}

      <div className="flex flex-col gap-2">
        {vehicles.map((vehicle) => (
          <FleetCard
            key={vehicle.id}
            vehicle={vehicle}
            telemetry={vehiclesById[String(vehicle.id)]}
            totalCustomerStops={countCustomerStops(routeStops, vehicle.id)}
            activeCustomerName={findActiveCustomerName(
              orders,
              vehiclesById[String(vehicle.id)]?.active_customer_id ?? null,
            )}
            isCompletedPlan={isCompletedPlan}
          />
        ))}
      </div>
    </section>
  )
}
