import { motion } from "motion/react";

const STEPS = [
  {
    num: "01",
    title: "Replay ingestion",
    body: "Recorded events stream through the same versioned envelope future live adapters use — idempotent, checkpointed, quarantined on failure, and strictly tenant-scoped.",
  },
  {
    num: "02",
    title: "Privacy-first features",
    body: "Raw coordinates never leave the ingestion boundary. Events aggregate to H3 cells, and every feature at time t uses only information available before t.",
  },
  {
    num: "03",
    title: "Honest models",
    body: "A historical-rate baseline is always in the race. Candidates win only by beating it on an untouched, chronological test window — or the baseline ships.",
  },
  {
    num: "04",
    title: "Grounded explanations",
    body: "The AI copilot answers only from published aggregate facts with citations. It never generates, adjusts, or overrides a numeric risk score.",
  },
];

const cardVariants = {
  hidden: { opacity: 0, y: 48 },
  visible: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { duration: 0.6, delay: i * 0.1, ease: "easeOut" as const },
  }),
};

export default function HowItWorks() {
  return (
    <section id="how" style={{ padding: "96px 0" }}>
      <div className="container">
        <motion.p
          className="eyebrow"
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true, amount: 0.6 }}
        >
          Pipeline
        </motion.p>
        <motion.h2
          className="section-title"
          initial={{ opacity: 0, y: 40 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, amount: 0.4 }}
          transition={{ duration: 0.7 }}
        >
          FROM EVENTS TO <span className="accent">EVIDENCE</span>
        </motion.h2>
        <div className="cards">
          {STEPS.map((step, i) => (
            <motion.div
              key={step.num}
              className="card"
              custom={i}
              initial="hidden"
              whileInView="visible"
              viewport={{ once: true, amount: 0.3 }}
              variants={cardVariants}
              whileHover={{ y: -6 }}
            >
              <span className="num">{step.num}</span>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}
