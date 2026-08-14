# Paper-trading evaluation report

> Offline replay only. These results do not validate an investment strategy and are not real trades.

Run `8cce06c6-209e-5509-9e78-daded052f901` used dataset `historical-v1` version `1.0.0`, seed `7`, model `offline-deterministic-proxy`, prompt `eval-agent-v1`, and git SHA `local-verification`.

| Strategy | Total return | Relative | Volatility | Sharpe | Max drawdown | Turnover | Win rate | Valid decisions | Valid citations | Tool success | Cost (USD) | Latency (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| buy_and_hold | -3.06% | 0.00% | 18.10% | -1.35 | 8.79% | 100.00% | 50.00% | 100.00% | 100.00% | 100.00% | 0.0000 | 0 |
| equal_weight | -2.38% | 0.68% | 17.68% | -1.06 | 8.04% | 100.00% | 50.00% | 100.00% | 100.00% | 100.00% | 0.0000 | 0 |
| no_trade | 0.00% | 3.06% | 0.00% | n/a | 0.00% | 0.00% | 0.00% | 100.00% | 100.00% | 100.00% | 0.0000 | 0 |
| random_valid_trades | -5.87% | -2.82% | 19.72% | -2.48 | 9.72% | 86.67% | 36.67% | 100.00% | 100.00% | 100.00% | 0.0000 | 0 |
| momentum | -1.33% | 1.73% | 18.65% | -0.51 | 9.18% | 80.00% | 30.00% | 100.00% | 100.00% | 100.00% | 0.0000 | 0 |
| single_agent | 2.76% | 5.82% | 14.62% | 1.64 | 3.14% | 56.67% | 26.67% | 100.00% | 100.00% | 100.00% | 0.0300 | 120 |
| multi_agent | 1.05% | 4.11% | 17.09% | 0.60 | 5.88% | 80.00% | 40.00% | 100.00% | 100.00% | 100.00% | 0.0750 | 260 |

## Ablation

Multi-agent minus single-agent total return: -1.71%. Incremental fixture-estimated cost: $0.0450; incremental latency: 140 ms.

All fixture hashes and point-in-time checks passed. Outcome records were withheld until each decision completed.
