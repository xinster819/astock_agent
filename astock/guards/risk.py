"""Small, deterministic portfolio risk controls.

The module is intentionally stateful only for trade-history cooldowns.  Portfolio
metrics are supplied by the caller, which keeps decisions reproducible and makes
this module independent of a strategy, broker, or clock.
"""
from dataclasses import dataclass
from typing import Optional

REGIMES = ("normal", "high_volatility", "risk_off")


def classify_market_regime(
    index_return: float,
    volatility: float,
    drawdown: float,
    *,
    risk_off_return: float = -0.06,
    risk_off_drawdown: float = -0.15,
    high_volatility: float = 0.018,
) -> str:
    """Classify market conditions using explicit, inclusive thresholds.

    ``drawdown`` and ``index_return`` are decimal fractions (``-0.05`` is -5%).
    Risk-off is checked first so a stressed market cannot be downgraded to merely
    high-volatility.

    阈值经沪深300近1000个交易日(2022-06~2026-07)分布校准，非拍脑袋：
      · 旧阈值(-3%收益 / -7%回撤)会在【51%】的交易日触发 risk_off——因为距120日
        峰值回撤的中位数就有 -6.2%，-7% 几乎是常态，等于半数时间冻结开仓。
      · 且实证上旧 risk_off 无保护价值：触发后20日沪深300平均【上涨】(均值回归)，
        封杀买入反而错过反弹。
      · 新阈值(-6%收益 / -15%回撤)把 risk_off 收敛为【9.9%】的真实尾部事件，仍能
        完整覆盖 2022-07、2025-04、2026-07 等真实急跌区间。
      · high_volatility 阈值从 2.5% 降到 1.8%(≈近4年20日波动率的p95)，让"高波动但
        未急跌"的市场进入减半开仓档，而非要么全放要么全封的二元切换。
    """
    values = (index_return, volatility, drawdown, risk_off_return,
              risk_off_drawdown, high_volatility)
    if not all(isinstance(value, (int, float)) for value in values):
        raise TypeError("market regime inputs must be numeric")
    if volatility < 0 or high_volatility < 0:
        raise ValueError("volatility thresholds must be non-negative")
    if index_return <= risk_off_return or drawdown <= risk_off_drawdown:
        return "risk_off"
    if volatility >= high_volatility:
        return "high_volatility"
    return "normal"


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


class RiskGuard:
    """Deterministic portfolio-level and trade-history risk guard."""

    def __init__(
        self,
        *,
        daily_loss_limit: float = 0.02,
        max_drawdown: float = 0.10,
        consecutive_loss_limit: int = 3,
        loss_cooldown_trades: int = 3,
        stop_loss_cooldown_trades: int = 3,
    ) -> None:
        if not 0 < daily_loss_limit <= 1:
            raise ValueError("daily_loss_limit must be in (0, 1]")
        if not 0 < max_drawdown <= 1:
            raise ValueError("max_drawdown must be in (0, 1]")
        if consecutive_loss_limit < 1:
            raise ValueError("consecutive_loss_limit must be positive")
        if loss_cooldown_trades < 0 or stop_loss_cooldown_trades < 0:
            raise ValueError("cooldown lengths cannot be negative")
        self.daily_loss_limit = float(daily_loss_limit)
        self.max_drawdown = float(max_drawdown)
        self.consecutive_loss_limit = int(consecutive_loss_limit)
        self.loss_cooldown_trades = int(loss_cooldown_trades)
        self.stop_loss_cooldown_trades = int(stop_loss_cooldown_trades)
        self._consecutive_losses = 0
        self._loss_cooldown_remaining = 0
        self._stop_loss_cooldowns: dict[str, int] = {}

    def to_dict(self) -> dict:
        """Serialize cooldown counters for persistence across restarts."""
        return {
            "consecutive_losses": self._consecutive_losses,
            "loss_cooldown_remaining": self._loss_cooldown_remaining,
            "stop_loss_cooldowns": dict(self._stop_loss_cooldowns),
        }

    def restore(self, data: Optional[dict]) -> None:
        """Restore persisted cooldown counters defensively."""
        if not isinstance(data, dict):
            return
        self._consecutive_losses = max(0, int(data.get("consecutive_losses", 0)))
        self._loss_cooldown_remaining = max(0, int(data.get("loss_cooldown_remaining", 0)))
        raw = data.get("stop_loss_cooldowns", {})
        if isinstance(raw, dict):
            restored = {}
            for symbol, remaining in raw.items():
                try:
                    remaining = int(remaining)
                except (TypeError, ValueError):
                    continue
                if remaining > 0:
                    restored[str(symbol)] = remaining
            self._stop_loss_cooldowns = restored

    def allow(
        self,
        equity: float,
        day_start_equity: float,
        peak_equity: float,
        symbol: Optional[str] = None,
    ) -> RiskDecision:
        """Return whether a new trade may be opened at the supplied snapshot."""
        if day_start_equity <= 0 or peak_equity <= 0:
            raise ValueError("reference equity values must be positive")
        if equity <= day_start_equity * (1 - self.daily_loss_limit):
            return RiskDecision(False, "daily_loss_limit")
        if equity <= peak_equity * (1 - self.max_drawdown):
            return RiskDecision(False, "max_drawdown")
        if self._loss_cooldown_remaining:
            return RiskDecision(False, "consecutive_loss_cooldown")
        if symbol is not None and self._stop_loss_cooldowns.get(symbol, 0):
            return RiskDecision(False, "symbol_stop_loss_cooldown")
        return RiskDecision(True, "ok")

    def record_trade(self, pnl: float, symbol: str, *, stop_loss: bool = False) -> None:
        """Record a closed trade and advance existing trade-count cooldowns."""
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(pnl, (int, float)):
            raise TypeError("pnl must be numeric")

        if self._loss_cooldown_remaining:
            self._loss_cooldown_remaining -= 1
        for name in list(self._stop_loss_cooldowns):
            remaining = self._stop_loss_cooldowns[name] - 1
            if remaining > 0:
                self._stop_loss_cooldowns[name] = remaining
            else:
                del self._stop_loss_cooldowns[name]

        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.consecutive_loss_limit:
                self._loss_cooldown_remaining = self.loss_cooldown_trades
        else:
            self._consecutive_losses = 0
        if stop_loss and self.stop_loss_cooldown_trades:
            self._stop_loss_cooldowns[symbol] = self.stop_loss_cooldown_trades
