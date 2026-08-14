from types import SimpleNamespace

import backend.strategies as strategies


def test_default_strategies_fill_blanks_and_preserve_customization(monkeypatch) -> None:
    accounts = {
        name: SimpleNamespace(strategy="Custom mandate" if name == "ray" else "")
        for name in strategies.DEFAULT_STRATEGIES
    }
    saved = []
    for name, account in accounts.items():
        account.save = lambda name=name: saved.append(name)

    monkeypatch.setattr(
        strategies.Account,
        "get",
        lambda name: accounts[name],
    )
    initialized = strategies.ensure_default_strategies()

    assert initialized == ["warren", "george", "cathie"]
    assert saved == initialized
    assert accounts["ray"].strategy == "Custom mandate"
    assert all(accounts[name].strategy for name in strategies.DEFAULT_STRATEGIES)
