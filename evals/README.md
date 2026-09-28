# PRISM RCA evaluation

The benchmark separates **inferred RCA** from **confirmed ground truth**.

## Metrics

- top-1 RCA accuracy
- top-3 hypothesis recall
- abstention accuracy (`undetermined` when evidence is insufficient)
- evidence attribution coverage
- contradiction coverage
- unsupported-claim rate
- investigation latency
- agent timeout/error rate
- degraded dependency rate

Do not optimize for raw confidence. PRISM's support score is not a calibrated
probability unless a calibration study has established that property.

## Running

Set `PRISM_EVAL_BASE_URL` to a running PRISM instance and run:

```bash
python evals/run.py
```

The runner sends each scenario with a unique idempotency key and records the
raw API response. It does not write the scenario's ground truth into PRISM.
Ground truth is used only by the evaluator.
