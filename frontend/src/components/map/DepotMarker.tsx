import { Marker, Popup } from 'react-leaflet'
import { createDepotIcon } from './mapIcons'
import type { NetworkNode } from '../../types/network'

const DEPOT_ICON = createDepotIcon()

interface DepotMarkerProps {
  depotNode: NetworkNode
}

/** The single, fixed physical depot every workday plan departs from and returns to. */
export function DepotMarker({ depotNode }: DepotMarkerProps) {
  return (
    <Marker position={[depotNode.latitude, depotNode.longitude]} icon={DEPOT_ICON}>
      <Popup>
        <div className="text-sm">
          <p className="font-semibold text-text-heading">Regional Depot</p>
          <p className="mt-1 font-mono text-xs text-text-muted">Node {depotNode.node_id}</p>
        </div>
      </Popup>
    </Marker>
  )
}
