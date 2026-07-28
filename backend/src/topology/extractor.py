"""
Street network extraction and visualization utilities for the VRP Simulator.

This module is responsible for downloading the drivable street network of a
target urban area from OpenStreetMap (via OSMnx), reducing it to its largest
strongly connected component so that it is suitable for vehicle routing,
imputing speed limits and travel times for every edge, and rendering the
result on an interactive Leaflet map (via Folium) for visual validation,
including the directionality of each street. The fully processed graph is
also serialized to disk so that the backend can load it instantaneously at
startup instead of re-downloading and reprocessing it on every run.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import folium
import folium.plugins
import networkx as nx
import osmnx as ox

# Approximate geographic center of Malaga, Andalusia, Spain (Plaza de la Constitucion area).
MALAGA_CITY_CENTER_COORDINATES: tuple[float, float] = (36.7213, -4.4213)

# Radius, in meters, defining the square bounding box retained around the center point.
NETWORK_RADIUS_METERS: float = 2000.0

# Destination path for the generated interactive map preview.
OUTPUT_MAP_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "malaga_preview.html"

# Destination path for the serialized, fully processed graph.
PROCESSED_GRAPH_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "malaga_graph.pkl"

# Typical free-flow speeds (km per hour) per OSM highway type, used to impute
# missing "maxspeed" values. These reflect common Spanish urban and interurban
# speed limits and are only applied to edges of a given highway type that have
# no preexisting "maxspeed" tag on any edge of that same type.
DEFAULT_HIGHWAY_SPEEDS_KPH: dict[str, float] = {
    "motorway": 100.0,
    "motorway_link": 70.0,
    "trunk": 90.0,
    "trunk_link": 50.0,
    "primary": 50.0,
    "primary_link": 40.0,
    "secondary": 50.0,
    "secondary_link": 40.0,
    "tertiary": 50.0,
    "tertiary_link": 40.0,
    "unclassified": 40.0,
    "residential": 30.0,
    "living_street": 20.0,
    "service": 20.0,
    "pedestrian": 10.0,
    "track": 20.0,
    "road": 30.0,
}

# Fallback speed (km per hour) for any highway type absent from
# DEFAULT_HIGHWAY_SPEEDS_KPH that also has no preexisting "maxspeed" data.
FALLBACK_SPEED_KPH: float = 30.0


def download_drive_network(
    center_point: tuple[float, float] = MALAGA_CITY_CENTER_COORDINATES,
    radius_meters: float = NETWORK_RADIUS_METERS,
) -> nx.MultiDiGraph:
    """
    Download the drivable street network around a given geographic point.

    The "drive_service" network type is used instead of "drive" so that
    restricted-access service roads (for example, those found in historical
    city centers) are included, as they are traversable by delivery vehicles.

    Parameters
    ----------
    center_point:
        Latitude and longitude, in degrees (EPSG:4326), of the area of interest.
    radius_meters:
        Radius, in meters, defining the square bounding box retained around
        the center point.

    Returns
    -------
    networkx.MultiDiGraph
        The street network graph restricted to its largest strongly
        connected component, guaranteeing that every node is reachable from
        every other node while respecting street directionality.
    """
    street_network_graph = ox.graph_from_point(
        center_point,
        dist=radius_meters,
        dist_type="bbox",
        network_type="drive_service",
        simplify=True,
        retain_all=True,
    )

    # A vehicle routing engine requires that any node be reachable from any other
    # node while respecting one-way streets. osmnx only guarantees weak connectivity,
    # so the largest strongly connected component must be extracted explicitly here.
    strongly_connected_components = nx.strongly_connected_components(street_network_graph)
    largest_strongly_connected_component = max(strongly_connected_components, key=len)
    strongly_connected_street_network = street_network_graph.subgraph(
        largest_strongly_connected_component
    ).copy()

    return strongly_connected_street_network


def compute_edge_travel_times(street_network_graph: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """
    Impute missing speed limits and compute free-flow travel times for every edge.

    OpenStreetMap "maxspeed" tags are frequently missing, so speed limits are
    first imputed per edge using `DEFAULT_HIGHWAY_SPEEDS_KPH` as a per-highway
    fallback (with `FALLBACK_SPEED_KPH` as a last resort), then travel times
    are derived from each edge's length and imputed speed. This produces a
    fully weighted graph, which is required as an input for the shortest-path
    and routing algorithms of the simulation engine.

    Parameters
    ----------
    street_network_graph:
        The graph whose edges must hold a "length" attribute, as produced by
        OSMnx.

    Returns
    -------
    networkx.MultiDiGraph
        The same graph, enriched with "speed_kph" and "travel_time" edge
        attributes.
    """
    street_network_graph_with_speeds = ox.add_edge_speeds(
        street_network_graph,
        hwy_speeds=DEFAULT_HIGHWAY_SPEEDS_KPH,
        fallback=FALLBACK_SPEED_KPH,
    )
    street_network_graph_with_travel_times = ox.add_edge_travel_times(street_network_graph_with_speeds)

    return street_network_graph_with_travel_times


def save_processed_graph(
    street_network_graph: nx.MultiDiGraph, output_path: Path = PROCESSED_GRAPH_PATH
) -> None:
    """
    Serialize the fully processed graph to disk using pickle.

    Pickle is chosen over text-based formats (such as GraphML) because it
    preserves the exact in-memory `MultiDiGraph` structure and its attribute
    types without any parsing overhead, allowing the backend to deserialize
    it directly into a ready-to-use NetworkX object when the simulation
    starts, rather than re-downloading and reprocessing the network on every
    run.

    Parameters
    ----------
    street_network_graph:
        The processed graph (strongly connected, with speed and travel time
        attributes) to persist.
    output_path:
        Destination path for the generated pickle file. Parent directories
        are created automatically if they do not already exist.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as processed_graph_file:
        pickle.dump(street_network_graph, processed_graph_file, protocol=pickle.HIGHEST_PROTOCOL)


def build_interactive_map(street_network_graph: nx.MultiDiGraph) -> folium.Map:
    """
    Render a MultiDiGraph street network as an interactive Folium map.

    Parameters
    ----------
    street_network_graph:
        The graph to visualize, expected to hold "x"/"y" node attributes and
        edge geometries as produced by OSMnx.

    Returns
    -------
    folium.Map
        A Folium map with the network edges and nodes drawn over an
        OpenStreetMap-derived base layer, ready to be saved to disk.
    """
    node_coordinates: list[tuple[float, float]] = [
        (node_data["y"], node_data["x"]) for node_id, node_data in street_network_graph.nodes(data=True)
    ]
    average_latitude = sum(latitude for latitude, longitude in node_coordinates) / len(node_coordinates)
    average_longitude = sum(longitude for latitude, longitude in node_coordinates) / len(node_coordinates)

    # Create a new Folium map with the average latitude and longitude of the nodes.
    interactive_map = folium.Map(
        location=(average_latitude, average_longitude),
        zoom_start=15,
        tiles="cartodbpositron",
    )

    # Convert the street network graph to a GeoDataFrame of edges and nodes.
    edges_geodataframe = ox.graph_to_gdfs(street_network_graph, nodes=False, edges=True)

    # Iterate over the edges and add them to the map.
    for edge_key, edge_row in edges_geodataframe.iterrows():
        edge_coordinates = [
            (latitude, longitude) for longitude, latitude in edge_row["geometry"].coords
        ]
        edge_polyline = folium.PolyLine(
            locations=edge_coordinates,
            color="#2c3e50",
            weight=2,
            opacity=0.8,
        )
        edge_polyline.add_to(interactive_map)

        # Attach a repeating arrow symbol along the edge to visualize its
        # directionality, since the graph is a DiGraph and every edge represents
        # a one-way traversal (two-way streets appear as a pair of opposite edges).
        folium.plugins.PolyLineTextPath(
            edge_polyline,
            text=" ► ",
            repeat=True,
            offset=6,
            attributes={"fill": "#2c3e50", "font-weight": "bold", "font-size": "14"},
        ).add_to(interactive_map)

    for latitude, longitude in node_coordinates:
        # Add a circle marker for each node.
        folium.CircleMarker(
            location=(latitude, longitude),
            radius=1.5,
            color="#e74c3c",
            fill=True,
            fill_opacity=0.9,
        ).add_to(interactive_map)

    return interactive_map


def save_map_to_html(interactive_map: folium.Map, output_path: Path = OUTPUT_MAP_PATH) -> None:
    """
    Persist a Folium map to disk as a standalone HTML file.

    Parameters
    ----------
    interactive_map:
        The Folium map to save.
    output_path:
        Destination path for the generated HTML file. Parent directories are
        created automatically if they do not already exist.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    interactive_map.save(str(output_path))


if __name__ == "__main__":
    print("Downloading the drivable street network of Malaga city center.")
    malaga_street_network = download_drive_network()
    print(
        f"Network downloaded: {malaga_street_network.number_of_nodes()} nodes, "
        f"{malaga_street_network.number_of_edges()} edges."
    )

    print("Imputing speed limits and computing edge travel times.")
    malaga_street_network = compute_edge_travel_times(malaga_street_network)

    print("Building the interactive visualization map.")
    malaga_interactive_map = build_interactive_map(malaga_street_network)

    save_map_to_html(malaga_interactive_map)
    print(f"Map saved to: {OUTPUT_MAP_PATH}")

    save_processed_graph(malaga_street_network)
    print(f"Processed graph serialized to: {PROCESSED_GRAPH_PATH}")
