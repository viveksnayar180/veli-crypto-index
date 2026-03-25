"""
Fee engine — five fee types, all deduct from actual portfolio value (units held).

  1. Entry fee:        % of initial investment charged once at strategy start
  2. Exit fee:         % of final portfolio value charged once at strategy end
  3. AUM fee:          1% p.a. charged weekly (1%/52)
  4. Rebalancing fee:  0.3% on rebalanced amount (at each rebalance)
  5. Performance fee:  15% on weekly gains (crystallised every period)

Performance fee uses weekly crystallisation: charged every week the portfolio
is above its value at the *previous* week's close — regardless of all-time
high. The period watermark resets each week (up or down) so fees apply to any
positive weekly return. The reported high_watermark is still the all-time high
for display purposes.
"""

AUM_FEE_ANNUAL       = 0.01
AUM_FEE_WEEKLY       = AUM_FEE_ANNUAL / 52
REBALANCING_FEE_RATE = 0.003
PERFORMANCE_FEE_RATE = 0.15


class FeeEngine:
    def __init__(
        self,
        entry_fee:          float = 0.0,
        exit_fee:           float = 0.0,
        aum_fee_weekly:     float = AUM_FEE_WEEKLY,
        rebalance_fee:      float = REBALANCING_FEE_RATE,
        perf_fee:           float = PERFORMANCE_FEE_RATE,
        initial_investment: float = 0.0,
    ):
        self.entry_fee      = entry_fee
        self.exit_fee       = exit_fee
        self.aum_fee_weekly = aum_fee_weekly
        self.rebalance_fee  = rebalance_fee
        self.perf_fee       = perf_fee
        # high_watermark = all-time high (for display)
        # period_watermark = last week's close (for fee calculation)
        _start = initial_investment * (1.0 - entry_fee)
        self.high_watermark   = _start
        self.period_watermark = _start
        self.total_fees_paid = {
            "entry":       0.0,
            "exit":        0.0,
            "aum":         0.0,
            "rebalancing": 0.0,
            "performance": 0.0,
        }

    # ── One-time fees ─────────────────────────────────────────────────────────

    def apply_entry_fee(self, initial_investment: float) -> float:
        """Charged once on day 0. Returns fee dollar amount."""
        fee = initial_investment * self.entry_fee
        self.total_fees_paid["entry"] += fee
        return fee

    def apply_exit_fee(self, final_value: float) -> float:
        """Charged once at strategy end. Returns fee dollar amount."""
        fee = final_value * self.exit_fee
        self.total_fees_paid["exit"] += fee
        return fee

    # ── Recurring fees ────────────────────────────────────────────────────────

    def apply_aum_fee(self, portfolio_value: float) -> float:
        """Deduct weekly AUM fee (1% p.a. / 52). Returns fee dollar amount."""
        fee = portfolio_value * self.aum_fee_weekly
        self.total_fees_paid["aum"] += fee
        return fee

    def apply_rebalancing_fee(self, rebalanced_amount: float) -> float:
        """0.3% on total traded amount. Returns fee dollar amount."""
        fee = rebalanced_amount * self.rebalance_fee
        self.total_fees_paid["rebalancing"] += fee
        return fee

    def apply_performance_fee(self, current_value: float) -> float:
        """
        Weekly crystallisation performance fee — 15% on any weekly gain.

        Charged when portfolio is above its value at the previous weekly
        close (period_watermark), regardless of all-time high. After each
        call the period_watermark is updated to current_value (post-fee),
        so it tracks each week's close — up or down. The all-time high
        (high_watermark) is updated separately for display.
        """
        if current_value <= self.period_watermark:
            # Down week — no fee, but reset period watermark to new base
            self.period_watermark = current_value
            return 0.0
        gain = current_value - self.period_watermark
        fee  = gain * self.perf_fee
        post_fee_value = current_value - fee
        self.period_watermark = post_fee_value
        self.high_watermark   = max(self.high_watermark, post_fee_value)
        self.total_fees_paid["performance"] += fee
        return fee

    # ── Summary ───────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        total = sum(self.total_fees_paid.values())
        return {**self.total_fees_paid, "total": round(total, 4)}
