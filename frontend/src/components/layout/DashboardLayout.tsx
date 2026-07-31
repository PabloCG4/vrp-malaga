import { Header } from './Header'
import { WorkdaySelectorPanel } from '../panels/WorkdaySelectorPanel'
import { ControlPanel } from '../panels/ControlPanel'
import { FleetPanel } from '../panels/FleetPanel'
import { EventLogPanel } from '../panels/EventLogPanel'
import { LeafletMap } from '../map/LeafletMap'
import { TrafficIncidentModal } from '../modals/TrafficIncidentModal'
import { UrgentOrderModal } from '../modals/UrgentOrderModal'

/**
 * Root dashboard shell: header, left-hand sidebar (workday selection and
 * dispatch controls), central interactive map, and right-hand fleet
 * telemetry/activity timeline. Every panel is a self-contained view over
 * `useSimulationStore` (plus, from this block on, `networkStore`/`uiStore`
 * for map-only concerns); this component only arranges them.
 */
export function DashboardLayout() {
  return (
    <div className="flex h-screen flex-col bg-surface">
      <Header />
      <div className="grid min-h-0 flex-1 grid-cols-[20rem_1fr_22rem] gap-4 p-4">
        <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <WorkdaySelectorPanel />
          <ControlPanel />
        </aside>
        <main className="min-h-0">
          <LeafletMap />
        </main>
        <aside className="flex min-h-0 flex-col gap-4 overflow-y-auto">
          <FleetPanel />
          <EventLogPanel />
        </aside>
      </div>

      <TrafficIncidentModal />
      <UrgentOrderModal />
    </div>
  )
}
