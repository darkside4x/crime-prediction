import { useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import { ScrollTrigger } from "gsap/ScrollTrigger";

gsap.registerPlugin(useGSAP, ScrollTrigger);

const STEPS = [
  {
    index: "01",
    title: "Replay ingestion",
    body: "Versioned, idempotent, tenant-scoped intake.",
  },
  {
    index: "02",
    title: "Privacy-first features",
    body: "Raw coordinates never leave the boundary.",
  },
  {
    index: "03",
    title: "Honest models",
    body: "Only what beats the baseline ships.",
  },
  {
    index: "04",
    title: "Grounded explanations",
    body: "Citations only. Never new numbers.",
  },
];

export default function HowItWorks() {
  const sectionRef = useRef<HTMLElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const trackRef = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();

      mm.add(
        {
          desktop: "(min-width: 881px) and (prefers-reduced-motion: no-preference)",
          motionOk: "(prefers-reduced-motion: no-preference)",
        },
        (ctx) => {
          const track = trackRef.current;
          const wrap = wrapRef.current;
          if (!track || !wrap) return;

          if (ctx.conditions?.motionOk) {
            gsap.from(".how-head > *", {
              autoAlpha: 0,
              y: 36,
              duration: 0.8,
              stagger: 0.12,
              ease: "power3.out",
              scrollTrigger: { trigger: ".how-head", start: "top 78%" },
            });
          }

          if (!ctx.conditions?.desktop) {
            // stacked layout: simple vertical reveals
            gsap.utils.toArray<HTMLElement>(".step-panel").forEach((panel) => {
              gsap.from(panel.querySelectorAll(".step-tag, h3, p"), {
                autoAlpha: 0,
                y: 30,
                duration: 0.7,
                stagger: 0.08,
                ease: "power3.out",
                scrollTrigger: { trigger: panel, start: "top 82%" },
              });
            });
            return;
          }

          const distance = () => track.scrollWidth - wrap.clientWidth;
          const scrollTween = gsap.to(track, {
            x: () => -distance(),
            ease: "none",
            scrollTrigger: {
              trigger: wrap,
              start: "top top",
              end: () => `+=${distance()}`,
              pin: true,
              scrub: 0.6,
              invalidateOnRefresh: true,
            },
          });

          gsap.utils.toArray<HTMLElement>(".step-panel").forEach((panel) => {
            gsap.from(panel.querySelectorAll(".step-tag, h3, p"), {
              autoAlpha: 0,
              y: 30,
              duration: 0.7,
              stagger: 0.08,
              ease: "power3.out",
              scrollTrigger: {
                trigger: panel,
                containerAnimation: scrollTween,
                start: "left 88%",
              },
            });
          });
        },
      );
    },
    { scope: sectionRef },
  );

  return (
    <section id="how" className="how" ref={sectionRef}>
      <div className="how-head container">
        <p className="eyebrow">How it works</p>
        <h2 className="section-title">
          From events to <span className="accent">evidence</span>
        </h2>
      </div>
      <div className="how-track-wrap" ref={wrapRef}>
        <div className="how-track" ref={trackRef}>
          {STEPS.map((step) => (
            <article className="step-panel" key={step.index}>
              <span className="step-index" aria-hidden="true">{step.index}</span>
              <span className="step-tag">{`Step ${step.index} / 04`}</span>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
