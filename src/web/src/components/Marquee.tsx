import { motion, useReducedMotion } from "motion/react";

const ITEMS = [
  "H3 CELLS", "6-HOUR WINDOWS", "CALIBRATED RISK", "TENANT ISOLATED",
  "REPLAY INGESTION", "WALK-FORWARD EVAL", "GROUNDED AI", "HUMAN REVIEW",
];

export default function Marquee() {
  const reduced = useReducedMotion();
  const row = ITEMS.flatMap((text) => [text, "•"]);
  return (
    <div className="marquee" aria-hidden>
      <motion.div
        className="marquee-track"
        animate={reduced ? undefined : { x: ["0%", "-50%"] }}
        transition={{ duration: 24, repeat: Infinity, ease: "linear" }}
      >
        {[...row, ...row].map((text, i) =>
          text === "•" ? (
            <span key={i} className="dot">•</span>
          ) : (
            <span key={i}>{text}</span>
          ),
        )}
      </motion.div>
    </div>
  );
}
