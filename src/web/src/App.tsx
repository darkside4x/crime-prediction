import { lazy, Suspense, useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Hero from "./components/Hero";
import Marquee from "./components/Marquee";
import HowItWorks from "./components/HowItWorks";
import { AuthProvider } from "./console/AuthContext";
import { useHashRoute } from "./console/router";

gsap.registerPlugin(useGSAP, ScrollTrigger);

const ConsoleShell = lazy(() => import("./console/ConsoleShell"));

function Landing() {
  const ref = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      gsap.set(".scroll-progress", { scaleX: 0 });
      gsap.to(".scroll-progress", {
        scaleX: 1,
        ease: "none",
        scrollTrigger: { start: 0, end: "max", scrub: 0.3 },
      });

      const mm = gsap.matchMedia();
      mm.add("(prefers-reduced-motion: no-preference)", () => {
        gsap.from(".cta-card > *", {
          autoAlpha: 0,
          y: 40,
          duration: 0.8,
          stagger: 0.12,
          ease: "power3.out",
          scrollTrigger: { trigger: ".cta-card", start: "top 74%" },
        });
        gsap.from(".footer .big", {
          autoAlpha: 0,
          y: 44,
          duration: 0.9,
          ease: "power3.out",
          scrollTrigger: { trigger: ".footer", start: "top 80%" },
        });
      });
    },
    { scope: ref },
  );

  return (
    <div className="landing" ref={ref}>
      <div className="scroll-progress" />
      <nav className="nav">
        <a className="nav-logo" href="#top" aria-label="Xecrex home">
          Xecrex<span>.</span>
        </a>
        <div className="nav-links">
          <a href="#how">Pipeline</a>
          <a href="#/console">Console</a>
          <a href="#limits">Limitations</a>
        </div>
        <a className="nav-cta" href="#/console">
          Open console <span aria-hidden>→</span>
        </a>
      </nav>
      <Hero />
      <Marquee />
      <HowItWorks />
      <section className="console-cta" id="dashboard">
        <div className="container">
          <div className="cta-card">
            <h2 className="section-title">The console</h2>
            <p>
              Forecast map, review queue, coverage health, and the model card —
              scoped to your tenant and role.
            </p>
            <a className="btn btn-dark" href="#/console">
              Open the console <span aria-hidden>→</span>
            </a>
          </div>
        </div>
      </section>
      <footer className="footer" id="limits">
        <div className="container">
          <div className="big">Forecasts, not verdicts.</div>
          <p>
            Aggregate area-level decision support only — never individual assessment,
            identification, or automated enforcement. Every cell is an uncertain
            forecast, not ground truth.
          </p>
          <p className="footer-note">
            Xecrex · human-reviewed urban safety intelligence · hackathon prototype
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
