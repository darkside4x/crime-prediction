export interface CircularGalleryItem { image: string; text: string; }

export interface CircularGalleryProps {
  items?: CircularGalleryItem[];
  bend?: number;
  textColor?: string;
  borderRadius?: number;
  font?: string;
  fontUrl?: string;
  scrollSpeed?: number;
  scrollEase?: number;
}

declare function CircularGallery(props: CircularGalleryProps): JSX.Element;
export default CircularGallery;
