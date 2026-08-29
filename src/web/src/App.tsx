import { motion, useScroll, useSpring } from "motion/react";
import Hero from "./components/Hero";
import Marquee from "./components/Marquee";
import HowItWorks from "./components/HowItWorks";
import Dashboard from "./components/Dashboard";

export default function App() {
  const { scrollYProgress } = useScroll();
  const progress = useSpring(scrollYProgress, { stiffness: 120, damping: 30, restDelta: 0.001 });

  return (
    <>
      <motion.div className="scroll-progress" style={{ scaleX: progress }} />
      <nav className="nav">
        <a className="nav-logo" href="#top">HOT<span>SPOT</span></a>
        <div className="nav-links">
          <a href="#how">Pipeline</a>
          <a href="#dashboard">Risk map</a>
          <a href="#limits">Limitations</a>
        </div>
      </nav>
      <Hero />
      <Marquee />
      <HowItWorks />
      <Dashboard />
      <footer className="footer" id="limits">
        <div className="container">
          <motion.div
            className="big"
            initial={{ opacity: 0, y: 40 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, amount: 0.6 }}
            transition={{ duration: 0.8 }}
          >
            FORECASTS, NOT VERDICTS.
          </motion.div>
          <p>
            This prototype estimates aggregate area-level incident risk to support
            human planning. It must not be used for individual criminality
            assessment, suspect identification, or automated enforcement decisions.
            Historical data can reflect reporting and enforcement patterns — treat
            every cell as an uncertain forecast, never as ground truth.
          </p>
          <p style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}>
            crime-hotspot prototype · hackathon demo · person 3: api / web / integration
          </p>
        </div>
      </footer>
    </>
  );
}
