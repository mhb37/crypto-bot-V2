"""
Watchlist active — gère les tokens en surveillance intensive.
Un token entre en watchlist quand il prouve un fort momentum sur h6/h24.
Le bot surveille ensuite chaque token toutes les 60s pour détecter
un point d'entrée optimal basé sur le micro-momentum et la pression acheteuse.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import aiohttp

logger = logging.getLogger(__name__)

# ── Critères d'entrée en watchlist ────────────────────────────────────────────
WATCHLIST_MIN_H24        = 20.0   # h24 minimum pour entrer en watchlist
WATCHLIST_MIN_H6         = 10.0   # h6 minimum
WATCHLIST_MIN_LIQUIDITY  = 10000  # liquidité minimum $10k
WATCHLIST_MAX_TOKENS     = 20     # max tokens surveillés simultanément
WATCHLIST_EXPIRY_HOURS   = 6      # retire le token après 6h sans trade

# ── Critères de signal d'entrée ───────────────────────────────────────────────
ENTRY_MIN_PRICE_CHANGE_5M  = 1.0   # prix monte d'au moins +1% sur 5 min
ENTRY_MIN_BUY_RATIO        = 0.60  # 60% d'achats minimum sur les txns récentes
ENTRY_MIN_VOLUME_ACCEL     = 1.5   # volume 5min > 1.5x la moyenne par minute

# ── Structure d'un token en watchlist ─────────────────────────────────────────
# {
#   address: str,
#   symbol: str,
#   chain: str,
#   added_at: datetime,
#   prices: list[{time: datetime, price: float}],  # historique des prix
#   volumes: list[{time: datetime, volume: float}], # historique des volumes
#   buys: list[{time: datetime, count: int}],
#   sells: list[{time: datetime, count: int}],
#   last_entry_at: Optional[datetime],  # dernier trade ouvert sur ce token
#   token_data: dict,  # données complètes du token pour scoring
# }

_watchlist: dict[str, dict] = {}


def add_to_watchlist(token: dict) -> bool:
    """
    Ajoute un token à la watchlist si les critères sont remplis.
    Retourne True si ajouté, False sinon.
    """
    address = token.get("address", "")
    if not address:
        return False

    # Déjà en watchlist
    if address in _watchlist:
        # Met à jour les données du token
        _watchlist[address]["token_data"] = token
        return False

    # Vérifier critères
    h24 = token.get("price_change_24h", 0)
    h6 = token.get("price_change_6h", 0)
    liq = token.get("liquidity_usd", 0)

    if h24 < WATCHLIST_MIN_H24 and h6 < WATCHLIST_MIN_H6:
        return False
    if liq < WATCHLIST_MIN_LIQUIDITY:
        return False

    # Adresse EVM sur Solana → ignorer
    if token.get("chain") == "solana" and token.get("address", "").startswith("0x"):
        return False

    # Watchlist pleine
    if len(_watchlist) >= WATCHLIST_MAX_TOKENS:
        # Retire le plus ancien
        oldest = min(_watchlist.items(), key=lambda x: x[1]["added_at"])
        del _watchlist[oldest[0]]
        logger.info(
            "Watchlist pleine — retire %s pour faire place à %s",
            oldest[1]["symbol"], token.get("symbol")
        )

    now = datetime.now(timezone.utc)
    price = token.get("price_usd", 0)

    _watchlist[address] = {
        "address": address,
        "symbol": token.get("symbol", "?"),
        "chain": token.get("chain", "solana"),
        "added_at": now,
        "prices": [{"time": now, "price": price}] if price > 0 else [],
        "buys": [{"time": now, "count": token.get("buys_1h", 0)}],
        "sells": [{"time": now, "count": token.get("sells_1h", 0)}],
        "last_entry_at": None,
        "token_data": token,
        "entry_count": 0,
    }

    logger.info(
        "✅ Watchlist +%s | h6: +%.1f%% | h24: +%.1f%% | Liq: $%.0f",
        token.get("symbol"), h6, h24, liq
    )
    return True


def remove_from_watchlist(address: str):
    if address in _watchlist:
        symbol = _watchlist[address]["symbol"]
        del _watchlist[address]
        logger.info("❌ Watchlist -%s", symbol)


def get_watchlist() -> list[dict]:
    return list(_watchlist.values())


def get_watchlist_count() -> int:
    return len(_watchlist)


def cleanup_expired():
    """Retire les tokens expirés de la watchlist."""
    now = datetime.now(timezone.utc)
    expired = [
        addr for addr, w in _watchlist.items()
        if (now - w["added_at"]).total_seconds() / 3600 > WATCHLIST_EXPIRY_HOURS
    ]
    for addr in expired:
        logger.info(
            "⏰ Watchlist expire: %s (>%dh sans trade)",
            _watchlist[addr]["symbol"], WATCHLIST_EXPIRY_HOURS
        )
        del _watchlist[addr]


async def fetch_realtime_data(address: str, chain: str) -> Optional[dict]:
    """
    Fetch les données temps réel d'un token depuis DexScreener.
    Retourne price, buys_1h, sells_1h, volume_1h ou None si erreur.
    """
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
                if not chain_pairs:
                    return None
                best = max(
                    chain_pairs,
                    key=lambda p: float(
                        p.get("liquidity", {}).get("usd", 0) or 0
                    )
                )
                price = float(best.get("priceUsd", 0) or 0)
                txns = best.get("txns", {})
                buys = int((txns.get("h1") or {}).get("buys", 0))
                sells = int((txns.get("h1") or {}).get("sells", 0))
                vol1h = float((best.get("volume") or {}).get("h1", 0) or 0)
                liq = float(
                    (best.get("liquidity") or {}).get("usd", 0) or 0
                )
                return {
                    "price": price,
                    "buys_1h": buys,
                    "sells_1h": sells,
                    "volume_1h": vol1h,
                    "liquidity_usd": liq,
                }
    except Exception as e:
        logger.warning("Fetch realtime %s: %s", address, e)
    return None


def update_price_history(address: str, data: dict):
    """Met à jour l'historique des prix/volumes pour un token en watchlist."""
    if address not in _watchlist:
        return
    now = datetime.now(timezone.utc)
    w = _watchlist[address]

    if data.get("price", 0) > 0:
        w["prices"].append({"time": now, "price": data["price"]})
    w["buys"].append({"time": now, "count": data.get("buys_1h", 0)})
    w["sells"].append({"time": now, "count": data.get("sells_1h", 0)})

    # Garder seulement les 30 dernières minutes
    cutoff = now - timedelta(minutes=30)
    w["prices"] = [p for p in w["prices"] if p["time"] > cutoff]
    w["buys"] = [b for b in w["buys"] if b["time"] > cutoff]
    w["sells"] = [s for s in w["sells"] if s["time"] > cutoff]


def compute_entry_signal(address: str) -> Optional[dict]:
    """
    Calcule si le moment est bon pour entrer sur ce token.

    Signal positif si :
    1. Prix monte sur les 5 dernières minutes (micro-momentum)
    2. Ratio acheteurs dominant (≥60% des txns)
    3. Volume en accélération

    Retourne un dict avec le signal et les paramètres TP/SL adaptatifs,
    ou None si pas de signal.
    """
    if address not in _watchlist:
        return None

    w = _watchlist[address]
    prices = w["prices"]
    buys_hist = w["buys"]
    sells_hist = w["sells"]

    # Pas assez d'historique
    if len(prices) < 3:
        return None

    now = datetime.now(timezone.utc)
    current_price = prices[-1]["price"]

    # ── 1. Micro-momentum : variation sur 5 minutes ───────────────────────
    cutoff_5m = now - timedelta(minutes=5)
    prices_5m = [p for p in prices if p["time"] > cutoff_5m]
    if len(prices_5m) < 2:
        return None

    oldest_5m_price = prices_5m[0]["price"]
    if oldest_5m_price <= 0:
        return None

    price_change_5m = ((current_price - oldest_5m_price) / oldest_5m_price) * 100

    if price_change_5m < ENTRY_MIN_PRICE_CHANGE_5M:
        return None

    # ── 2. Pression acheteuse ─────────────────────────────────────────────
    recent_buys = buys_hist[-1]["count"] if buys_hist else 0
    recent_sells = sells_hist[-1]["count"] if sells_hist else 0
    total_txns = recent_buys + recent_sells

    if total_txns > 0:
        buy_ratio = recent_buys / total_txns
    else:
        buy_ratio = 0.5

    if buy_ratio < ENTRY_MIN_BUY_RATIO:
        return None

    # ── 3. Accélération du volume ─────────────────────────────────────────
    # Pas bloquant si données insuffisantes, juste un bonus
    volume_accel = 1.0
    if len(prices) >= 6:
        # Compare volume récent vs volume moyen de la fenêtre
        recent_price_changes = [
            abs(prices[i]["price"] - prices[i-1]["price"]) / prices[i-1]["price"] * 100
            if prices[i-1]["price"] > 0 else 0
            for i in range(-3, 0)
        ]
        older_price_changes = [
            abs(prices[i]["price"] - prices[i-1]["price"]) / prices[i-1]["price"] * 100
            if prices[i-1]["price"] > 0 else 0
            for i in range(-6, -3)
        ]
        avg_recent = sum(recent_price_changes) / max(len(recent_price_changes), 1)
        avg_older = sum(older_price_changes) / max(len(older_price_changes), 1)
        volume_accel = avg_recent / max(avg_older, 0.01)

    # ── 4. Calcul TP/SL adaptatifs selon la force du signal ───────────────
    # Signal fort : momentum élevé + pression acheteuse forte
    # Signal modéré : conditions minimales remplies

    signal_strength = (
        (price_change_5m / 5.0) * 0.4 +      # contribution momentum
        ((buy_ratio - 0.5) / 0.5) * 0.4 +     # contribution buy ratio
        min(volume_accel / 3.0, 1.0) * 0.2    # contribution volume accel
    )
    signal_strength = max(0.0, min(1.0, signal_strength))

    if signal_strength >= 0.7:
        # Signal fort → TP ambitieux, SL large
        tp_pct = 25.0
        sl_pct = 10.0
        label = "FORT"
    elif signal_strength >= 0.4:
        # Signal modéré → TP/SL équilibrés
        tp_pct = 12.0
        sl_pct = 6.0
        label = "MODÉRÉ"
    else:
        # Signal faible → TP serré, sortie rapide
        tp_pct = 6.0
        sl_pct = 3.0
        label = "FAIBLE"

    return {
        "address": address,
        "symbol": w["symbol"],
        "chain": w["chain"],
        "current_price": current_price,
        "price_change_5m": price_change_5m,
        "buy_ratio": buy_ratio,
        "volume_accel": volume_accel,
        "signal_strength": round(signal_strength, 2),
        "signal_label": label,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "token_data": w["token_data"],
    }


def mark_entry(address: str):
    """Enregistre qu'un trade vient d'être ouvert sur ce token."""
    if address in _watchlist:
        _watchlist[address]["last_entry_at"] = datetime.now(timezone.utc)
        _watchlist[address]["entry_count"] += 1


def can_reenter(address: str, cooldown_minutes: int = 15) -> bool:
    """
    Vérifie si on peut re-rentrer sur ce token.
    Cooldown de 15 minutes entre deux entrées sur le même token.
    """
    if address not in _watchlist:
        return False
    last_entry = _watchlist[address].get("last_entry_at")
    if not last_entry:
        return True
    elapsed = (datetime.now(timezone.utc) - last_entry).total_seconds() / 60
    return elapsed >= cooldown_minutes
