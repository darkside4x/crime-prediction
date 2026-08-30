import { useRef } from "react";
import { motion, useReducedMotion, useScroll, useTransform } from "motion/react";

const line = {
  hidden: { y: "110%" },
  visible: (i: number) => ({
    y: "0%",
    transition: { duration: 0.8, delay: 0.12 * i, ease: [0.22, 1, 0.36, 1] as const },
  }),
};

export default function Hero() {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ["start start", "end start"],
  });
  const glowY = useTransform(scrollYProgress, [0, 1], ["0%", reduced ? "0%" : "30%"]);
  const titleY = useTransform(scrollYProgress, [0, 1], ["0%", reduced ? "0%" : "-18%"]);
  const fade = useTransform(scrollYProgress, [0, 0.7], [1, 0]);

  return (
    <section className="hero" id="top" ref={ref}>
      <motion.div className="hero-glow" style={{ y: glowY }} />
      <div className="hero-grid" />
      <motion.div className="container" style={{ y: titleY, opacity: fade }}>
        <motion.p
          className="eyebrow"
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
        >
          Aggregate area-level forecasting · decision support · human in the loop
        </motion.p>
        <h1 className="hero-title" aria-label="City signals, forecast ahead">
          {["CITY", "SIGNALS,", "FORECAST"].map((word, i) => (
            <span key={word} style={{ display: "block", overflow: "hidden" }}>
              <motion.span
                style={{ display: "block" }}
                className={i === 2 ? "line-red" : undefined}
                custom={i}
                initial="hidden"
                animate="visible"
                variants={line}
              >
                {word}
              </motion.span>
            </span>
          ))}
          <span style={{ display: "block", overflow: "hidden" }}>
            <motion.span
              style={{ display: "block" }}
              className="outline-text"
              custom={3}
              initial="hidden"
              animate="visible"
              variants={line}
            >
              AHEAD.
            </motion.span>
          </span>
        </h1>
        <motion.p
          className="hero-sub"
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.5 }}
        >
          Calibrated incident-risk estimates for H3 grid cells over the next six-hour
          window — with uncertainty, drivers, and a model card. No individual scores.
          No enforcement recommendations. Ever.
        </motion.p>
        <motion.div
          className="hero-cta-row"
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.65 }}
        >
          <motion.a className="btn btn-red" href="#dashboard" whileHover={{ scale: 1.04 }} whileTap={{ scale: 0.96 }}>
            Open the map
          </motion.a>
          <motion.a className="btn" href="#how" whileHover={{ scale: 1.04 }} whileTap={{ scale: 0.96 }}>
            How it works
          </motion.a>
        </motion.div>
      </motion.div>
      <motion.div
        className="hero-scroll-hint"
        animate={reduced ? undefined : { y: [0, 8, 0] }}
        transition={{ duration: 1.8, repeat: Infinity }}
      >
        SCROLL ↓
      </motion.div>
    </section>
  );
}
