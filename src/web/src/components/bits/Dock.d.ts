import type { ReactNode } from "react";

export interface DockItemConfig {
  icon: ReactNode;
  label: string;
  onClick?: () => void;
  href?: string;
  ariaCurrent?: "page";
  className?: string;
}

export interface DockProps {
  items: DockItemConfig[];
  className?: string;
  spring?: { mass?: number; stiffness?: number; damping?: number };
  magnification?: number;
  distance?: number;
  panelHeight?: number;
  dockHeight?: number;
  baseItemSize?: number;
}

declare function Dock(props: DockProps): JSX.Element;
export default Dock;
