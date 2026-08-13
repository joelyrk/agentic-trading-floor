# Replay evaluation

The evaluator is an offline, point-in-time decision test. It does not place real
orders, call a broker, or demonstrate that any strategy is profitable. The
checked-in v1 dataset is intentionally small and is suitable for testing system
behavior and report reproducibility, not for making investment claims.

Run the complete comparison from the repository root:

```bash
uv run python -m evals.runner
```

This validates fixture hashes and temporal boundaries, runs buy-and-hold, equal
weight, no-trade, seeded random-valid-trade, and momentum baselines, and compares
deterministic single-agent and multi-agent workflow proxies. It writes
`report.json`, `report.md`, and an atomic `checkpoint.json` under
`evals/results/<run-id>/`. Results are ignored by Git because run metadata is
specific to the local checkout. Re-running the same dataset/model/prompt/seed
returns the completed report; an interrupted run resumes by stable
strategy/scenario keys and stable paper-order IDs.

Useful options:

```bash
uv run python -m evals.runner --seed 17 \
  --model recorded-model-name --prompt-version trader-v2
```

## Contracts and boundaries

- `manifest.json` uses schema `1.0`, pins dataset version, scenario count,
  symbols, and SHA-256 hashes for both fixture files.
- `decision_fixtures.json` is the only strategy-visible context. It contains
  cutoff-safe prices, trailing values, and timestamped source signals.
- `outcome_fixtures.json` is loaded into a private mapping and revealed to the
  scorer only after a strategy decision has completed.
- `ScenarioMetrics.timing` proves that research cutoff, decision, and simulated
  execution used the same injected clock; market publication and retrieval must
  precede it.
- `report.json` schema `1.0` stores dataset/version, git SHA, model, prompt,
  configuration, seed, start/completion timestamps, scenario metrics, aggregate
  metrics, and the single-vs-multi ablation.

The single-agent and multi-agent entries in the default credential-free run are
deterministic workflow proxies, not live model invocations. Their latency and
cost fields are fixture estimates so the report pipeline exercises those
metrics. A model-backed adapter can implement the same `Strategy` protocol while
keeping external calls opt-in and recording actual usage. No paid credentials
are needed by the default command or test suite.

## Metrics

For every strategy the report includes compounded total return, benchmark
return and relative return, annualized volatility, annualized Sharpe (zero
risk-free rate), maximum drawdown, average gross allocation turnover, win rate,
decision validity, citation validity, tool success rate, average latency, and
model/API cost. These metrics describe this fixture only.
