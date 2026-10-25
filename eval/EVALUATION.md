# Genesis local evaluation harness

Business evaluators and datasets live in eval/canonical. No external tracing backend, exporter, collector credential or hosted dataset upload is used.

Run `python scripts/run_canonical_experiments.py --dry-run` to validate coverage. Real runs require provider credentials and write measured responses, verdicts, costs and execution receipts to the selected local output directory. Safety validators and the independent semantic judge remain mandatory.

Run `python -m pytest tests eval/tests` for regressions. Historical proof artifacts retain their original claims; they do not certify a new revision.
