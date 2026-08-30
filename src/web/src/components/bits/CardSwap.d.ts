import type { CSSProperties, ReactNode } from "react";

export declare const Card: React.ForwardRefExoticComponent<
  { customClass?: string; className?: string; style?: CSSProperties; children?: ReactNode } &
  React.RefAttributes<HTMLDivElement>
>;

export interface CardSwapProps {
  width?: number;
  height?: number;
  cardDistance?: number;
  verticalDistance?: number;
  delay?: number;
  pauseOnHover?: boolean;
  onCardClick?: (index: number) => void;
  skewAmount?: number;
  easing?: "elastic" | "power";
  children?: ReactNode;
}

declare function CardSwap(props: CardSwapProps): JSX.Element;
export default CardSwap;
