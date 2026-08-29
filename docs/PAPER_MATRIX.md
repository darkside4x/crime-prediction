# Research Paper Evidence Matrix

No papers were available in the workspace when this file was created. Add PDFs under `papers/`, then fill one row per paper. Do not treat a paper's reported score as transferable unless its geography, label process, horizon, split protocol, and baseline are comparable.

| Citation | Dataset / geography | Spatial unit | Horizon | Target | Features | Model | Split and baselines | Main result | Limitations / leakage risk | Architecture decision |
|---|---|---|---|---|---|---|---|---|---|---|
| _Pending_ | | | | | | | | | | |

The empirical dataset decision and locally reproduced results are tracked in
`docs/EVALUATION.md`. That benchmark is evidence about this implementation, not
a research-paper claim, so it is intentionally not inserted into the paper
matrix as if it were a publication.

## Cross-paper synthesis questions

1. Which gains remain after chronological or spatial holdout rather than random splitting?
2. Is the label an incident, a report, an arrest, or enforcement activity?
3. Were features available at the actual prediction time?
4. Does performance beat a historical-rate or seasonal naive baseline?
5. What grid size and horizon are supported by the dataset density?
6. Are uncertainty, calibration, geographic error slices, and dataset shift reported?
7. Which code, data, and hyperparameters are reproducible?
8. Could the proposed use amplify reporting or enforcement bias?

## Decision log template

| Technique | Decision (`adopt`, `test`, `reject`) | Evidence | Cost | Guardrail / experiment |
|---|---|---|---|---|
| | | | | |
