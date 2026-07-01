"""
Moteur de paper trading.
- TP et SL adaptatifs selon la force du signal (watchlist)
- Trailing Stop activé à +15%, distance 10%
- Multi-TP : 50% à +20% (enregistré en DB), 50% selon signal
- Blacklist automatique 48h après SL
- Délai de grâce SL : 10 minutes
- Suivi post-trade 2h après fermeture
- Timeout: 24h
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiohttp

import config
import database as db

logger = logging.getLogger(__name__)

# ── Stratégie de sortie (valeurs par défaut si pas de signal watchlist) ───────
DEFAULT_TAKE_PROFIT_PCT  = 0.15
DEFAULT_STOP_LOSS_PCT    = 0.07
MAX_HOLD_HOURS           = 24
SL_GRACE_PERIOD_MINUTES  = 10
TRAILING_ACTIVATE        = 0.15
TRAILING_DISTANCE        = 0.10
TP1_PCT                  = 0.10   # TP1 à +10% (50% fermé)
TP1_SIZE                 = 0.50
BLACKLIST_HOURS          = 48

POST_TRADE_FOLLOW_MINUTES = 120
POST_TRADE_CHECK_INTERVAL = 300

_peak_prices: dict[str, float] = {}
_tp1_done: dict[str, bool] = {}
_blacklist: dict[str, datetime] = {}
_trade_params: dict[str, dict] = {}  # {trade_id: {tp_pct, sl_pct}}
_post_trade_tracking: dict[str, dict] = {}


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
    logger.info(
        "🚫 Blacklist: %s jusqu'à %s",
        symbol, expiry.strftime("%H:%M %d/%m")
    )


async def _fetch_price(address: str, chain: str = "solana") -> Optional[float]:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                chain_pairs = [
                    p for p in pairs
                    if p.get("chainId", "").lower() == chain.lower()
                ] or pairs
                if chain_pairs:
                    best = max(
                        chain_pairs,
                        key=lambda p: float(
                            p.get("liquidity", {}).get("usd", 0) or 0
                        )
                    )
                    price = float(best.get("priceUsd", 0) or 0)
                    return price if price > 0 else None
    except Exception as e:
        logger.warning("Fetch prix post-trade %s: %s", address, e)
    return None


class PaperTrader:
    def __init__(self):
        self.portfolio_value = config.INITIAL_PORTFOLIO
        self._sync_portfolio()

    def _sync_portfolio(self):
        stats = db.get_portfolio_stats()
        realized_pnl = stats.get("total_pnl_usd", 0)
        self.portfolio_value = config.INITIAL_PORTFOLIO + realized_pnl

    def can_trade(self, token_address: str) -> tuple[bool, str]:
        if is_blacklisted(token_address):
            return False, "Token en blacklist (SL récent)"
        open_trades = db.get_open_trades()
        if len(open_trades) >= config.MAX_OPEN_POSITIONS:
            return False, f"Max positions atteintes ({config.MAX_OPEN_POSITIONS})"
        existing = db.get_trade_by_address(token_address)
        if existing:
            return False, "Position déjà ouverte sur ce token"
        return True, "ok"

    def open_trade(
        self,
        token: dict,
        ai_score: int,
        tp_pct: Optional[float] = None,
        sl_pct: Optional[float] = None,
        signal_label: str = "STANDARD",
    ) -> dict | None:
        can, reason = self.can_trade(token["address"])
        if not can:
            logger.debug("Trade refusé [%s]: %s", token["symbol"], reason)
            return None

        self._sync_portfolio()
        position_usd = self.portfolio_value * config.POSITION_SIZE_PCT
        slippage = _estimate_slippage(
            token.get("liquidity_usd", 10000), position_usd
        )
        entry_price = token["price_usd"] * (1 + slippage)

        trade_id = db.open_trade(token, entry_price, position_usd, ai_score)

        # Paramètres TP/SL adaptatifs ou défaut
        effective_tp = (tp_pct / 100) if tp_pct else DEFAULT_TAKE_PROFIT_PCT
        effective_sl = (sl_pct / 100) if sl_pct else DEFAULT_STOP_LOSS_PCT

        _peak_prices[token["address"]] = entry_price
        _tp1_done[str(trade_id)] = False
        _trade_params[str(trade_id)] = {
            "tp_pct": effective_tp,
            "sl_pct": effective_sl,
            "signal_label": signal_label,
        }

        logger.info(
            "📈 Trade OUVERT #%d | %s | $%.8f | score=%d | "
            "TP: +%.0f%% | SL: -%.0f%% | Signal: %s",
            trade_id, token["symbol"], entry_price, ai_score,
            effective_tp * 100, effective_sl * 100, signal_label
        )

        return {
            "id": trade_id,
            "symbol": token["symbol"],
            "name": token["name"],
            "chain": token["chain"],
            "entry_price": entry_price,
            "position_usd": position_usd,
            "ai_score": ai_score,
            "tp_pct": effective_tp * 100,
            "sl_pct": effective_sl * 100,
            "signal_label": signal_label,
            "url": token.get("url", ""),
        }

    def start_post_trade_tracking(
        self, closed_trade: dict, close_price: float
    ):
        addr = closed_trade.get("token_address", "")
        if not addr:
            return
        _post_trade_tracking[addr] = {
            "closed_trade": closed_trade,
            "close_price": close_price,
            "close_time": datetime.now(timezone.utc),
            "snapshots": [],
            "chain": closed_trade.get("chain", "solana"),
        }

    async def check_post_trade_tracking(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        completed = []
        to_remove = []

        for addr, tracking in _post_trade_tracking.items():
            close_time = tracking["close_time"]
            elapsed = (now - close_time).total_seconds() / 60

            if elapsed >= POST_TRADE_FOLLOW_MINUTES:
                final_price = await _fetch_price(addr, tracking["chain"])
                if final_price:
                    tracking["snapshots"].append({
                        "minutes": round(elapsed),
                        "price": final_price,
                    })
                completed.append({
                    "trade": tracking["closed_trade"],
                    "close_price": tracking["close_price"],
                    "snapshots": tracking["snapshots"],
                    "final_price": final_price or tracking["close_price"],
                    "duration_minutes": round(elapsed),
                })
                to_remove.append(addr)
                continue

            snapshots = tracking["snapshots"]
            last_check_min = snapshots[-1]["minutes"] if snapshots else 0
            if elapsed - last_check_min >= POST_TRADE_CHECK_INTERVAL / 60:
                price = await _fetch_price(addr, tracking["chain"])
                if price:
                    tracking["snapshots"].append({
                        "minutes": round(elapsed),
                        "price": price,
                    })

        for addr in to_remove:
            del _post_trade_tracking[addr]

        return completed

    async def fetch_missing_prices(
        self,
        open_trades: list[dict],
        current_prices: dict[str, float]
    ) -> dict[str, float]:
        missing = [
            t for t in open_trades
            if t["token_address"] not in current_prices
        ]
        if not missing:
            return current_prices

        updated = dict(current_prices)
        try:
            async with aiohttp.ClientSession() as session:
                for trade in missing:
                    addr = trade["token_address"]
                    chain = trade.get("chain", "solana")
                    url = (
                        f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
                    )
                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                pairs = data.get("pairs", [])
                                chain_pairs = [
                                    p for p in pairs
                                    if p.get("chainId", "").lower()
                                    == chain.lower()
                                ] or pairs
                                if chain_pairs:
                                    best = max(
                                        chain_pairs,
                                        key=lambda p: float(
                                            p.get("liquidity", {}).get(
                                                "usd", 0
                                            ) or 0
                                        )
                                    )
                                    price = float(
                                        best.get("priceUsd", 0) or 0
                                    )
                                    if price > 0:
                                        updated[addr] = price
                    except Exception as e:
                        logger.warning("Fetch prix %s: %s", addr, e)
        except Exception as e:
            logger.warning("Erreur session fetch: %s", e)

        return updated

    async def check_and_close_trades(
        self, current_prices: dict[str, float]
    ) -> list[dict]:
        closed = []
        open_trades = db.get_open_trades()
        if not open_trades:
            return []

        current_prices = await self.fetch_missing_prices(
            open_trades, current_prices
        )

        for trade in open_trades:
            addr = trade["token_address"]
            symbol = trade["token_symbol"]
            trade_id = str(trade["id"])
            current_price = current_prices.get(addr)

            if not current_price or current_price <= 0:
                logger.debug(
                    "Prix indisponible pour %s — position conservée", symbol
                )
                continue

            entry = trade["entry_price"]
            if entry <= 0:
                continue

            pnl_pct = ((current_price - entry) / entry) * 100

            open_at = datetime.fromisoformat(trade["open_at"])
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=timezone.utc)
            trade_age = datetime.now(timezone.utc) - open_at
            in_grace_period = trade_age < timedelta(
                minutes=SL_GRACE_PERIOD_MINUTES
            )

            # Récupère les paramètres adaptatifs de ce trade
            params = _trade_params.get(trade_id, {})
            tp_pct = params.get("tp_pct", DEFAULT_TAKE_PROFIT_PCT)
            sl_pct = params.get("sl_pct", DEFAULT_STOP_LOSS_PCT)

            # ── Peak tracking ──────────────────────────────────────────────
            peak = _peak_prices.get(addr, entry)
            if current_price > peak:
                _peak_prices[addr] = current_price
                peak = current_price

            # ── Trailing Stop ──────────────────────────────────────────────
            peak_pnl_pct = ((peak - entry) / entry) * 100
            if peak_pnl_pct >= TRAILING_ACTIVATE * 100:
                trailing_sl_price = peak * (1 - TRAILING_DISTANCE)
                if current_price <= trailing_sl_price:
                    exit_price = current_price * (1 - 0.003)
                    closed_trade = db.close_trade(
                        trade["id"], exit_price, "trailing_stop"
                    )
                    logger.info(
                        "📉 Trailing SL #%d | %s | Peak: +%.1f%% | "
                        "PnL: %.2f%%",
                        trade["id"], symbol, peak_pnl_pct, pnl_pct
                    )
                    self.start_post_trade_tracking(closed_trade, current_price)
                    _peak_prices.pop(addr, None)
                    _tp1_done.pop(trade_id, None)
                    _trade_params.pop(trade_id, None)
                    closed.append({**closed_trade, "reason": "trailing_stop"})
                    continue

            # ── Stop Loss adaptatif ────────────────────────────────────────
            if not in_grace_period and pnl_pct <= -sl_pct * 100:
                exit_price = current_price * (1 - 0.003)
                closed_trade = db.close_trade(
                    trade["id"], exit_price, "stop_loss"
                )
                logger.info(
                    "🔴 SL #%d | %s | PnL: %.2f%% (seuil: -%.0f%%)",
                    trade["id"], symbol, pnl_pct, sl_pct * 100
                )
                _add_to_blacklist(addr, symbol)
                self.start_post_trade_tracking(closed_trade, current_price)
                _peak_prices.pop(addr, None)
                _tp1_done.pop(trade_id, None)
                _trade_params.pop(trade_id, None)
                closed.append({**closed_trade, "reason": "stop_loss"})
                continue
            elif in_grace_period and pnl_pct <= -sl_pct * 100:
                logger.debug(
                    "⏳ SL ignoré (grâce) #%d | %s | %.2f%%",
                    trade["id"], symbol, pnl_pct
                )

            # ── TP1 adaptatif : 50% à la moitié du TP cible ───────────────
            tp1_threshold = tp_pct * 0.5 * 100  # ex: TP=25% → TP1 à 12.5%
            if (
                pnl_pct >= tp1_threshold
                and not _tp1_done.get(trade_id, False)
            ):
                _tp1_done[trade_id] = True
                db.mark_tp1_hit(trade["id"], pnl_pct)
                exit_price = current_price * (1 - 0.005)
                partial_usd = trade["position_usd"] * TP1_SIZE
                partial_pnl_usd = partial_usd * (pnl_pct / 100)
                logger.info(
                    "🟡 TP1 #%d | %s | +%.1f%% | $%.2f profit",
                    trade["id"], symbol, pnl_pct, partial_pnl_usd
                )
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

            # ── TP2 adaptatif : 100% au TP cible ──────────────────────────
            if pnl_pct >= tp_pct * 100:
                exit_price = current_price * (1 - 0.005)
                closed_trade = db.close_trade(
                    trade["id"], exit_price, "take_profit"
                )
                logger.info(
                    "🟢 TP2 #%d | %s | PnL: +%.2f%% (cible: +%.0f%%)",
                    trade["id"], symbol, pnl_pct, tp_pct * 100
                )
                _peak_prices.pop(addr, None)
                _tp1_done.pop(trade_id, None)
                _trade_params.pop(trade_id, None)
                closed.append({**closed_trade, "reason": "take_profit"})
                continue

            # ── Timeout ────────────────────────────────────────────────────
            if trade_age > timedelta(hours=MAX_HOLD_HOURS):
                exit_price = current_price * (1 - 0.005)
                closed_trade = db.close_trade(
                    trade["id"], exit_price, "timeout"
                )
                logger.info(
                    "⏰ Timeout #%d | %s | PnL: %.2f%%",
                    trade["id"], symbol, pnl_pct
                )
                self.start_post_trade_tracking(closed_trade, current_price)
                _peak_prices.pop(addr, None)
                _tp1_done.pop(trade_id, None)
                _trade_params.pop(trade_id, None)
                closed.append({**closed_trade, "reason": "timeout"})

        return closed

    def get_blacklist(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [
            {
                "address": addr,
                "expires_in_h": round(
                    (exp - now).total_seconds() / 3600, 1
                )
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
                (self.portfolio_value - config.INITIAL_PORTFOLIO)
                / config.INITIAL_PORTFOLIO * 100, 2
            ),
            "open_positions": len(open_trades),
            "total_trades": stats["total_trades"],
            "win_rate": stats["win_rate"],
        }


def _estimate_slippage(
    liquidity_usd: float, trade_size_usd: float
) -> float:
    if liquidity_usd <= 0:
        return 0.02
    impact = trade_size_usd / liquidity_usd
    return min(0.005 + impact * 0.5, 0.05)
