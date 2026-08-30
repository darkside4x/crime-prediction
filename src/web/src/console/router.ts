/** Minimal hash router — keeps the landing page at "#/" and console views under "#/console". */

import { useEffect, useState } from "react";

export type ConsoleRoute =
  | "live"
  | "map"
  | "sources"
  | "processing"
  | "review"
  | "response"
  | "mobile-capture"
  | "model-card";

export function useHashRoute(): string {
  const [hash, setHash] = useState(() => window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export function consoleRoute(hash: string): ConsoleRoute | null {
  if (!hash.startsWith("#/console")) return null;
  const rest = hash.slice("#/console".length).replace(/^\//, "");
  if (rest === "") return "live";
  if (
    rest === "live" ||
    rest === "map" ||
    rest === "sources" ||
    rest === "processing" ||
    rest === "review" ||
    rest === "response" ||
    rest === "mobile-capture" ||
    rest === "model-card"
  )
    return rest;
  return "map";
}

export function navigate(hash: string): void {
  window.location.hash = hash;
}
