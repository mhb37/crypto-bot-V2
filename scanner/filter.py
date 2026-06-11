"""
Filtres de qualité — élimine les scams évidents et tokens hors critères.
"""
import logging
import config

logger = logging.getLogger(__name__)


def apply_filters(tokens: list[dict]) -> list[dict]:
    """Applique tous les filtres et retourne les tokens valides."""
    results = []
    stats = {
        "total": len(tokens),
        "too_young": 0,
        "too_old": 0,
        "low_liquidity": 0,
        "low_volume": 0,
        "not_rising": 0,
        "scam_suspect": 0,
        "passed": 0,
    }

    for token in tokens:
        age = token.get("age_hours", 0)
        liq = token.get("liquidity_usd", 0)
        vol24 = token.get("volume_24h", 0)
        h1 = token.get("price_change_1h", 0)
        h24 = token.get("price_change_24h", 0)

        # ── Age filter ──────────────────────────────────────────────────────
        if age < config.MIN_AGE_HOURS:
            stats["too_young"] += 1
            continue
        if age > config.MAX_AGE_HOURS:
            stats["too_old"] += 1
            continue

        # ── Liquidity filter (seuil par chain) ───────────────────────────────
        chain = token.get("chain", "solana").lower()
        min_liq = config.MIN_LIQUIDITY_PER_CHAIN.get(chain, config.MIN_LIQUIDITY_USD)
        if liq < min_liq:
            stats["low_liquidity"] += 1
            continue

        # ── Volume filter ────────────────────────────────────────────────────
        if vol24 < config.MIN_VOLUME_24H_USD:
            stats["low_volume"] += 1
            continue

        # ── Price direction filter ───────────────────────────────────────────
        if h1 < config.MIN_PRICE_CHANGE_1H and h24 < config.MIN_PRICE_CHANGE_24H:
            stats["not_rising"] += 1
            continue

        # ── Anti-scam heuristics ─────────────────────────────────────────────
        if _is_likely_scam(token):
            stats["scam_suspect"] += 1
            logger.debug("Scam suspect: %s (%s)", token.get("symbol"), token.get("address"))
            continue

        stats["passed"] += 1
        results.append(token)

    logger.info(
        "Filter stats: total=%d, passed=%d, too_young=%d, too_old=%d, "
        "low_liq=%d, low_vol=%d, not_rising=%d, scam=%d",
        stats["total"], stats["passed"], stats["too_young"], stats["too_old"],
        stats["low_liquidity"], stats["low_volume"], stats["not_rising"], stats["scam_suspect"],
    )
    return results


def _is_likely_scam(token: dict) -> bool:
    """
    Heuristiques anti-scam (pas infaillibles mais filtrent les cas évidents).
    Retourne True si le token est probablement un scam.
    """
    liq = token.get("liquidity_usd", 0)
    vol24 = token.get("volume_24h", 0)
    h1 = token.get("price_change_1h", 0)
    h24 = token.get("price_change_24h", 0)
    buys_1h = token.get("buys_1h", 0)
    sells_1h = token.get("sells_1h", 0)
    market_cap = token.get("market_cap", 0)

    # ── Honeypot heuristic : beaucoup d'achats, quasi aucune vente ───────────
    if buys_1h > 50 and sells_1h == 0:
        return True

    # ── Volume/Liquidity ratio anormal (wash trading) ─────────────────────────
    if liq > 0 and vol24 / liq > 100:
        return True

    # ── Pump extrême non justifié ────────────────────────────────────────────
    if h1 > 500 and vol24 < 10000:
        return True

    # ── Market cap irréaliste vs liquidité ───────────────────────────────────
    if market_cap > 0 and liq > 0 and market_cap / liq > 1000:
        return True

    # ── Crash récent (-50% 24h) → probablement post-dump ────────────────────
    if h24 < -50:
        return True

    # ── Pas assez de transactions (fausse activité) ──────────────────────────
    total_txns_1h = buys_1h + sells_1h
    if vol24 > 100000 and total_txns_1h < 5:
        return True

    return False
