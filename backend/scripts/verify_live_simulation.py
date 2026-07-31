"""
Manual, ad-hoc verification script for Phase 4 Block 3 (live simulation layer).

Connects to a running Control Tower API instance, opens the live simulation
WebSocket stream for an ACTIVE workday plan, injects one traffic incident and
one urgent order via the REST endpoints, and prints every telemetry message
observed. Not part of the automated pytest suite (see
`backend/tests/test_api_live_simulation.py` for that); this script is meant
to be run manually, once, against a live server, as documented in the
Phase 4 Block 3 changelog entry.

Usage (from the repository root, with the server already running):

    venv\\Scripts\\python -m backend.scripts.verify_live_simulation --workday-id 15 --base-url http://127.0.0.1:8010
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx
import websockets

from backend.src.topology.extractor import load_processed_graph


async def main(workday_id: int, base_url: str, tick_interval_seconds: float, message_budget: int) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        nodes_response = await client.get(f"/api/v1/workdays/{workday_id}/events/urgent-order-nodes")
        nodes_response.raise_for_status()
        eligible_nodes = nodes_response.json()
        print(f"Eligible urgent-order nodes: {len(eligible_nodes)} (using the first one).")
        delivery_node = eligible_nodes[0]["node_id"]

        plan_response = await client.get(f"/api/v1/workdays/{workday_id}")
        plan_response.raise_for_status()
        plan = plan_response.json()
        print(f"Workday {workday_id} status={plan['status']}, orders={len(plan['orders'])}.")

        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/api/v1/workdays/{workday_id}/live?tick_interval_seconds={tick_interval_seconds}"

        async with websockets.connect(ws_url) as websocket:
            snapshot = json.loads(await websocket.recv())
            print(f"[WS] snapshot: clock={snapshot['clock']}, vehicles={len(snapshot['vehicles'])}")

            street_network_graph = load_processed_graph()
            first_node, second_node, _ = next(iter(street_network_graph.edges(keys=True)))

            print("Injecting a traffic incident...")
            traffic_response = await client.post(
                f"/api/v1/workdays/{workday_id}/events/traffic",
                json={
                    "first_node": first_node,
                    "second_node": second_node,
                    "reopen_after_minutes": 20,
                    "description": "Manual verification traffic incident",
                },
            )
            print(f"  -> HTTP {traffic_response.status_code}: {traffic_response.json()}")

            print("Injecting an urgent order...")
            urgent_response = await client.post(
                f"/api/v1/workdays/{workday_id}/events/urgent-order",
                json={
                    "delivery_node": delivery_node,
                    "demand": 20.0,
                    "order_id": "URG-MANUAL-VERIFY",
                    "description": "Manual verification urgent order",
                },
            )
            print(f"  -> HTTP {urgent_response.status_code}: {urgent_response.json()}")

            print(f"Reading up to {message_budget} WebSocket messages...")
            for _ in range(message_budget):
                message = json.loads(await websocket.recv())
                message_type = message.get("type")
                if message_type == "tick":
                    print(f"[WS] tick: {message['clock']['formatted_time']} ({message['clock']['current_minute']} min)")
                else:
                    print(f"[WS] {message_type}: {message}")

        final_plan_response = await client.get(f"/api/v1/workdays/{workday_id}")
        final_plan_response.raise_for_status()
        final_plan = final_plan_response.json()
        print(f"\nFinal order count: {len(final_plan['orders'])} (started at {len(plan['orders'])}).")
        print(f"Final route stop count: {len(final_plan['route_stops'])}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workday-id", type=int, required=True)
    parser.add_argument("--base-url", type=str, default="http://127.0.0.1:8000")
    parser.add_argument("--tick-interval-seconds", type=float, default=0.5)
    parser.add_argument("--message-budget", type=int, default=15)
    args = parser.parse_args()

    asyncio.run(main(args.workday_id, args.base_url, args.tick_interval_seconds, args.message_budget))
