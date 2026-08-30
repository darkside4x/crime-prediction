import { useEffect, useMemo, useRef } from "react";
import maplibregl, { type Map as MLMap } from "maplibre-gl";
import { cellToBoundary } from "h3-js";
import "maplibre-gl/dist/maplibre-gl.css";
import type { SourceMapLocation } from "../api/client";

const STYLE = "https://tiles.openfreemap.org/styles/dark";

export default function SourceLocationMap({ location }: { location: SourceMapLocation }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MLMap | null>(null);
  const ring = useMemo(() => cellToBoundary(location.cell_id, true), [location.cell_id]);

  useEffect(() => {
    if (!container.current) return;
    map.current?.remove();
    const bounds = new maplibregl.LngLatBounds();
    ring.forEach(([lng, lat]) => bounds.extend([lng, lat]));
    const instance = new maplibregl.Map({
      container: container.current,
      style: STYLE,
      bounds,
      fitBoundsOptions: { padding: 44, maxZoom: 14 },
      attributionControl: { compact: true },
    });
    instance.on("load", () => {
      instance.addSource("source-location", {
        type: "geojson",
        data: {
          type: "Feature",
          properties: {},
          geometry: { type: "Polygon", coordinates: [ring] },
        },
      });
      instance.addLayer({
        id: "source-location-fill",
        type: "fill",
        source: "source-location",
        paint: { "fill-color": "#f40c3f", "fill-opacity": 0.34 },
      });
      instance.addLayer({
        id: "source-location-line",
        type: "line",
        source: "source-location",
        paint: { "line-color": "#fff0eb", "line-width": 2.5 },
      });
    });
    map.current = instance;
    return () => {
      instance.remove();
      if (map.current === instance) map.current = null;
    };
  }, [ring]);

  return (
    <section className="source-location-card" aria-label={`${location.source_name} map location`}>
      <div>
        <strong>{location.source_name}</strong>
        <span className="muted">
          Approximate H3 resolution {location.h3_resolution} area · {location.cell_id}
        </span>
      </div>
      <div ref={container} className="source-location-map" />
    </section>
  );
}
