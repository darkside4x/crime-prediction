import { useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";

gsap.registerPlugin(useGSAP);

const ITEMS = [
  "H3 cells", "6-hour windows", "Calibrated risk", "Tenant isolated",
  "Replay ingestion", "Walk-forward eval", "Grounded AI", "Human review",
];

export default function Marquee() {
  const ref = useRef<HTMLDivElement>(null);
  const row = ITEMS.flatMap((text) => [text, "•"]);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add("(prefers-reduced-motion: no-preference)", () => {
        gsap.to(".marquee-track", { xPercent: -50, ease: "none", duration: 28, repeat: -1 });
      });
    },
    { scope: ref },
  );

  return (
    <div className="marquee" aria-hidden ref={ref}>
      <div className="marquee-track">
        {[...row, ...row].map((text, i) =>
          text === "•" ? (
            <span key={i} className="dot">•</span>
          ) : (
            <span key={i} className="marquee-chip">{text}</span>
          ),
        )}
      </div>
    </div>
  );
}
