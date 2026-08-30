import { useEffect, useRef } from "react";
import maplibregl, { Map as MLMap } from "maplibre-gl";
import { cellToBoundary } from "h3-js";
import type { OperationalAggregateForecast } from "../api/client";

const MAP_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    carto: {
      type: "raster",
      tiles: [
        "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
      ],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors © CARTO",
    },
  },
  layers: [{ id: "carto", type: "raster", source: "carto" }],
};

const BAND_RISK: Record<string, number> = {
  low: 0.15,
  typical: 0.4,
  elevated: 0.7,
  high: 0.95,
  suppressed: 0,
};

export function toFeatureCollection(
  items: OperationalAggregateForecast[],
): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: items.map((item) => ({
      type: "Feature",
      id: item.forecast_id,
      geometry: {
        type: "Polygon",
        coordinates: [
          [...cellToBoundary(item.cell_id, true)],
        ],
      },
      properties: {
        forecast_id: item.forecast_id,
        cell_id: item.cell_id,
        suppressed: item.suppression.suppressed,
        risk: item.suppression.suppressed
          ? 0
          : item.occurrence_probability.value ?? BAND_RISK[item.risk_band] ?? 0,
        risk_band: item.risk_band,
      },
    })),
  };
}

interface Props {
  items: OperationalAggregateForecast[] | undefined;
  selected: string | null;
  onSelect: (forecastId: string | null) => void;
}

export default function ForecastMap({ items, selected, onSelect }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const loadedRef = useRef(false);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const dataRef = useRef<GeoJSON.FeatureCollection | null>(null);
  const fittedRef = useRef(false);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: MAP_STYLE,
      center: [77.5946, 12.9716],
      zoom: 11.2,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");
    map.on("load", () => {
      map.addSource("forecasts", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "forecast-fill",
        type: "fill",
        source: "forecasts",
        paint: {
          "fill-color": [
            "case",
            ["get", "suppressed"],
            "rgba(120,120,130,0.16)",
            [
              "interpolate",
              ["linear"],
              ["get", "risk"],
              0, "rgba(65,20,27,0.42)",
              0.25, "rgba(111,21,35,0.52)",
              0.5, "rgba(176,24,47,0.62)",
              0.75, "rgba(226,31,61,0.72)",
              1, "rgba(244,12,63,0.84)",
            ],
          ],
          "fill-opacity": 0.85,
        },
      });
      // Distinct outline so suppressed cells never read as "low risk".
      map.addLayer({
        id: "forecast-suppressed-line",
        type: "line",
        source: "forecasts",
        paint: {
          "line-color": "rgba(170,170,180,0.7)",
          "line-width": 1.4,
          "line-dasharray": [2, 2],
        },
        filter: ["==", ["get", "suppressed"], true],
      });
      map.addLayer({
        id: "forecast-line",
        type: "line",
        source: "forecasts",
        paint: { "line-color": "rgba(255,240,235,0.16)", "line-width": 0.7 },
        filter: ["==", ["get", "suppressed"], false],
      });
      map.addLayer({
        id: "forecast-selected",
        type: "line",
        source: "forecasts",
        paint: { "line-color": "#fff0eb", "line-width": 2.4 },
        filter: ["==", ["id"], ""],
      });
      map.on("click", "forecast-fill", (event) => {
        const feature = event.features?.[0];
        onSelectRef.current(feature ? String(feature.id) : null);
      });
      map.on("mouseenter", "forecast-fill", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "forecast-fill", () => {
        map.getCanvas().style.cursor = "";
      });
      loadedRef.current = true;
      if (dataRef.current) applyData(map, dataRef.current);
    });
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      loadedRef.current = false;
      fittedRef.current = false;
    };
  }, []);

  const applyData = (map: MLMap, collection: GeoJSON.FeatureCollection) => {
    const source = map.getSource("forecasts") as maplibregl.GeoJSONSource | undefined;
    if (!source) return;
    source.setData(collection);
    if (!fittedRef.current && collection.features.length > 0) {
      const bounds = new maplibregl.LngLatBounds();
      for (const feature of collection.features) {
        const polygon = feature.geometry as GeoJSON.Polygon;
        for (const [lng, lat] of polygon.coordinates[0]) bounds.extend([lng, lat]);
      }
      map.fitBounds(bounds, { padding: 48, duration: 900, maxZoom: 12.5 });
      fittedRef.current = true;
    }
  };

  useEffect(() => {
    const collection = items ? toFeatureCollection(items) : null;
    dataRef.current = collection;
    const map = mapRef.current;
    if (map && loadedRef.current && collection) applyData(map, collection);
  }, [items]);

  useEffect(() => {
    const map = mapRef.current;
    if (map && loadedRef.current) {
      map.setFilter("forecast-selected", ["==", ["id"], selected ?? ""]);
    }
  }, [selected]);

  return (
    <div
      ref={containerRef}
      className="map-canvas"
      role="application"
      aria-label="Aggregate forecast map"
    />
  );
}
