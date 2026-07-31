import { useMemo } from 'react'
import { Marker, Popup } from 'react-leaflet'
import { createCustomerIcon } from './mapIcons'
import { formatWorkdaySeconds } from '../../utils/time'
import type { Order, RouteStop } from '../../types/domain'

const STANDARD_ICON = createCustomerIcon(false)
const URGENT_ICON = createCustomerIcon(true)

interface OrderMarkerProps {
  order: Order
  routeStop: RouteStop | undefined
}

function OrderMarker({ order, routeStop }: OrderMarkerProps) {
  return (
    <Marker position={[order.latitude, order.longitude]} icon={order.is_urgent ? URGENT_ICON : STANDARD_ICON}>
      <Popup>
        <div className="min-w-[12rem] text-sm">
          <p className="font-semibold text-text-heading">{order.customer_name}</p>
          {order.is_urgent && (
            <span className="badge mt-1 bg-danger/15 text-danger">Urgent VRPPD delivery</span>
          )}
          <dl className="mt-2 grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
            <dt className="text-text-muted">Node</dt>
            <dd className="font-mono text-text-heading">{order.node_id}</dd>
            <dt className="text-text-muted">Demand</dt>
            <dd className="text-text-heading">{order.demand_kg.toFixed(1)} kg</dd>
            <dt className="text-text-muted">Time window</dt>
            <dd className="text-text-heading">
              {formatWorkdaySeconds(order.time_window_start_seconds)}–{formatWorkdaySeconds(order.time_window_end_seconds)}
            </dd>
            {routeStop && (
              <>
                <dt className="text-text-muted">Planned arrival</dt>
                <dd className="text-text-heading">{formatWorkdaySeconds(routeStop.planned_arrival_seconds)}</dd>
                <dt className="text-text-muted">Actual arrival</dt>
                <dd className={routeStop.actual_arrival_seconds !== null ? 'font-semibold text-success' : 'text-text-muted'}>
                  {routeStop.actual_arrival_seconds !== null ? formatWorkdaySeconds(routeStop.actual_arrival_seconds) : 'Pending'}
                </dd>
              </>
            )}
          </dl>
        </div>
      </Popup>
    </Marker>
  )
}

interface OrderMarkersProps {
  orders: Order[]
  routeStops: RouteStop[]
}

/** Renders every customer delivery (standard and urgent) as a distinct, clickable marker. */
export function OrderMarkers({ orders, routeStops }: OrderMarkersProps) {
  const deliveryOrders = useMemo(() => (orders ?? []).filter((order) => !order.is_pickup_stop), [orders])
  const routeStopByOrderId = useMemo(() => {
    const map = new Map<number, RouteStop>()
    for (const stop of routeStops ?? []) {
      if (stop?.order_id !== null && stop?.order_id !== undefined) {
        map.set(stop.order_id, stop)
      }
    }
    return map
  }, [routeStops])

  return (
    <>
      {deliveryOrders.map((order) => (
        <OrderMarker key={order.id} order={order} routeStop={routeStopByOrderId.get(order.id)} />
      ))}
    </>
  )
}
