import { lazy, Suspense, useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import Hero from "./components/Hero";
import Marquee from "./components/Marquee";
import HowItWorks from "./components/HowItWorks";
import CircularGallery from "./components/bits/CircularGallery";
import CardSwap, { Card } from "./components/bits/CardSwap";
import shotLive from "./assets/screens/live.webp";
import shotReview from "./assets/screens/review.webp";
import shotMap from "./assets/screens/map.webp";
import shotSystem from "./assets/screens/system.webp";
import shotModel from "./assets/screens/model.webp";
import shotSignin from "./assets/screens/signin.webp";
import { AuthProvider } from "./console/AuthContext";
import { useHashRoute } from "./console/router";

gsap.registerPlugin(useGSAP, ScrollTrigger);

const ConsoleShell = lazy(() => import("./console/ConsoleShell"));

const SCREENS = [
  { image: shotLive, text: "Live monitor" },
  { image: shotReview, text: "Review queue" },
  { image: shotMap, text: "Prediction map" },
  { image: shotSystem, text: "System status" },
  { image: shotModel, text: "Model card" },
  { image: shotSignin, text: "Secure sign-in" },
];

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
      <section className="gallery-section" id="screens">
        <div className="container">
          <p className="eyebrow">Inside the console</p>
          <h2 className="section-title">
            Real screens. <span className="accent">Real contract.</span>
          </h2>
        </div>
        <div className="gallery-wrap">
          <CircularGallery
            items={SCREENS}
            bend={2.2}
            textColor="#fff1ea"
            borderRadius={0.06}
            font="400 24px Urbanist"
            fontUrl="https://fonts.googleapis.com/css2?family=Urbanist:wght@400;700&display=swap"
            scrollSpeed={1.6}
            scrollEase={0.06}
          />
        </div>
      </section>
      <section className="console-cta" id="dashboard">
        <div className="container">
          <div className="cta-card">
            <div className="cta-copy">
              <h2 className="section-title">The console</h2>
              <p>
                Forecast map, review queue, coverage health, and the model card —
                scoped to your tenant and role.
              </p>
              <a className="btn btn-dark" href="#/console">
                Open the console <span aria-hidden>→</span>
              </a>
            </div>
            <CardSwap width={380} height={250} cardDistance={48} verticalDistance={56} delay={4200} skewAmount={4} pauseOnHover easing="elastic">
              <Card>
                <span className="swap-tag">Stage 01</span>
                <h3>Live capture</h3>
                <p>Bounded 12-second clips through Reka Vision.</p>
              </Card>
              <Card>
                <span className="swap-tag">Stage 02</span>
                <h3>Human review</h3>
                <p>Immutable confirm-or-reject decisions.</p>
              </Card>
              <Card>
                <span className="swap-tag">Stage 03</span>
                <h3>Published forecast</h3>
                <p>Atomic tenant-scoped six-hour windows.</p>
              </Card>
            </CardSwap>
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
