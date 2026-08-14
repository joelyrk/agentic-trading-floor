# Evaluation method and published result

The evaluation is a deterministic test of temporal controls, accounting,
metrics, retries, and report generation. It is not evidence that the agents or
any baseline will be profitable in real markets.

## Reproduce

From a locked Python 3.12 environment:

```bash
uv sync --locked
uv run python -m evals.runner \
  --dataset evals/datasets/historical-v1 \
  --output evals/results \
  --seed 7 \
  --model offline-deterministic-proxy \
  --prompt-version eval-agent-v1
```

The manifest verifies SHA-256 hashes before use. Decision-time prices, trailing
returns, and timestamped source signals live in `decision_fixtures.json`.
Outcomes live in a separate file and are unavailable to a strategy until its
decision record is complete. The evaluator writes `report.json`, `report.md`,
and an atomic retry checkpoint under a stable run ID.

## Published fixture result

The checked-in [human-readable report](evals/published/historical-v1/report.md)
and [machine-readable report](evals/published/historical-v1/report.json) use:

- dataset `historical-v1` version `1.0.0`, 30 scenarios;
- model label `offline-deterministic-proxy`;
- prompt version `eval-agent-v1`;
- seed `7`; and
- seven strategies: five baselines plus single-agent and multi-agent workflow
  proxies.

In that fixture, multi-agent total return was `1.05%` versus `2.76%` for the
single-agent proxy and `-3.06%` for buy-and-hold. Multi-agent incurred higher
fixture-estimated latency and cost. These values describe only the small
synthetic/derived replay fixture; they are included to make comparison and
reporting inspectable, not to rank deployable strategies.

## Metrics

The report contains compounded return, benchmark-relative return, annualized
volatility and Sharpe at zero risk-free rate, maximum drawdown, gross allocation
turnover, win rate, decision/citation validity, tool success, latency, and
model/API cost. The deterministic proxies use fixture estimates for model cost
and latency so those reporting paths are exercised without provider calls.

## Limitations

- Thirty scenarios are too few for strategy inference and are not a
  representative market sample.
- Decision signals and workflow proxies are simplified; they are not recorded
  live agent outputs.
- Prices omit many real execution effects, corporate actions, taxes, borrow,
  liquidity, and market impact.
- The baseline comparison is not statistically powered and has no confidence
  intervals or out-of-sample validation.
- The checked-in report records `git_sha=local-verification`; a local rerun
  records the current checkout SHA. Compare dataset hashes, versions,
  configuration, and generated metrics when verifying it.

See [evals/README.md](evals/README.md) for schema details and metric definitions.
