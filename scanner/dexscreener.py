"""
Scanner DexScreener & GeckoTerminal — APIs 100% gratuites.
Retourne une liste de tokens candidats selon les critères de config.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import aiohttp

import config

logger = logging.getLogger(__name__)

DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex"
DEXSCREENER_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None) -> Optional[dict]:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning("HTTP %s for %s", resp.status, url)
            return None
    except Exception as e:
        logger.error("Fetch error %s: %s", url, e)
        return None


def _age_hours(pair_created_at_ms: Optional[int]) -> float:
    """Retourne l'âge du token en heures."""
    if not pair_created_at_ms:
        return 9999
    created = datetime.fromtimestamp(pair_created_at_ms / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def _parse_dexscreener_pair(pair: dict) -> Optional[dict]:
    """Convertit un pair DexScreener en token normalisé."""
    try:
        chain = pair.get("chainId", "").lower()
        if chain not in [c.lower() for c in config.TARGET_CHAINS]:
            return None

        price_change = pair.get("priceChange", {})
        volume = pair.get("volume", {})
        liquidity = pair.get("liquidity", {})
        txns = pair.get("txns", {})

        age_h = _age_hours(pair.get("pairCreatedAt"))

        h1 = float(price_change.get("h1", 0) or 0)
        h6 = float(price_change.get("h6", 0) or 0)
        h24 = float(price_change.get("h24", 0) or 0)
        liq_usd = float(liquidity.get("usd", 0) or 0)
        vol_24h = float(volume.get("h24", 0) or 0)
        vol_6h = float(volume.get("h6", 0) or 0)
        vol_1h = float(volume.get("h1", 0) or 0)
        price_usd = float(pair.get("priceUsd", 0) or 0)
        market_cap = float(pair.get("marketCap", 0) or 0)

        buys_1h = int((txns.get("h1") or {}).get("buys", 0))
        sells_1h = int((txns.get("h1") or {}).get("sells", 0))
        buys_24h = int((txns.get("h24") or {}).get("buys", 0))
        sells_24h = int((txns.get("h24") or {}).get("sells", 0))

        return {
            "source": "dexscreener",
            "chain": chain,
            "address": pair.get("baseToken", {}).get("address", ""),
            "name": pair.get("baseToken", {}).get("name", "Unknown"),
            "symbol": pair.get("baseToken", {}).get("symbol", "???"),
            "pair_address": pair.get("pairAddress", ""),
            "dex": pair.get("dexId", ""),
            "price_usd": price_usd,
            "price_change_1h": h1,
            "price_change_6h": h6,
            "price_change_24h": h24,
            "liquidity_usd": liq_usd,
            "volume_1h": vol_1h,
            "volume_6h": vol_6h,
            "volume_24h": vol_24h,
            "market_cap": market_cap,
            "age_hours": age_h,
            "buys_1h": buys_1h,
            "sells_1h": sells_1h,
            "buys_24h": buys_24h,
            "sells_24h": sells_24h,
            "buy_sell_ratio_1h": buys_1h / max(sells_1h, 1),
            "url": pair.get("url", ""),
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.debug("Parse error: %s", e)
        return None


async def scan_dexscreener_trending(session: aiohttp.ClientSession) -> list[dict]:
    """Récupère les tokens en tendance sur DexScreener pour les chains cibles."""
    tokens = []
    for chain in config.TARGET_CHAINS:
        url = f"{DEXSCREENER_BASE}/search"
        data = await fetch_json(session, url, params={"q": "meme"})
        if data and "pairs" in data:
            for pair in data["pairs"]:
                parsed = _parse_dexscreener_pair(pair)
                if parsed:
                    tokens.append(parsed)

    # Fetch top boosted tokens (endpoint valide DexScreener v1)
    boosted = await fetch_json(session, DEXSCREENER_BOOSTS_URL)
    if boosted and isinstance(boosted, list):
        addresses = [b.get("tokenAddress", "") for b in boosted if b.get("chainId", "").lower() in [c.lower() for c in config.TARGET_CHAINS]]
        if addresses:
            chunk = ",".join(addresses[:30])
            data2 = await fetch_json(session, f"{DEXSCREENER_BASE}/tokens/{chunk}")
            if data2 and "pairs" in data2:
                for pair in data2["pairs"]:
                    parsed = _parse_dexscreener_pair(pair)
                    if parsed:
                        tokens.append(parsed)

    # Deduplicate by address
    seen = set()
    unique = []
    for t in tokens:
        if t["address"] and t["address"] not in seen:
            seen.add(t["address"])
            unique.append(t)
    logger.info("DexScreener: %d unique tokens found", len(unique))
    return unique


async def scan_geckoterminal(session: aiohttp.ClientSession) -> list[dict]:
    """Récupère les trending pools GeckoTerminal pour les chains cibles."""
    chain_map = {"solana": "solana", "bsc": "bsc", "eth": "eth"}
    tokens = []

    for chain in config.TARGET_CHAINS:
        gt_chain = chain_map.get(chain.lower())
        if not gt_chain:
            continue

        url = f"{GECKOTERMINAL_BASE}/networks/{gt_chain}/trending_pools"
        data = await fetch_json(session, url, params={"page": 1})
        if not data or "data" not in data:
            continue

        for pool in data["data"]:
            try:
                attrs = pool.get("attributes", {})
                price_changes = attrs.get("price_change_percentage", {})
                volume = attrs.get("volume_usd", {})
                created_at_str = attrs.get("pool_created_at", "")

                if created_at_str:
                    created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                else:
                    age_h = 9999

                rel = pool.get("relationships", {})
                base_token = rel.get("base_token", {}).get("data", {})

                h1 = float(price_changes.get("h1", 0) or 0)
                h6 = float(price_changes.get("h6", 0) or 0)
                h24 = float(price_changes.get("h24", 0) or 0)
                vol_24h = float(volume.get("h24", 0) or 0)
                liq = float(attrs.get("reserve_in_usd", 0) or 0)

                tokens.append({
                    "source": "geckoterminal",
                    "chain": chain.lower(),
                    "address": base_token.get("id", "").split("_")[-1],
                    "name": attrs.get("name", "Unknown").split(" / ")[0],
                    "symbol": attrs.get("name", "???").split(" / ")[0],
                    "pair_address": attrs.get("address", ""),
                    "dex": "",
                    "price_usd": float(attrs.get("base_token_price_usd", 0) or 0),
                    "price_change_1h": h1,
                    "price_change_6h": h6,
                    "price_change_24h": h24,
                    "liquidity_usd": liq,
                    "volume_1h": float(volume.get("h1", 0) or 0),
                    "volume_6h": float(volume.get("h6", 0) or 0),
                    "volume_24h": vol_24h,
                    "market_cap": float(attrs.get("market_cap_usd", 0) or 0),
                    "age_hours": age_h,
                    "buys_1h": int(attrs.get("transactions", {}).get("h1", {}).get("buys", 0) or 0),
                    "sells_1h": int(attrs.get("transactions", {}).get("h1", {}).get("sells", 0) or 0),
                    "buys_24h": int(attrs.get("transactions", {}).get("h24", {}).get("buys", 0) or 0),
                    "sells_24h": int(attrs.get("transactions", {}).get("h24", {}).get("sells", 0) or 0),
                    "buy_sell_ratio_1h": 1.0,
                    "url": f"https://www.geckoterminal.com/{gt_chain}/pools/{attrs.get('address', '')}",
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:
                logger.debug("GeckoTerminal parse error: %s", e)

    logger.info("GeckoTerminal: %d tokens found", len(tokens))
    return tokens


async def scan_all() -> list[dict]:
    """Lance le scan complet (DexScreener + GeckoTerminal) en parallèle."""
    async with aiohttp.ClientSession(headers={"Accept": "application/json"}) as session:
        dex, gecko = await asyncio.gather(
            scan_dexscreener_trending(session),
            scan_geckoterminal(session),
            return_exceptions=True,
        )

    result = []
    if isinstance(dex, list):
        result.extend(dex)
    if isinstance(gecko, list):
        result.extend(gecko)

    # Deduplicate
    seen = set()
    unique = []
    for t in result:
        key = t.get("address") or t.get("pair_address")
        if key and key not in seen:
            seen.add(key)
            unique.append(t)

    logger.info("Total unique tokens after merge: %d", len(unique))
    return unique
