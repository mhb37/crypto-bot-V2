"""
Moteur de paper trading.
- Trailing Stop activé à +15%, distance 10%
- Multi-TP : 50% à +20%, 50% à +40%
- Blacklist automatique 48h après SL
- TP: +30% / SL: -10% / Timeout: 24h
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiohttp

import config
import database as db

logger = logging.getLogger(__name__)

# ── Stratégie de sortie ───────────────────────────────────────────────────────
TAKE_PROFIT_PCT      = 0.30   # TP final (si pas de multi-TP)
STOP_LOSS_PCT        = 0.10   # SL initial
MAX_HOLD_HOURS       = 24     # Timeout

# Trailing Stop
TRAILING_ACTIVATE    = 0.15   # S'active à +15%
TRAILING_DISTANCE    = 0.10   # SL remonte à (peak - 10%)

# Multi-TP
TP1_PCT              = 0.20   # Premier TP à +20%
TP1_SIZE             = 0.50   # Ferme 50% de la position
TP2_PCT              = 0.40   # Deuxième TP à +40%

# Blacklist
BLACKLIST_HOURS      = 48     # Durée blacklist après SL

# État en mémoire
_peak_prices: dict[str, float] = {}        # {address: prix_max_atteint}
_tp1_done: dict[str, bool] = {}            # {trade_id: tp1_déjà_exécuté}
_blacklist: dict[str, datetime] = {}       # {address: expiration}


def is_blacklisted(address: str) -> bool:
    expiry = _blacklist.get(address)
    if not expiry:
        return False
    if datetime.now(timezone.utc) > expiry:
        del _blacklist[address]
        return False
    return True


def _add_to_blacklist(address: str, symbol: str):
    expiry = datetime.now(timezone.utc) + timedelta(hours=BLACKLIST_HOURS)
    _blacklist[address] = expiry
    logger.info("🚫 Blacklist: %s jusqu'à %s", symbol, expiry.strftime("%H:%M %d/%m"))


class PaperTrader:
    def __init__(self):
        self.portfolio_value = config.INITIAL_PORTFOLIO
        self._sync_portfolio()

    def _sync_portfolio(self):
        stats = db.get_portfolio_stats()
        realized_pnl = stats.get("total_pnl_usd", 0)
        self.portfolio_value = config.INITIAL_PORTFOLIO + realized_pnl

    def can_trade(self, token_address: str) -> tuple[bool, str]:
        # Vérifier blacklist
        if is_blacklisted(token_address):
            return False, "Token en blacklist (SL récent)"

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

        # Init trailing et multi-TP
        _peak_prices[token["address"]] = entry_price
        _tp1_done[str(trade_id)] = False

        logger.info(
            "📈 Trade OUVERT #%d | %s | $%.8f | score=%d | size=$%.2f",
            trade_id, token["symbol"], entry_price, ai_score, position_usd
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
        missing = [t for t in open_trades if t["token_address"] not in current_prices]
        if not missing:
            return current_prices

        updated = dict(current_prices)
        try:
            async with aiohttp.ClientSession() as session:
                for trade in missing:
                    addr = trade["token_address"]
                    chain = trade.get("chain", "solana")
                    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                pairs = data.get("pairs", [])
                                chain_pairs = [
                                    p for p in pairs
                                    if p.get("chainId", "").lower() == chain.lower()
                                ] or pairs
                                if chain_pairs:
                                    best = max(
                                        chain_pairs,
                                        key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0)
                                    )
                                    price = float(best.get("priceUsd", 0) or 0)
                                    if price > 0:
                                        updated[addr] = price
                    except Exception as e:
                        logger.warning("Fetch prix %s: %s", addr, e)
        except Exception as e:
            logger.warning("Erreur session fetch: %s", e)

        return updated

    async def check_and_close_trades(self, current_prices: dict[str, float]) -> list[dict]:
        """
        Vérifie SL / Trailing Stop / Multi-TP / Timeout.
        Retourne la liste des trades fermés (ou partiellement fermés).
        """
        closed = []
        open_trades = db.get_open_trades()
        if not open_trades:
            return []

        current_prices = await self.fetch_missing_prices(open_trades, current_prices)

        for trade in open_trades:
            addr = trade["token_address"]
            symbol = trade["token_symbol"]
            trade_id = str(trade["id"])
            current_price = current_prices.get(addr)

            if not current_price or current_price <= 0:
                logger.debug("Prix indisponible pour %s — position conservée", symbol)
                continue

            entry = trade["entry_price"]
            if entry <= 0:
                continue

            pnl_pct = ((current_price - entry) / entry) * 100

            # ── Mise à jour du prix max (trailing) ────────────────────────
            peak = _peak_prices.get(addr, entry)
            if current_price > peak:
                _peak_prices[addr] = current_price
                peak = current_price

            # ── Trailing Stop Loss ─────────────────────────────────────────
            peak_pnl_pct = ((peak - entry) / entry) * 100
            if peak_pnl_pct >= TRAILING_ACTIVATE * 100:
                trailing_sl_price = peak * (1 - TRAILING_DISTANCE)
                if current_price <= trailing_sl_price:
                    slippage = 0.003
                    exit_price = current_price * (1 - slippage)
                    closed_trade = db.close_trade(trade["id"], exit_price, "trailing_stop")
                    logger.info(
                        "📉 Trailing SL #%d | %s | Peak: +%.1f%% | PnL final: %.2f%%",
                        trade["id"], symbol, peak_pnl_pct, pnl_pct
                    )
                    # Pas de blacklist sur trailing stop (sortie positive probable)
                    _peak_prices.pop(addr, None)
                    _tp1_done.pop(trade_id, None)
                    closed.append({**closed_trade, "reason": "trailing_stop"})
                    continue

            # ── Stop Loss classique ────────────────────────────────────────
            if pnl_pct <= -STOP_LOSS_PCT * 100:
                slippage = 0.003
                exit_price = current_price * (1 - slippage)
                closed_trade = db.close_trade(trade["id"], exit_price, "stop_loss")
                logger.info("🔴 SL #%d | %s | PnL: %.2f%%", trade["id"], symbol, pnl_pct)
                _add_to_blacklist(addr, symbol)
                _peak_prices.pop(addr, None)
                _tp1_done.pop(trade_id, None)
                closed.append({**closed_trade, "reason": "stop_loss"})
                continue

            # ── Multi-TP 1 : +20% → ferme 50% ────────────────────────────
            if pnl_pct >= TP1_PCT * 100 and not _tp1_done.get(trade_id, False):
                _tp1_done[trade_id] = True
                exit_price = current_price * (1 - 0.005)
                partial_usd = trade["position_usd"] * TP1_SIZE
                partial_pnl_usd = partial_usd * (pnl_pct / 100)
                logger.info(
                    "🟡 TP1 #%d | %s | +%.1f%% | Partiel: $%.2f profit",
                    trade["id"], symbol, pnl_pct, partial_pnl_usd
                )
                # On enregistre un trade partiel fictif pour le notifier
                closed.append({
                    "id": trade["id"],
                    "token_symbol": symbol,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": partial_pnl_usd,
                    "position_usd": partial_usd,
                    "reason": "tp1_partial",
                    "partial": True,
                })
                continue

            # ── Multi-TP 2 : +40% → ferme 100% ───────────────────────────
            if pnl_pct >= TP2_PCT * 100:
                slippage = 0.005
                exit_price = current_price * (1 - slippage)
                closed_trade = db.close_trade(trade["id"], exit_price, "take_profit")
                logger.info("🟢 TP2 #%d | %s | PnL: +%.2f%%", trade["id"], symbol, pnl_pct)
                _peak_prices.pop(addr, None)
                _tp1_done.pop(trade_id, None)
                closed.append({**closed_trade, "reason": "take_profit"})
                continue

            # ── Timeout ────────────────────────────────────────────────────
            open_at = datetime.fromisoformat(trade["open_at"])
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - open_at
            if age > timedelta(hours=MAX_HOLD_HOURS):
                slippage = 0.005
                exit_price = current_price * (1 - slippage)
                closed_trade = db.close_trade(trade["id"], exit_price, "timeout")
                logger.info("⏰ Timeout #%d | %s | PnL: %.2f%%", trade["id"], symbol, pnl_pct)
                _peak_prices.pop(addr, None)
                _tp1_done.pop(trade_id, None)
                closed.append({**closed_trade, "reason": "timeout"})

        return closed

    def get_blacklist(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [
            {
                "address": addr,
                "expires_in_h": round((exp - now).total_seconds() / 3600, 1)
            }
            for addr, exp in _blacklist.items()
            if exp > now
        ]

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
