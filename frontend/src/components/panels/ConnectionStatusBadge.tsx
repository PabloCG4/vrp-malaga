import type { LiveConnectionStatus } from '../../services/liveSimulationSocket'

const STATUS_LABELS: Record<LiveConnectionStatus, string> = {
  idle: 'Not connected',
  connecting: 'Connecting…',
  open: 'Live',
  reconnecting: 'Reconnecting…',
  closed: 'Closed',
  error: 'Connection error',
}

interface ConnectionStatusBadgeProps {
  status: LiveConnectionStatus
  detail?: string | null
}

/** Small colored pill summarizing the live WebSocket connection's current state. */
export function ConnectionStatusBadge({ status, detail }: ConnectionStatusBadgeProps) {
  return (
    <span className={`status-badge status-badge--${status}`} title={detail ?? undefined}>
      <span className="status-badge__dot" aria-hidden="true" />
      {STATUS_LABELS[status]}
    </span>
  )
}
