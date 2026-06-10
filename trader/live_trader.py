"""
Module de trading réel via Jupiter DEX (Solana).
DÉSACTIVÉ par défaut — activé uniquement si TRADING_MODE=live.

⚠️  AVERTISSEMENT : Ce module exécute de vraies transactions avec de l'argent réel.
    Testez TOUJOURS en paper trading avant d'activer le mode live.
"""
import logging
import asyncio
from typing import Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

JUPITER_API = "https://quote-api.jup.ag/v6"
JUPITER_PRICE_API = "https://price.jup.ag/v4"


async def get_token_price_usd(token_mint: str) -> Optional[float]:
    """Récupère le prix d'un token en USD via Jupiter Price API."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{JUPITER_PRICE_API}/price",
                params={"ids": token_mint},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                price = data.get("data", {}).get(token_mint, {}).get("price")
                return float(price) if price else None
        except Exception as e:
            logger.error("Jupiter price error: %s", e)
            return None


async def get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = None,
) -> Optional[dict]:
    """Obtient un quote de swap Jupiter."""
    slippage_bps = slippage_bps or config.JUPITER_SLIPPAGE_BPS
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{JUPITER_API}/quote",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": amount_lamports,
                    "slippageBps": slippage_bps,
                },
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning("Jupiter quote error: HTTP %d", resp.status)
                return None
        except Exception as e:
            logger.error("Jupiter quote error: %s", e)
            return None


async def execute_swap(quote: dict, wallet_keypair) -> Optional[str]:
    """
    Exécute un swap via Jupiter.
    Retourne la signature de transaction ou None en cas d'erreur.

    ATTENTION : Nécessite solders ou solana-py installé.
    """
    try:
        # Import lazily pour éviter les erreurs si non installé
        from solders.keypair import Keypair  # type: ignore
        from solders.transaction import VersionedTransaction  # type: ignore
        import base64
    except ImportError:
        logger.error(
            "solders non installé. Installez avec: pip install solders\n"
            "Le trading live est désactivé sans cette dépendance."
        )
        return None

    async with aiohttp.ClientSession() as session:
        try:
            # Obtenir la transaction de swap
            payload = {
                "quoteResponse": quote,
                "userPublicKey": str(wallet_keypair.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            }
            async with session.post(
                f"{JUPITER_API}/swap",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    logger.error("Jupiter swap error: HTTP %d", resp.status)
                    return None
                swap_data = await resp.json()

            # Signer et envoyer la transaction
            swap_tx_bytes = base64.b64decode(swap_data["swapTransaction"])
            tx = VersionedTransaction.from_bytes(swap_tx_bytes)
            # TODO: Implémenter la signature et l'envoi via RPC Solana
            # tx.sign([wallet_keypair])
            # signature = await send_transaction(tx)
            logger.warning("Live trading: signature non implémentée — transaction non envoyée")
            return None

        except Exception as e:
            logger.error("Execute swap error: %s", e)
            return None


class LiveTrader:
    """
    Trader réel pour Solana via Jupiter DEX.
    Activé uniquement si TRADING_MODE=live ET SOLANA_PRIVATE_KEY configurée.
    """

    def __init__(self):
        if config.TRADING_MODE != "live":
            raise RuntimeError("LiveTrader instancié mais TRADING_MODE != 'live'")
        if not config.SOLANA_PRIVATE_KEY:
            raise RuntimeError("SOLANA_PRIVATE_KEY non configurée")

        try:
            from solders.keypair import Keypair  # type: ignore
            import base58  # type: ignore
            key_bytes = base58.b58decode(config.SOLANA_PRIVATE_KEY)
            self.keypair = Keypair.from_bytes(key_bytes)
            logger.info("🔐 Wallet live: %s", str(self.keypair.pubkey())[:8] + "...")
        except ImportError:
            raise RuntimeError("solders/base58 non installés pour le trading live")

    async def buy(self, token_mint: str, amount_usd: float) -> Optional[str]:
        """Achète un token avec amount_usd USD (via SOL)."""
        logger.warning("⚠️  BUY LIVE: %s pour $%.2f", token_mint, amount_usd)
        # TODO: Convertir USD → lamports, obtenir quote, exécuter swap
        return None

    async def sell(self, token_mint: str, amount_tokens: float) -> Optional[str]:
        """Vend amount_tokens tokens contre SOL."""
        logger.warning("⚠️  SELL LIVE: %s — %.6f tokens", token_mint, amount_tokens)
        return None
