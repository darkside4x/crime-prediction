import { lazy, Suspense } from "react";
import { motion, useScroll, useSpring } from "motion/react";
import Hero from "./components/Hero";
import Marquee from "./components/Marquee";
import HowItWorks from "./components/HowItWorks";
import { AuthProvider } from "./console/AuthContext";
import { useHashRoute } from "./console/router";

const ConsoleShell = lazy(() => import("./console/ConsoleShell"));

function Landing() {
  const { scrollYProgress } = useScroll();
  const progress = useSpring(scrollYProgress, { stiffness: 120, damping: 30, restDelta: 0.001 });

  return (
    <div className="github-landing">
      <motion.div className="scroll-progress" style={{ scaleX: progress }} />
      <nav className="nav">
        <a className="nav-logo" href="#top" aria-label="CivicHalo home">CIVIC<span>HALO</span></a>
        <div className="nav-links">
          <a href="#how">Pipeline</a>
          <a href="#/console">Console</a>
          <a href="#limits">Limitations</a>
        </div>
      </nav>
      <Hero />
      <Marquee />
      <HowItWorks />
      <section className="console-cta" id="dashboard">
        <div className="container">
          <motion.h2
            className="section-title"
            initial={{ opacity: 0, y: 40 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, amount: 0.4 }}
            transition={{ duration: 0.7 }}
          >
            THE <span className="accent">CONSOLE</span>
          </motion.h2>
          <p>
            Sign in to the authenticated console for the forecast map, candidate review,
            recorded-video sources, coverage health, and the model card — scoped to your
            tenant and role.
          </p>
          <motion.a
            className="btn btn-red"
            href="#/console"
            whileHover={{ scale: 1.04 }}
            whileTap={{ scale: 0.96 }}
          >
            Open the console
          </motion.a>
        </div>
      </section>
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
            CivicHalo · human-reviewed urban safety intelligence · hackathon prototype
          </p>
        </div>
      </footer>
    </div>
  );
}

export default function App() {
  const hash = useHashRoute();
  const inConsole = hash.startsWith("#/console");

  return (
    <AuthProvider>
      {inConsole ? (
        <Suspense fallback={<p className="muted">Loading secure console…</p>}>
          <ConsoleShell />
        </Suspense>
      ) : (
        <Landing />
      )}
    </AuthProvider>
  );
}
