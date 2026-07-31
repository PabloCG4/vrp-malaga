/** REST client for the read-only street network topology endpoint (`routers/network.py`). */

import { apiRequest } from './httpClient'
import type { NetworkGraph } from '../types/network'

/** `GET /api/v1/network` - the full Malaga street network's nodes, adjacency list and depot. */
export function getNetwork(signal?: AbortSignal): Promise<NetworkGraph> {
  return apiRequest<NetworkGraph>('/api/v1/network', { ...(signal !== undefined && { signal }) })
}
