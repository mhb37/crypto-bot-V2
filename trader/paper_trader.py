"""
Moteur de paper trading — TP 30% / SL 10% / timeout 24h.
Inclut fetch de prix dédié pour les positions sans prix dans le scan.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiohttp

import config
import database as db

logger = logging.getLogger(__name__)

# ── Stratégie de sortie ───────────────────────────────────────────────────────
TAKE_PROFIT_PCT = 0.30   # +30%
STOP_LOSS_PCT   = 0.10   # -10%
MAX_HOLD_HOURS  = 24     # timeout 24h


class PaperTrader:
    def __init__(self):
        self.portfolio_value = config.INITIAL_PORTFOLIO
        self._sync_portfolio()

    def _sync_portfolio(self):
        stats = db.get_portfolio_stats()
        realized_pnl = stats.get("total_pnl_usd", 0)
        self.portfolio_value = config.INITIAL_PORTFOLIO + realized_pnl

    def can_trade(self, token_address: str) -> tuple[bool, str]:
        open_trades = db.get_open_trades()
        if len(open_trades) >= config.MAX_OPEN_POSITIONS:
            return False, f"Max positions atteintes ({config.MAX_OPEN_POSITIONS})"
        existing = db.get_trade_by_address(token_address)
        if existing:
            return False, "Position déjà ouverte sur ce token"
        return True, "ok"

    def open_trade(self, token: dict, ai_score: int) -> dict | None:
        can, reason = self.can_trade(token["address"])
        if not can:
            logger.debug("Trade refusé [%s]: %s", token["symbol"], reason)
            return None

        self._sync_portfolio()
        position_usd = self.portfolio_value * config.POSITION_SIZE_PCT
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

    async def fetch_missing_prices(
        self,
        open_trades: list[dict],
        current_prices: dict[str, float]
    ) -> dict[str, float]:
        """
        Pour les positions dont le prix n'est pas dans le scan courant,
        fait un fetch dédié sur DexScreener.
        """
        missing = [
            t for t in open_trades
            if t["token_address"] not in current_prices
        ]
        if not missing:
            return current_prices

        updated = dict(current_prices)
        addresses = [t["token_address"] for t in missing]

        try:
            async with aiohttp.ClientSession() as session:
                chunk = ",".join(addresses[:30])
                url = f"https://api.dexscreener.com/latest/dex/tokens/{chunk}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for pair in data.get("pairs", []):
                            addr = pair.get("baseToken", {}).get("address", "")
                            price = float(pair.get("priceUsd", 0) or 0)
                            if addr and price > 0:
                                updated[addr] = price
                                logger.debug("Prix fetchés pour %s: $%.8f", addr, price)
        except Exception as e:
            logger.warning("Erreur fetch prix manquants: %s", e)

        return updated

    async def check_and_close_trades(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Vérifie les trades ouverts et ferme ceux qui atteignent SL/TP/timeout.
        Fetch les prix manquants si besoin.
        """
        closed = []
        open_trades = db.get_open_trades()
        if not open_trades:
            return []

        # ← Fix : fetch prix pour les tokens absents du scan
        current_prices = await self.fetch_missing_prices(open_trades, current_prices)

        for trade in open_trades:
            addr = trade["token_address"]
            current_price = current_prices.get(addr)

            if not current_price or current_price <= 0:
                logger.debug("Prix indisponible pour %s — position conservée", trade["token_symbol"])
                continue

            entry = trade["entry_price"]
            if entry <= 0:
                continue

            pnl_pct = ((current_price - entry) / entry) * 100

            # ── Stop Loss ──────────────────────────────────────────────────
            if pnl_pct <= -STOP_LOSS_PCT * 100:
                exit_price = current_price * (1 - 0.003)
                closed_trade = db.close_trade(trade["id"], exit_price, "stop_loss")
                logger.info("🔴 SL #%d | %s | PnL: %.2f%%", trade["id"], trade["token_symbol"], pnl_pct)
                closed.append({**closed_trade, "reason": "stop_loss"})
                continue

            # ── Take Profit ────────────────────────────────────────────────
            if pnl_pct >= TAKE_PROFIT_PCT * 100:
                exit_price = current_price * (1 - 0.005)
                closed_trade = db.close_trade(trade["id"], exit_price, "take_profit")
                logger.info("🟢 TP #%d | %s | PnL: +%.2f%%", trade["id"], trade["token_symbol"], pnl_pct)
                closed.append({**closed_trade, "reason": "take_profit"})
                continue

            # ── Timeout ────────────────────────────────────────────────────
            open_at = datetime.fromisoformat(trade["open_at"])
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - open_at
            if age > timedelta(hours=MAX_HOLD_HOURS):
                exit_price = current_price * (1 - 0.005)
                closed_trade = db.close_trade(trade["id"], exit_price, "timeout")
                logger.info("⏰ Timeout #%d | %s | PnL: %.2f%%", trade["id"], trade["token_symbol"], pnl_pct)
                closed.append({**closed_trade, "reason": "timeout"})

        return closed

    def get_positions_with_pnl(self, current_prices: dict[str, float]) -> list[dict]:
        """Retourne les positions ouvertes avec PnL temps réel."""
        open_trades = db.get_open_trades()
        positions = []
        for t in open_trades:
            addr = t["token_address"]
            entry = t["entry_price"]
            current = current_prices.get(addr, 0)
            if current > 0 and entry > 0:
                pnl_pct = ((current - entry) / entry) * 100
                pnl_usd = t["position_usd"] * (pnl_pct / 100)
            else:
                pnl_pct = 0.0
                pnl_usd = 0.0
                current = entry
            positions.append({
                **t,
                "current_price": current,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
            })
        return positions

    def get_positions_summary(self) -> dict:
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
        }


def _estimate_slippage(liquidity_usd: float, trade_size_usd: float) -> float:
    if liquidity_usd <= 0:
        return 0.02
    impact = trade_size_usd / liquidity_usd
    return min(0.005 + impact * 0.5, 0.05)
