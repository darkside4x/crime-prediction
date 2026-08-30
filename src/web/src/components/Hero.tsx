import { useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";

gsap.registerPlugin(useGSAP, ScrollTrigger);

export default function Hero() {
  const ref = useRef<HTMLElement>(null);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();

      mm.add("(prefers-reduced-motion: no-preference)", () => {
        gsap
          .timeline({ defaults: { ease: "power4.out" } })
          .from(".hero-title .line", {
            yPercent: 115,
            duration: 0.9,
            stagger: 0.12,
          })
          .from(".hero-cta-row", { autoAlpha: 0, y: 20, duration: 0.6 }, "-=0.45");

        gsap.to(".hero-inner", {
          yPercent: -14,
          autoAlpha: 0,
          ease: "none",
          scrollTrigger: { trigger: ref.current, start: "top top", end: "75% top", scrub: 0.4 },
        });

        gsap.to(".hero-scroll-hint", {
          y: 8,
          repeat: -1,
          yoyo: true,
          duration: 0.9,
          ease: "sine.inOut",
        });
      });
    },
    { scope: ref },
  );

  return (
    <section className="hero" id="top" ref={ref}>
      <div className="hero-grid" />

      <div className="container hero-inner">
        <p className="eyebrow">Aggregate risk intelligence · Human in the loop</p>
        <h1 className="hero-title" aria-label="Xecrex — forecasts.">
          <span className="line-mask" aria-hidden="true">
            <span className="line brand">Xecrex</span>
          </span>
          <span className="line-mask" aria-hidden="true">
            <span className="line outline-text">Forecasts.</span>
          </span>
        </h1>
        <div className="hero-cta-row">
          <a className="btn btn-red" href="#dashboard">
            Open the map <span aria-hidden>→</span>
          </a>
        </div>
      </div>

      <div className="hero-scroll-hint">Scroll ↓</div>
    </section>
  );
}
