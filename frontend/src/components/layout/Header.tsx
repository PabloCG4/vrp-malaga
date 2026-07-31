import { useSimulationStore } from '../../store/simulationStore'
import { ConnectionStatusBadge } from '../panels/ConnectionStatusBadge'
import type { WorkdayStatus } from '../../types/enums'

const STATUS_STYLES: Record<WorkdayStatus, string> = {
  DRAFT: 'bg-warning/10 text-warning border-warning/30',
  ACTIVE: 'bg-success/10 text-success border-success/30',
  COMPLETED: 'bg-text-muted/10 text-text-muted border-border-strong',
}

/** Top application bar: brand, selected plan status, centered live simulation clock, and connection badge. */
export function Header() {
  const activePlan = useSimulationStore((state) => state.activePlan)
  const connectionStatus = useSimulationStore((state) => state.connectionStatus)
  const connectionDetail = useSimulationStore((state) => state.connectionDetail)
  const clock = useSimulationStore((state) => state.clock)

  return (
    <header className="grid grid-cols-3 items-center border-b border-border bg-surface-panel px-6 py-3">
      <div className="flex flex-col">
        <span className="text-lg font-bold text-text-heading">Control Tower</span>
        <span className="text-xs text-text-muted">Malaga Regional Depot &middot; Rich VRP</span>
      </div>

      <div className="flex flex-col items-center justify-self-center">
        <span className="font-mono text-3xl font-bold tabular-nums text-text-heading">
          {clock ? clock.formatted_time : '--:--'}
        </span>
        {activePlan && (
          <span className={`badge mt-1 border ${STATUS_STYLES[activePlan.status] ?? STATUS_STYLES.COMPLETED}`}>
            <span className="h-1.5 w-1.5 rounded-full bg-current" />
            Plan #{activePlan.id} &middot; {activePlan.status}
          </span>
        )}
      </div>

      <div className="flex items-center justify-end gap-3">
        <ConnectionStatusBadge status={connectionStatus} detail={connectionDetail} />
      </div>
    </header>
  )
}
