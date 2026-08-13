# Historical fixture v1

This immutable fixture contains 30 one-session scenarios using adjusted daily
closes for AAPL, MSFT, NVDA, and XOM from the public
[`pmoe7/Stock-Market-Data`](https://github.com/pmoe7/Stock-Market-Data)
dataset (repository snapshot read on 2026-08-13). The source repository says its
prices were scraped or aggregated and permits personal use; this fixture should
therefore be treated as a demonstrative evaluation dataset, not a redistributable
market-data product or evidence of strategy quality.

`SPX` is an explicitly derived equal-weight proxy over those four securities; it
is not an observation of the S&P 500 index. Each decision record includes only
the current adjusted close, a five-session trailing return, and a derived trend
signal available at the cutoff. The following session's prices live only in the
separate outcome file.

The manifest pins SHA-256 hashes of both files. To rebuild from a local copy of
the source CSV:

```bash
uv run python -m evals.build_dataset \
  '/path/to/sp500_daily_stock_prices(10 years).csv' \
  evals/datasets/historical-v1
```
