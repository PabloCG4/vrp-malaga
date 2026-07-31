/**
 * Street network topology store (Zustand).
 *
 * A new, standalone store, deliberately separate from
 * `store/simulationStore.ts` (Block 1's store is not modified by this
 * block): it owns exactly one static resource, the Malaga street network
 * fetched once from `GET /api/v1/network`, and the two derived lookups the
 * map needs from it (nearest-node search for map clicks, and adjacency
 * checks for the interactive road-closure tool).
 */

import { create } from 'zustand'
import { getNetwork } from '../api/networkApi'
import { ApiError } from '../api/httpClient'
import type { NetworkNode } from '../types/network'

/** Approximate planar distance, in meters, between two lat/lon points (adequate at city scale). */
function approximateDistanceMeters(latA: number, lonA: number, latB: number, lonB: number): number {
  const metersPerDegreeLatitude = 111_320
  const latRadians = (latA * Math.PI) / 180
  const deltaLatMeters = (latB - latA) * metersPerDegreeLatitude
  const deltaLonMeters = (lonB - lonA) * metersPerDegreeLatitude * Math.cos(latRadians)
  return Math.hypot(deltaLatMeters, deltaLonMeters)
}

function edgeKey(nodeA: number, nodeB: number): string {
  return nodeA < nodeB ? `${nodeA}-${nodeB}` : `${nodeB}-${nodeA}`
}

/** Maximum distance, in meters, a map click may be from a node for it to be considered a match. */
const NEAREST_NODE_MAX_DISTANCE_METERS = 120

interface NetworkStoreState {
  depotNodeId: number | null
  nodesById: Map<number, NetworkNode>
  neighborsByNode: Map<number, number[]>
  edgeKeys: Set<string>
  isLoading: boolean
  error: string | null

  fetchNetwork: () => Promise<void>
  getNode: (nodeId: number) => NetworkNode | undefined
  getNeighbors: (nodeId: number) => NetworkNode[]
  areNodesAdjacent: (nodeA: number, nodeB: number) => boolean
  findNearestNode: (latitude: number, longitude: number) => NetworkNode | null
}

export const useNetworkStore = create<NetworkStoreState>((set, get) => ({
  depotNodeId: null,
  nodesById: new Map(),
  neighborsByNode: new Map(),
  edgeKeys: new Set(),
  isLoading: false,
  error: null,

  async fetchNetwork() {
    if (get().nodesById.size > 0 || get().isLoading) {
      return // Already loaded (or loading); the network never changes at runtime.
    }
    set({ isLoading: true, error: null })
    try {
      const graph = await getNetwork()
      const nodesById = new Map<number, NetworkNode>()
      for (const node of graph.nodes) {
        nodesById.set(node.node_id, node)
      }

      const neighborsByNode = new Map<number, number[]>()
      const edgeKeys = new Set<string>()
      for (const [nodeA, nodeB] of graph.edges) {
        edgeKeys.add(edgeKey(nodeA, nodeB))
        neighborsByNode.set(nodeA, [...(neighborsByNode.get(nodeA) ?? []), nodeB])
        neighborsByNode.set(nodeB, [...(neighborsByNode.get(nodeB) ?? []), nodeA])
      }

      set({
        depotNodeId: graph.depot_node_id,
        nodesById,
        neighborsByNode,
        edgeKeys,
        isLoading: false,
      })
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.detail
          : error instanceof Error
            ? error.message
            : 'Failed to load the street network.'
      set({ isLoading: false, error: message })
    }
  },

  getNode(nodeId: number) {
    return get().nodesById.get(nodeId)
  },

  getNeighbors(nodeId: number) {
    const { neighborsByNode, nodesById } = get()
    const neighborIds = neighborsByNode.get(nodeId) ?? []
    const neighbors: NetworkNode[] = []
    for (const neighborId of neighborIds) {
      const neighbor = nodesById.get(neighborId)
      if (neighbor !== undefined) {
        neighbors.push(neighbor)
      }
    }
    return neighbors
  },

  areNodesAdjacent(nodeA: number, nodeB: number) {
    return get().edgeKeys.has(edgeKey(nodeA, nodeB))
  },

  findNearestNode(latitude: number, longitude: number) {
    let closestNode: NetworkNode | null = null
    let closestDistanceMeters = NEAREST_NODE_MAX_DISTANCE_METERS
    for (const node of get().nodesById.values()) {
      const distanceMeters = approximateDistanceMeters(latitude, longitude, node.latitude, node.longitude)
      if (distanceMeters < closestDistanceMeters) {
        closestDistanceMeters = distanceMeters
        closestNode = node
      }
    }
    return closestNode
  },
}))
