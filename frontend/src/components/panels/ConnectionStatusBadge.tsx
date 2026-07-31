import type { LiveConnectionStatus } from '../../services/liveSimulationSocket'

const STATUS_LABELS: Record<LiveConnectionStatus, string> = {
  idle: 'Not connected',
  connecting: 'Connecting…',
  open: 'Live',
  reconnecting: 'Reconnecting…',
  closed: 'Closed',
  error: 'Connection error',
}

const STATUS_DOT_COLORS: Record<LiveConnectionStatus, string> = {
  idle: 'bg-text-muted',
  connecting: 'bg-warning animate-pulse',
  open: 'bg-success',
  reconnecting: 'bg-warning animate-pulse',
  closed: 'bg-danger',
  error: 'bg-danger',
}

interface ConnectionStatusBadgeProps {
  status: LiveConnectionStatus
  detail?: string | null
}

/** Small colored pill summarizing the live WebSocket connection's current state. */
export function ConnectionStatusBadge({ status, detail }: ConnectionStatusBadgeProps) {
  return (
    <span className="badge border border-border-strong bg-surface-alt text-text" title={detail ?? undefined}>
      <span className={`h-1.5 w-1.5 rounded-full ${STATUS_DOT_COLORS[status]}`} aria-hidden="true" />
      {STATUS_LABELS[status]}
    </span>
  )
}
