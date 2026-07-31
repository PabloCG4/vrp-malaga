/**
 * TypeScript mirrors of `api/schemas/network.py` (`GET /api/v1/network`).
 *
 * Workday-agnostic: the street network itself never changes at runtime, so
 * this payload is fetched once by `store/networkStore.ts` and reused by
 * every panel/map for the lifetime of the session.
 */

export interface NetworkNode {
  node_id: number
  latitude: number
  longitude: number
}

export interface NetworkGraph {
  depot_node_id: number
  nodes: NetworkNode[]
  edges: [number, number][]
}
