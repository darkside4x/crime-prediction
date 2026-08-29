import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useMutation } from "@tanstack/react-query";
import { api, type CopilotInsight } from "../api";

export default function Copilot({ token }: { token: string }) {
  const [question, setQuestion] = useState("");
  const [insight, setInsight] = useState<CopilotInsight | null>(null);

  const mutation = useMutation({
    mutationFn: (q: string) => api.copilot(token, q),
    onSuccess: setInsight,
  });

  const refused = insight && insight.refusal_code !== "not_applicable";

  return (
    <div className="panel">
      <h4>Analyst copilot · grounded</h4>
      <p className="hint">
        Ask aggregate questions. Answers cite published facts and show data
        freshness. The AI never computes risk.
      </p>
      <form
        className="copilot-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (question.trim()) mutation.mutate(question.trim());
        }}
      >
        <input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="How did the model do on the test window?"
          aria-label="Copilot question"
        />
        <motion.button className="chip active" whileTap={{ scale: 0.94 }} disabled={mutation.isPending}>
          {mutation.isPending ? "…" : "ASK"}
        </motion.button>
      </form>
      <AnimatePresence>
        {insight && (
          <motion.div
            key={insight.request_id}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            style={{ marginTop: 12 }}
          >
            <div className={`copilot-answer${refused ? " refusal" : ""}`}>{insight.answer}</div>
            {insight.claims.map((claim, i) => (
              <p className="citation" key={i}>
                ◆ {claim.text} [{claim.fact_ids.join(", ")}]
              </p>
            ))}
            <p className="citation">
              model {insight.model_version} · data as of {insight.data_as_of} ·{" "}
              {insight.reka_model} · {insight.refusal_code === "not_applicable" ? "grounded" : `refused: ${insight.refusal_code}`}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
      {mutation.isError && (
        <p className="hint" style={{ marginTop: 10 }}>
          AI explanation unavailable — the deterministic map and model card still work.
        </p>
      )}
    </div>
  );
}
