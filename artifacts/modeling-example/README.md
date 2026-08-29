# Modeling artifacts

The evaluation command writes one isolated directory per tenant and selected model:

```text
artifacts/
  tenant=<tenant-id>/
    models/<model-version>/
      bundle.json
      parameters.json or model.txt
      predictions.parquet
      evaluation.json
      model-card.json
      reka-facts.json
      run-manifest.json
```

Large or data-derived artifacts are intentionally not committed. The synthetic payloads under `contracts/fixtures/` demonstrate every artifact contract without presenting illustrative fixture metrics as real performance. A real evaluation/model card should be frozen here only after Person 1 supplies a checksum-verified tenant feature manifest.
