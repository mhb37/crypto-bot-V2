"""
Moteur de paper trading — simule des trades avec slippage réaliste.
"""
import logging
from datetime import datetime, timezone, timedelta

import config
import database as db

logger = logging.getLogger(__name__)


class PaperTrader:
    def __init__(self):
        self.portfolio_value = config.INITIAL_PORTFOLIO
        self._sync_portfolio()

    def _sync_portfolio(self):
        """Recalcule la valeur du portfolio depuis la DB."""
        stats = db.get_portfolio_stats()
        realized_pnl = stats.get("total_pnl_usd", 0)
        self.portfolio_value = config.INITIAL_PORTFOLIO + realized_pnl

    def can_trade(self, token_address: str) -> tuple[bool, str]:
        """Vérifie si on peut ouvrir un trade sur ce token."""
        open_trades = db.get_open_trades()

        # Max positions simultanées
        if len(open_trades) >= config.MAX_OPEN_POSITIONS:
            return False, f"Max positions atteintes ({config.MAX_OPEN_POSITIONS})"

        # Déjà une position ouverte sur ce token
        existing = db.get_trade_by_address(token_address)
        if existing:
            return False, "Position déjà ouverte sur ce token"

        return True, "ok"

    def open_trade(self, token: dict, ai_score: int) -> dict | None:
        """
        Ouvre un trade paper.
        Retourne le trade ouvert ou None si impossible.
        """
        can, reason = self.can_trade(token["address"])
        if not can:
            logger.debug("Trade refusé [%s]: %s", token["symbol"], reason)
            return None

        # Taille de position
        self._sync_portfolio()
        position_usd = self.portfolio_value * config.POSITION_SIZE_PCT

        # Slippage simulé (0.5% à 2% selon la liquidité)
        slippage = _estimate_slippage(token.get("liquidity_usd", 10000), position_usd)
        entry_price = token["price_usd"] * (1 + slippage)

        trade_id = db.open_trade(token, entry_price, position_usd, ai_score)
        logger.info(
            "📈 Trade OUVERT #%d | %s | $%.6f | score=%d | size=$%.2f | slippage=%.2f%%",
            trade_id, token["symbol"], entry_price, ai_score, position_usd, slippage * 100
        )

        return {
            "id": trade_id,
            "symbol": token["symbol"],
            "name": token["name"],
            "chain": token["chain"],
            "entry_price": entry_price,
            "position_usd": position_usd,
            "ai_score": ai_score,
            "url": token.get("url", ""),
        }

    def check_and_close_trades(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Vérifie les trades ouverts et ferme ceux qui atteignent SL/TP/timeout.
        current_prices: {token_address: current_price}
        Retourne la liste des trades fermés.
        """
        closed = []
        open_trades = db.get_open_trades()

        for trade in open_trades:
            addr = trade["token_address"]
            current_price = current_prices.get(addr)
            if not current_price or current_price <= 0:
                current_price = _estimate_current_price(trade)

            entry = trade["entry_price"]
            if entry <= 0:
                continue

            pnl_pct = ((current_price - entry) / entry) * 100

            # ── Stop Loss ──────────────────────────────────────────────────
            if pnl_pct <= -config.STOP_LOSS_PCT * 100:
                slippage = 0.003  # 0.3% slippage à la vente
                exit_price = current_price * (1 - slippage)
                closed_trade = db.close_trade(trade["id"], exit_price, "stop_loss")
                logger.info(
                    "🔴 SL déclenché #%d | %s | PnL: %.2f%%",
                    trade["id"], trade["token_symbol"], pnl_pct
                )
                closed.append({**closed_trade, "reason": "stop_loss"})
                continue

            # ── Take Profit ────────────────────────────────────────────────
            if pnl_pct >= config.TAKE_PROFIT_PCT * 100:
                slippage = 0.005
                exit_price = current_price * (1 - slippage)
                closed_trade = db.close_trade(trade["id"], exit_price, "take_profit")
                logger.info(
                    "🟢 TP atteint #%d | %s | PnL: +%.2f%%",
                    trade["id"], trade["token_symbol"], pnl_pct
                )
                closed.append({**closed_trade, "reason": "take_profit"})
                continue

            # ── Timeout ────────────────────────────────────────────────────
            open_at = datetime.fromisoformat(trade["open_at"])
            age = datetime.now(timezone.utc) - open_at
            if age > timedelta(hours=config.MAX_HOLD_HOURS):
                slippage = 0.005
                exit_price = current_price * (1 - slippage)
                closed_trade = db.close_trade(trade["id"], exit_price, "timeout")
                logger.info(
                    "⏰ Timeout #%d | %s | PnL: %.2f%%",
                    trade["id"], trade["token_symbol"], pnl_pct
                )
                closed.append({**closed_trade, "reason": "timeout"})

        return closed

    def get_positions_summary(self) -> dict:
        """Résumé du portfolio pour l'affichage Telegram."""
        self._sync_portfolio()
        stats = db.get_portfolio_stats()
        open_trades = db.get_open_trades()

        return {
            "portfolio_value": round(self.portfolio_value, 2),
            "initial_value": config.INITIAL_PORTFOLIO,
            "total_pnl_usd": stats["total_pnl_usd"],
            "total_pnl_pct": round(
                (self.portfolio_value - config.INITIAL_PORTFOLIO) / config.INITIAL_PORTFOLIO * 100, 2
            ),
            "open_positions": len(open_trades),
            "total_trades": stats["total_trades"],
            "win_rate": stats["win_rate"],
            "positions": [
                {
                    "id": t["id"],
                    "symbol": t["token_symbol"],
                    "entry": t["entry_price"],
                    "size_usd": t["position_usd"],
                    "open_at": t["open_at"],
                }
                for t in open_trades
            ],
        }


def _estimate_slippage(liquidity_usd: float, trade_size_usd: float) -> float:
    """Estime le slippage basé sur la liquidité et la taille de position."""
    if liquidity_usd <= 0:
        return 0.02
    impact = trade_size_usd / liquidity_usd
    base_slippage = 0.005
    return min(base_slippage + impact * 0.5, 0.05)


def _estimate_current_price(trade: dict) -> float:
    """Fallback: retourne le prix d'entrée si le prix actuel est inconnu."""
    return trade["entry_price"]
