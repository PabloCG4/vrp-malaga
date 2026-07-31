import { Header } from './Header'
import { WorkdaySelectorPanel } from '../panels/WorkdaySelectorPanel'
import { ControlPanel } from '../panels/ControlPanel'
import { TelemetryPanel } from '../panels/TelemetryPanel'
import { EventLogPanel } from '../panels/EventLogPanel'
import { MapPlaceholder } from '../map/MapPlaceholder'

/**
 * Root dashboard shell: header, left-hand sidebar (workday selection and
 * dispatch controls), central map slot, and right-hand telemetry/activity
 * feed. Every panel is a self-contained view over `useSimulationStore`; this
 * component only arranges them and holds no state of its own, so Block 2
 * can slot in a real map/richer panels without touching this layout.
 */
export function DashboardLayout() {
  return (
    <div className="dashboard">
      <Header />
      <div className="dashboard__body">
        <aside className="dashboard__sidebar">
          <WorkdaySelectorPanel />
          <ControlPanel />
        </aside>
        <main className="dashboard__main">
          <MapPlaceholder />
        </main>
        <aside className="dashboard__telemetry">
          <TelemetryPanel />
          <EventLogPanel />
        </aside>
      </div>
    </div>
  )
}
