from .accounts import Account
from .strategies import DEFAULT_STRATEGIES

warren_strategy = DEFAULT_STRATEGIES["warren"]
george_strategy = DEFAULT_STRATEGIES["george"]
ray_strategy = DEFAULT_STRATEGIES["ray"]
cathie_strategy = DEFAULT_STRATEGIES["cathie"]


def reset_traders():
    Account.get("Warren").reset(warren_strategy)
    Account.get("George").reset(george_strategy)
    Account.get("Ray").reset(ray_strategy)
    Account.get("Cathie").reset(cathie_strategy)


if __name__ == "__main__":
    reset_traders()
