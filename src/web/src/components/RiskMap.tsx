import { useEffect, useRef } from "react";
import maplibregl, { Map as MLMap } from "maplibre-gl";
import type { RiskCollection } from "../api";

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

interface Props {
  data: RiskCollection | undefined;
  onSelect: (cellId: string | null) => void;
  selected: string | null;
}

export default function RiskMap({ data, onSelect, selected }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MLMap | null>(null);
  const loadedRef = useRef(false);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

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
      map.addSource("risk", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({
        id: "risk-fill",
        type: "fill",
        source: "risk",
        paint: {
          "fill-color": [
            "case",
            ["get", "suppressed"], "rgba(120,120,130,0.12)",
            ["interpolate", ["linear"], ["get", "risk"],
              0, "rgba(43,15,20,0.55)",
              0.25, "rgba(122,16,39,0.6)",
              0.5, "rgba(244,12,63,0.62)",
              0.75, "rgba(255,95,74,0.68)",
              1, "rgba(255,182,72,0.75)"],
          ],
          "fill-opacity": 0.85,
        },
      });
      map.addLayer({
        id: "risk-line",
        type: "line",
        source: "risk",
        paint: { "line-color": "rgba(255,240,235,0.16)", "line-width": 0.7 },
      });
      map.addLayer({
        id: "risk-selected",
        type: "line",
        source: "risk",
        paint: { "line-color": "#fff0eb", "line-width": 2.4 },
        filter: ["==", ["id"], ""],
      });
      map.on("click", "risk-fill", (event) => {
        const feature = event.features?.[0];
        onSelectRef.current(feature ? String(feature.id) : null);
      });
      map.on("mouseenter", "risk-fill", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "risk-fill", () => { map.getCanvas().style.cursor = ""; });
      loadedRef.current = true;
      if (dataRef.current) applyData(map, dataRef.current);
    });
    mapRef.current = map;
    return () => { map.remove(); mapRef.current = null; loadedRef.current = false; };
  }, []);

  const dataRef = useRef<RiskCollection | undefined>(undefined);

  const applyData = (map: MLMap, collection: RiskCollection) => {
    const source = map.getSource("risk") as maplibregl.GeoJSONSource | undefined;
    if (!source) return;
    source.setData(collection as unknown as GeoJSON.FeatureCollection);
    if (collection.features.length > 0) {
      const bounds = new maplibregl.LngLatBounds();
      for (const feature of collection.features) {
        for (const [lng, lat] of feature.geometry.coordinates[0]) bounds.extend([lng, lat]);
      }
      map.fitBounds(bounds, { padding: 48, duration: 900, maxZoom: 12.5 });
    }
  };

  useEffect(() => {
    dataRef.current = data;
    const map = mapRef.current;
    if (map && loadedRef.current && data) applyData(map, data);
  }, [data]);

  useEffect(() => {
    const map = mapRef.current;
    if (map && loadedRef.current) {
      map.setFilter("risk-selected", ["==", ["id"], selected ?? ""]);
    }
  }, [selected]);

  return <div ref={containerRef} className="map-canvas" role="application" aria-label="Risk map" />;
}
