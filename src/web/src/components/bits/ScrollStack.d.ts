import type { ReactNode } from "react";

export interface ScrollStackProps {
  children?: ReactNode;
  className?: string;
  itemDistance?: number;
  itemScale?: number;
  itemStackDistance?: number;
  stackPosition?: string | number;
  scaleEndPosition?: string | number;
  baseScale?: number;
  scaleDuration?: number;
  rotationAmount?: number;
  blurAmount?: number;
  useWindowScroll?: boolean;
  onStackComplete?: () => void;
}

export declare function ScrollStackItem(props: { children?: ReactNode; itemClassName?: string }): JSX.Element;
declare function ScrollStack(props: ScrollStackProps): JSX.Element;
export default ScrollStack;
