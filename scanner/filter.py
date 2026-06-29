"""
Filtres de qualité — profil momentum fort.
Inclut vérification blacklist.
"""
import logging
import config
from trader.paper_trader import is_blacklisted

logger = logging.getLogger(__name__)


def apply_filters(tokens: list[dict]) -> list[dict]:
    results = []
    stats = {
        "total": len(tokens),
        "too_young": 0,
        "too_old": 0,
        "low_liquidity": 0,
        "low_volume": 0,
        "not_rising": 0,
        "weak_momentum": 0,
        "blacklisted": 0,
        "scam_suspect": 0,
        "passed": 0,
    }

    for token in tokens:
        age = token.get("age_hours", 0)
        liq = token.get("liquidity_usd", 0)
        vol24 = token.get("volume_24h", 0)
        vol1h = token.get("volume_1h", 0)
        h1 = token.get("price_change_1h", 0)
        h24 = token.get("price_change_24h", 0)
        buys_1h = token.get("buys_1h", 0)
        sells_1h = token.get("sells_1h", 0)
        address = token.get("address", "")

        # ── Blacklist ────────────────────────────────────────────────────────
        if address and is_blacklisted(address):
            stats["blacklisted"] += 1
            logger.debug("Blacklisté: %s", token.get("symbol"))
            continue

        # ── Age filter ───────────────────────────────────────────────────────
        if age < config.MIN_AGE_HOURS:
            stats["too_young"] += 1
            continue
        if age > config.MAX_AGE_HOURS:
            stats["too_old"] += 1
            continue

        # ── Liquidity filter ─────────────────────────────────────────────────
        chain = token.get("chain", "solana").lower()
        min_liq = config.MIN_LIQUIDITY_PER_CHAIN.get(chain, config.MIN_LIQUIDITY_USD)
        if liq < min_liq:
            stats["low_liquidity"] += 1
            continue

        # ── Volume filter ────────────────────────────────────────────────────
        if vol24 < config.MIN_VOLUME_24H_USD:
            stats["low_volume"] += 1
            continue

        # ── Direction filter ─────────────────────────────────────────────────
        if h1 < config.MIN_PRICE_CHANGE_1H and h24 < config.MIN_PRICE_CHANGE_24H:
            stats["not_rising"] += 1
            continue

        # ── Momentum fort ────────────────────────────────────────────────────
        if h1 <= 0:
            stats["weak_momentum"] += 1
            continue

        if vol24 > 0 and vol1h / vol24 < 0.10:
            stats["weak_momentum"] += 1
            continue

        total_txns = buys_1h + sells_1h
        if total_txns > 0 and buys_1h / max(total_txns, 1) < 0.55:
            stats["weak_momentum"] += 1
            continue

        # ── Anti-scam ────────────────────────────────────────────────────────
        if _is_likely_scam(token):
            stats["scam_suspect"] += 1
            logger.debug("Scam suspect: %s (%s)", token.get("symbol"), address)
            continue

        stats["passed"] += 1
        results.append(token)

    logger.info(
        "Filter stats: total=%d, passed=%d, too_young=%d, too_old=%d, "
        "low_liq=%d, low_vol=%d, not_rising=%d, weak_momentum=%d, "
        "blacklisted=%d, scam=%d",
        stats["total"], stats["passed"], stats["too_young"], stats["too_old"],
        stats["low_liquidity"], stats["low_volume"], stats["not_rising"],
        stats["weak_momentum"], stats["blacklisted"], stats["scam_suspect"],
    )
    return results


def _is_likely_scam(token: dict) -> bool:
    liq = token.get("liquidity_usd", 0)
    vol24 = token.get("volume_24h", 0)
    h1 = token.get("price_change_1h", 0)
    h24 = token.get("price_change_24h", 0)
    buys_1h = token.get("buys_1h", 0)
    sells_1h = token.get("sells_1h", 0)
    market_cap = token.get("market_cap", 0)

    if buys_1h > 50 and sells_1h == 0:
        return True
    if liq > 0 and vol24 / liq > 100:
        return True
    if h1 > 500 and vol24 < 10000:
        return True
    if market_cap > 0 and liq > 0 and market_cap / liq > 1000:
        return True
    if h24 < -50:
        return True
    total_txns_1h = buys_1h + sells_1h
    if vol24 > 100000 and total_txns_1h < 5:
        return True

    return False
