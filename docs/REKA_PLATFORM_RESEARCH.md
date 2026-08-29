# Reka Platform Research and Architecture Decision

Research date: 2026-08-29

## Conclusion

Use Reka for managed video understanding and grounded analyst assistance, but keep future aggregate incident forecasting in the reproducible local model service. Reka output is an unconfirmed proposal until schema validation and human review. Do not build facial recognition, known-offender matching, person tracking, guilt inference, or individual risk scores.

The hackathon vertical slice should be recorded MP4 first. A tenant administrator uploads an approved recording through FastAPI; a background worker submits it to Reka Vision, waits for indexing, asks a versioned safety-incident question, validates candidate timestamps/categories, and presents candidates to a reviewer. Only confirmed candidates enter aggregate incident history. A separate model then forecasts the next H3-cell/time/category window.

## What the current Reka APIs provide

Reka exposes two relevant surfaces:

| Surface | Useful capability | Product role |
|---|---|---|
| Chat API | OpenAI-compatible chat completions, multimodal messages, streaming, model discovery, and function calling | Redacted source mapping and explanations of already-computed aggregate facts |
| Vision API | Managed video/image upload, indexing, retrieval, deletion, semantic search, video Q&A, tags, transcripts/captions/scenes, object segmentation, and clips | Candidate safety-incident proposal from approved recordings |
| Research model | Web research with citations and JSON-Schema structured output | Optional evidence collection for public policy/dataset research, never live risk calculation |

Official references:

- Chat API: <https://docs.reka.ai/chat/api-reference/create>
- Models and runtime discovery: <https://docs.reka.ai/chat/models>
- Function calling: <https://docs.reka.ai/chat/function-calling>
- Vision overview: <https://docs.reka.ai/vision/overview>
- Video upload: <https://docs.reka.ai/vision/api-reference/video-management/upload>
- Video Q&A: <https://docs.reka.ai/vision/video-qa>
- Video search: <https://docs.reka.ai/vision/video-search>
- Image management/search: <https://docs.reka.ai/vision/image-management>
- Structured Research output: <https://docs.reka.ai/research/structured-output>
- Vision limits: <https://docs.reka.ai/vision/rate-limits>
- Vision pricing and retention: <https://docs.reka.ai/vision/pricing>
- Privacy policy: <https://reka.ai/privacy-policy>

Model availability is account-dependent. Discover it through `GET /v1/models` at startup or in an operator diagnostic rather than assuming every documented or newly released model is enabled.

## Important operating constraints

As documented on the research date, the self-service Vision tier includes 180 free indexed-video minutes, limits video uploads and searches to 50 requests each per rolling 24 hours, and automatically deletes stored video after 30 days. Published developer pricing includes video indexing at USD 0.05 per input minute, search at USD 0.005 per request, and Q&A/tagging output at USD 2 per million output tokens. These numbers can change and must be checked before the demo.

The privacy policy says free or promotional API interactions may be used to improve models, while paid API content is not used for training unless the account opts in. Consequently, do not send real public CCTV or other personal data on promotional credits. The production prerequisite is documented lawful authority, signage/consent as applicable, a paid or enterprise data-processing agreement, regional/retention review, and a tested remote deletion workflow.

The upload API exposes options such as person indexing and persisted frames. This project must leave person indexing off and avoid persisted frames unless a narrowly approved evidence workflow requires them. Presigned Reka URLs and opaque Reka video IDs stay in the restricted backend mapping and never cross into general browser responses.

## Target architecture

```text
Browser (admin/reviewer/viewer)
  | TLS, bearer session, no tenant_id or Reka key
  v
FastAPI boundary (Person 2)
  |-- authentication provider -> server-derived tenant context + role
  |-- idempotency + typed errors + audit + request IDs
  |-- bounded source/video/review/forecast endpoints
  |-- operational ForecastService -> approved tenant bundle or historical fallback
  |-- Reka Chat gateway -> allowlisted aggregate facts only
  |
  +--> queue (tenant-scoped job envelope)
          |
          v
      video worker (Person 1)
        |-- validate consent/type/size/checksum/duration/quota
        |-- Reka Vision upload -> index-status polling -> Q&A/tagging/search
        |-- strict candidate schema + prompt-injection/prohibited-field filter
        |-- retention delete + monitored retry/dead letter
        v
      restricted Postgres/object metadata
        | candidate -> immutable human review -> confirmed event
        v
      H3/time/category aggregation + measured coverage
        v
      labelled historical rows / unlabelled future feature snapshots
        v
      chronological model selection, final refit, calibration, temporal uncertainty
        v
      schema-valid aggregate forecasts -> map
```

### AWS hackathon deployment

For the hackathon, use one small EC2 instance for the Docker Compose API, worker, and web containers; use RDS PostgreSQL, a private S3 bucket for transient uploads, SQS for jobs, Secrets Manager for `REKA_API_KEY`, and CloudWatch for logs/metrics. Put CloudFront or an Application Load Balancer with TLS in front. Restrict the instance role to tenant-prefixed S3 objects and the one secret. Use outbound HTTPS only for Reka and package/update endpoints.

For production, split API and workers into ECS/Fargate services across private subnets, use RDS row-level security, separate dead-letter queues, KMS keys, WAF/rate limits, VPC endpoints, backup/restore exercises, and per-tenant budgets. An EC2-only database or queue is acceptable for a demo but is not a production isolation or durability design.

## Reka integration rules

1. One server-only `REKA_API_KEY` configures both Reka gateways. Never use a `VITE_` variable or direct browser-to-Reka call.
2. Send video only after tenant ownership, lawful-use, retention, checksum, media-type, duration, and quota checks.
3. Group and map every Reka asset to exactly one tenant locally. Never use a caller-supplied Reka ID as authorization.
4. Keep request timeouts bounded. Retry only safe/idempotent operations with jitter and `Retry-After`; use a circuit breaker for sustained 429/5xx failures.
5. Treat captions, transcripts, visible text, user questions, and model output as untrusted. They cannot override system policy or call arbitrary tools.
6. Validate all generated JSON locally even when structured output is requested. Allowlist fact IDs, categories, timestamp bounds, and tools.
7. Cache only by tenant, model, prompt, asset/snapshot version, and normalized request. Never share a cross-tenant semantic cache.
8. Record latency, quota headers, safe error code, prompt/model versions, candidate counts, reviewer outcome, and deletion completion without logging footage, prompts, secrets, coordinates, or presigned URLs.
9. Reka downtime degrades video analysis to manual review and explanations to deterministic facts; it never blocks access to already-produced forecasts.

## Evaluation plan

The platform needs three different evaluations:

- Video proposal evaluation: timestamp overlap, category precision/recall, reviewer acceptance, false-positive slices, latency, and cost on a lawful labelled clip set. Reka confidence is not crime probability.
- Forecast evaluation: chronological and rolling-origin MAE, Poisson deviance, top-k capture, Brier score, calibration, temporal intervals, and geography/category/coverage slices against historical-rate and previous-period baselines.
- Generative safety evaluation: schema validity, fact-citation coverage, hallucination rate, prompt-injection resistance, prohibited identity/enforcement refusals, cross-tenant denial, secret leakage, timeouts, and deterministic fallback.

Do not publish an accuracy claim from synthetic fixtures. A real claim requires a licensed dataset, documented label process, reproducible point-in-time features, and an untouched chronological test block.

## Recommended functionality

### Build for the hackathon

- recorded-video upload, indexing status, and candidate review;
- timestamped candidate highlights with expiring reviewer-only access;
- H3 forecast map with expected count, occurrence probability, separate intervals, coverage, freshness, and suppression;
- tenant/role isolation, audit trail, idempotent mutations, and typed degraded states;
- grounded Reka explanations that cite supplied aggregate fact IDs;
- model card and a “why this is not individual crime prediction” limitations panel;
- source coverage/health dashboard and Reka quota/cost meter.

### Valuable follow-ups

- live RTSP/ONVIF edge segmentation after the recorded slice works;
- multilingual reviewer summaries and accessibility-friendly incident descriptions;
- semantic search across a tenant's approved recordings for non-identifying event types;
- configurable safety taxonomies, reviewer disagreement/adjudication, and feedback-quality monitoring;
- drift, calibration, coverage, latency, deletion, and fairness alerts;
- counterfactual data-quality views such as “forecast unavailable if coverage falls below threshold,” without enforcement recommendations;
- secure source-schema onboarding where Reka proposes mappings from redacted column profiles and a human approves them;
- after-action aggregate trend reports and exportable audit/model-card packets;
- synthetic scenario generation for demos and failure drills, clearly labelled synthetic.

### Explicitly reject

- face recognition or matching known offenders against camera feeds;
- persistent person re-identification, identity watchlists, license-plate tracking, or protected-attribute inference;
- prediction of who will offend, guilt/intent classification, victim-address maps, or automated patrol/arrest/resource-allocation recommendations;
- sending raw incident databases, exact coordinates, identities, secrets, or cross-tenant context to Reka.

These rejected features create severe false-match, due-process, privacy, and bias risks and violate the repository's frozen product contract. The useful and defensible product is aggregate incident detection support plus calibrated area/time forecasting with human oversight.
