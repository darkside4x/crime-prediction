import type { CSSProperties } from "react";

export interface ReflectiveCardProps {
  blurStrength?: number;
  color?: string;
  metalness?: number;
  roughness?: number;
  overlayColor?: string;
  displacementStrength?: number;
  noiseScale?: number;
  specularConstant?: number;
  grayscale?: number;
  glassDistortion?: number;
  className?: string;
  style?: CSSProperties;
  enableCamera?: boolean;
  badgeLabel?: string;
  name?: string;
  role?: string;
  idLabel?: string;
  idValue?: string;
}

declare function ReflectiveCard(props: ReflectiveCardProps): JSX.Element;
export default ReflectiveCard;
