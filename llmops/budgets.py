"""
Monthly budgets per team. Before a request goes to a provider, the gateway
adds up what the team has spent since the start of the month (UTC) and either
refuses the request or lets it through with a warning header.

The check happens before the call, because the cost is only known after it.
So a team can go over its budget by about one request, never more.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_usd: float = Field(gt=0)
    # block: requests fail with 429 once the budget is used up; warn: they go through, with a header
    on_exceed: Literal["block", "warn"] = "block"
    # share of the budget after which responses carry a warning, so teams hear before they're cut off
    warn_at: float = Field(default=0.8, gt=0, le=1)


class BudgetStatus(BaseModel):
    budget_usd: float
    spent_usd: float
    exceeded: bool
    warning: str | None = None

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def check(budget: Budget, spent: float) -> BudgetStatus:
    exceeded = spent >= budget.monthly_usd
    warning = None
    if exceeded:
        warning = f"monthly budget of ${budget.monthly_usd:.2f} used up (${spent:.2f} spent)"
    elif spent >= budget.warn_at * budget.monthly_usd:
        warning = f"{spent / budget.monthly_usd:.0%} of the monthly budget of ${budget.monthly_usd:.2f} used"
    return BudgetStatus(budget_usd=budget.monthly_usd, spent_usd=spent, exceeded=exceeded, warning=warning)
