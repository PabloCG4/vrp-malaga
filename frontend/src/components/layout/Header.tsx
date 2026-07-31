import { useSimulationStore } from '../../store/simulationStore'
import { ConnectionStatusBadge } from '../panels/ConnectionStatusBadge'

/** Top application bar: brand, selected plan summary and live connection status. */
export function Header() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const connectionStatus = useSimulationStore((state) => state.connectionStatus)
  const connectionDetail = useSimulationStore((state) => state.connectionDetail)
  const clock = useSimulationStore((state) => state.clock)

  return (
    <header className="app-header">
      <div className="app-header__brand">
        <span className="app-header__title">Control Tower</span>
        <span className="app-header__subtitle">Malaga Regional Depot &middot; Rich VRP</span>
      </div>

      <div className="app-header__status">
        {activePlan && (
          <span className="app-header__plan">
            Plan #{activePlan.id} &middot; {activePlan.workday_date} &middot; {activePlan.status}
          </span>
        )}
        {clock && <span className="app-header__clock">{clock.formatted_time}</span>}
        <ConnectionStatusBadge status={connectionStatus} detail={connectionDetail} />
      </div>
    </header>
  )
}
