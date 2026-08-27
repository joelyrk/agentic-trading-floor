import json
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from pydantic import BaseModel

from .database import (
    read_account,
    write_account,
    write_log,
    write_market_observation,
)
from .market import MarketObservation, get_market_observation

load_dotenv()

INITIAL_BALANCE = 10_000.0
SPREAD = 0.002


class Transaction(BaseModel):
    symbol: str
    quantity: int
    price: float
    timestamp: str
    rationale: str
    market_observation_id: str | None = None
    market_observation: MarketObservation | None = None

    def total(self) -> float:
        return self.quantity * self.price

    def __repr__(self):
        return f"{abs(self.quantity)} shares of {self.symbol} at {self.price} each."


class Account(BaseModel):
    name: str
    balance: float
    strategy: str
    holdings: dict[str, int]
    transactions: list[Transaction]
    portfolio_value_time_series: list[tuple[str, float]]

    @classmethod
    def get(cls, name: str):
        fields = read_account(name.lower())
        if not fields:
            fields = {
                "name": name.lower(),
                "balance": INITIAL_BALANCE,
                "strategy": "",
                "holdings": {},
                "transactions": [],
                "portfolio_value_time_series": [],
            }
            write_account(name, fields)
        return cls(**fields)

    def save(self):
        write_account(self.name.lower(), self.model_dump(mode="json"))

    def reset(self, strategy: str = ""):
        self.balance = INITIAL_BALANCE
        self.strategy = strategy
        self.holdings = {}
        self.transactions = []
        self.portfolio_value_time_series = []
        self.save()

    def deposit(self, amount: float):
        """Deposit funds into the account."""
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")
        self.balance += amount
        print(f"Deposited ${amount}. New balance: ${self.balance}")
        self.save()

    def withdraw(self, amount: float):
        """Withdraw funds from the account, ensuring it doesn't go negative."""
        if amount > self.balance:
            raise ValueError("Insufficient funds for withdrawal.")
        self.balance -= amount
        print(f"Withdrew ${amount}. New balance: ${self.balance}")
        self.save()

    def buy_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """Buy shares of a stock if sufficient funds are available."""
        observation = get_market_observation(symbol)
        price = float(observation.price)
        buy_price = price * (1 + SPREAD)
        total_cost = buy_price * quantity

        if total_cost > self.balance:
            raise ValueError("Insufficient funds to buy shares.")
        elif price == 0:
            raise ValueError(f"Unrecognized symbol {symbol}")

        # Update holdings
        self.holdings[observation.symbol] = self.holdings.get(observation.symbol, 0) + quantity
        timestamp = datetime.now(timezone.utc).isoformat()
        order_id = str(uuid4())
        observation_id = write_market_observation(self.name, "order", order_id, observation)
        # Record transaction
        transaction = Transaction(
            symbol=observation.symbol,
            quantity=quantity,
            price=buy_price,
            timestamp=timestamp,
            rationale=rationale,
            market_observation_id=observation_id,
            market_observation=observation,
        )
        self.transactions.append(transaction)

        # Update balance
        self.balance -= total_cost
        self.save()
        write_log(self.name, "account", f"Bought {quantity} of {symbol}")
        return "Completed. Latest details:\n" + self.report()

    def sell_shares(self, symbol: str, quantity: int, rationale: str) -> str:
        """Sell shares of a stock if the user has enough shares."""
        normalized_symbol = symbol.strip().upper()
        if self.holdings.get(normalized_symbol, 0) < quantity:
            raise ValueError(
                f"Cannot sell {quantity} shares of {normalized_symbol}. Not enough shares held."
            )

        observation = get_market_observation(normalized_symbol)
        price = float(observation.price)
        sell_price = price * (1 - SPREAD)
        total_proceeds = sell_price * quantity

        # Update holdings
        self.holdings[normalized_symbol] -= quantity

        # If shares are completely sold, remove from holdings
        if self.holdings[normalized_symbol] == 0:
            del self.holdings[normalized_symbol]
        timestamp = datetime.now(timezone.utc).isoformat()
        order_id = str(uuid4())
        observation_id = write_market_observation(self.name, "order", order_id, observation)
        # Record transaction
        transaction = Transaction(
            symbol=observation.symbol,
            quantity=-quantity,
            price=sell_price,
            timestamp=timestamp,
            rationale=rationale,
            market_observation_id=observation_id,
            market_observation=observation,
        )
        self.transactions.append(transaction)

        # Update balance
        self.balance += total_proceeds
        self.save()
        write_log(self.name, "account", f"Sold {quantity} of {symbol}")
        return "Completed. Latest details:\n" + self.report()

    def calculate_portfolio_value(self):
        """Calculate the total value of the user's portfolio."""
        total_value = self.balance
        valuation_id = str(uuid4())
        for symbol, quantity in self.holdings.items():
            observation = get_market_observation(symbol)
            write_market_observation(self.name, "valuation", valuation_id, observation)
            total_value += float(observation.price) * quantity
        return total_value

    def calculate_profit_loss(self, portfolio_value: float):
        """Calculate profit or loss from the initial spend."""
        initial_spend = sum(transaction.total() for transaction in self.transactions)
        return portfolio_value - initial_spend - self.balance

    def get_holdings(self):
        """Report the current holdings of the user."""
        return self.holdings

    def get_profit_loss(self):
        """Report the user's profit or loss at any point in time."""
        return self.calculate_profit_loss()

    def list_transactions(self):
        """List all transactions made by the user."""
        return [transaction.model_dump() for transaction in self.transactions]

    def snapshot(self) -> str:
        """Return stored paper-account state without performing a market valuation."""
        return self.model_dump_json()

    def report(self) -> str:
        """Return a json string representing the account."""
        portfolio_value = self.calculate_portfolio_value()
        self.portfolio_value_time_series.append(
            (datetime.now(timezone.utc).isoformat(), portfolio_value)
        )
        self.save()
        pnl = self.calculate_profit_loss(portfolio_value)
        data = self.model_dump(mode="json")
        data["total_portfolio_value"] = portfolio_value
        data["total_profit_loss"] = pnl
        write_log(self.name, "account", "Retrieved account details")
        return json.dumps(data)

    def get_strategy(self) -> str:
        """Return the strategy of the account"""
        write_log(self.name, "account", "Retrieved strategy")
        return self.strategy

    def change_strategy(self, strategy: str) -> str:
        """At your discretion, if you choose to, call this to change your investment strategy for the future"""
        self.strategy = strategy
        self.save()
        write_log(self.name, "account", "Changed strategy")
        return "Changed strategy"
