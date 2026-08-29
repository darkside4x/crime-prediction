# Modeling configuration

`default.json` defines the deterministic forecasting-backend experiment.

Install the combined data/model development environment from the repository root:

```bash
python -m pip install -e ".[dev,model]"
```

Then run with one feature-table manifest per tenant:

```bash
uv run --python 3.12 --with-requirements configs/model/requirements.txt \
  python -m src.models.cli evaluate \
  --config configs/model/default.json \
  --feature-manifest artifacts/tenant=<tenant-id>/features/manifest.json \
  --output-root artifacts
```

The installed `crime-model evaluate` command is equivalent to
`python -m src.models.cli evaluate`.

Repeat `--feature-manifest` for additional tenants. Each input manifest and Parquet file must contain exactly one matching tenant. Models are trained independently and artifacts are written below `tenant=<tenant-id>/models/<model-version>/`.

When explicit split dates are omitted, ordered unique UTC intervals are divided using `train_fraction`, `validation_fraction`, and the remaining untouched test fraction. To freeze dates, set both `explicit_train_end` and `explicit_validation_end` to UTC timestamps. The resolved boundaries are always recorded in `run-manifest.json`.

LightGBM is a required evaluated candidate in the default configuration. Tests may disable it to keep focused unit tests fast; a missing LightGBM installation fails clearly when it is enabled.
