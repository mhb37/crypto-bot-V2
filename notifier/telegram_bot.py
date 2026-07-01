"""
Bot Telegram — notifications temps réel + commandes interactives.
Inclut : /close, /alert, /alerts, /blacklist, /historique,
rapport hebdo, alerte BTC, analyse post-trade, TP/SL adaptatifs.
"""
import logging
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import aiohttp

import config
import database as db

logger = logging.getLogger(__name__)

_app: Optional[Application] = None
_bot: Optional[Bot] = None
_paused = False
_trader_ref = None
_alerts: dict[str, float] = {}
_alert_symbols: dict[str, str] = {}


def set_trader(trader):
    global _trader_ref
    _trader_ref = trader


def is_paused() -> bool:
    return _paused


# ── Décorateur sécurité ───────────────────────────────────────────────────────

def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        caller_id = str(update.effective_chat.id)
        allowed_id = str(config.TELEGRAM_CHAT_ID).strip()
        if not allowed_id or caller_id != allowed_id:
            return
        return await func(update, context)
    return wrapper


# ── Init ──────────────────────────────────────────────────────────────────────

async def init_telegram() -> bool:
    global _app, _bot
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN non configuré")
        return False
    try:
        _bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
        info = await _bot.get_me()
        logger.info("Bot Telegram connecté: @%s", info.username)
        _app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        _app.add_handler(CommandHandler("start", cmd_start))
        _app.add_handler(CommandHandler("status", cmd_status))
        _app.add_handler(CommandHandler("positions", cmd_positions))
        _app.add_handler(CommandHandler("pnl", cmd_pnl))
        _app.add_handler(CommandHandler("close", cmd_close))
        _app.add_handler(CommandHandler("alert", cmd_alert))
        _app.add_handler(CommandHandler("alerts", cmd_alerts))
        _app.add_handler(CommandHandler("blacklist", cmd_blacklist))
        _app.add_handler(CommandHandler("historique", cmd_historique))
        _app.add_handler(CommandHandler("watchlist", cmd_watchlist))
        _app.add_handler(CommandHandler("pause", cmd_pause))
        _app.add_handler(CommandHandler("resume", cmd_resume))
        _app.add_handler(CommandHandler("help", cmd_help))
        return True
    except Exception as e:
        logger.error("Erreur init Telegram: %s", e)
        return False


async def start_polling():
    if _app:
        await _app.initialize()
        await _app.start()
        await _app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram polling démarré")


async def stop_polling():
    if _app and _app.updater.running:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()


async def send(text: str, parse_mode: str = ParseMode.HTML) -> bool:
    if not _bot or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        await _bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error("Erreur envoi Telegram: %s", e)
        return False


# ── Fetch prix ────────────────────────────────────────────────────────────────

async def _fetch_current_prices(open_trades: list[dict]) -> dict[str, float]:
    prices = {}
    if not open_trades:
        return prices
    try:
        async with aiohttp.ClientSession(
            headers={"Accept": "application/json"}
        ) as session:
            for trade in open_trades:
                addr = trade["token_address"]
                chain = trade.get("chain", "solana")
                url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status != 200:
                            continue
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
                            if price > 0:
                                prices[addr] = price
                except Exception as e:
                    logger.warning(
                        "Erreur prix %s: %s", trade.get("token_symbol"), e
                    )
    except Exception as e:
        logger.warning("Erreur session: %s", e)
    return prices


async def _fetch_btc_change_1h() -> Optional[float]:
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.dexscreener.com/latest/dex/search"
            async with session.get(
                url,
                params={"q": "WBTC USDC"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                pairs = data.get("pairs", [])
                btc_pairs = [
                    p for p in pairs
                    if p.get("baseToken", {}).get("symbol", "").upper()
                    in ("WBTC", "BTC")
                    and p.get("quoteToken", {}).get("symbol", "").upper()
                    in ("USDC", "USDT", "USD")
                ]
                if btc_pairs:
                    best = max(
                        btc_pairs,
                        key=lambda p: float(
                            p.get("liquidity", {}).get("usd", 0) or 0
                        )
                    )
                    return float(
                        best.get("priceChange", {}).get("h1", 0) or 0
                    )
    except Exception as e:
        logger.warning("Erreur fetch BTC: %s", e)
    return None


# ── Notifications ─────────────────────────────────────────────────────────────

async def notify_signal(token: dict, score: int):
    chain_emoji = {
        "solana": "◎", "bsc": "🟡", "eth": "🔷"
    }.get(token.get("chain", ""), "🔗")
    text = (
        f"🎯 <b>SIGNAL DÉTECTÉ</b> {chain_emoji}\n\n"
        f"<b>{token['name']}</b> (<code>{token['symbol']}</code>)\n"
        f"Score IA: {score}/100 {_score_bar(score)}\n\n"
        f"💰 Prix: <b>${token.get('price_usd', 0):.8f}</b>\n"
        f"📊 1h <b>{token.get('price_change_1h', 0):+.1f}%</b> | "
        f"6h <b>{token.get('price_change_6h', 0):+.1f}%</b> | "
        f"24h <b>{token.get('price_change_24h', 0):+.1f}%</b>\n"
        f"💧 Liquidité: <b>${token.get('liquidity_usd', 0):,.0f}</b>\n"
        f"📈 Volume 24h: <b>${token.get('volume_24h', 0):,.0f}</b>\n"
        f"⏱ Âge: <b>{token.get('age_hours', 0):.0f}h</b>\n\n"
        f"➕ Token ajouté à la watchlist active\n\n"
        f"<a href='{token.get('url', '#')}'>Voir sur DexScreener</a>"
    )
    await send(text)


async def notify_trade_open(trade: dict):
    mode = "📋 PAPER" if config.TRADING_MODE == "paper" else "💸 LIVE"
    tp = trade.get("tp_pct", 15)
    sl = trade.get("sl_pct", 7)
    signal_label = trade.get("signal_label", "STANDARD")
    label_emoji = {
        "FORT": "🔥", "MODÉRÉ": "⚡", "FAIBLE": "💧", "STANDARD": "📊"
    }.get(signal_label, "📊")
    text = (
        f"{mode} <b>TRADE OUVERT</b> #{trade['id']}\n\n"
        f"Token: <b>{trade['symbol']}</b>\n"
        f"Signal: {label_emoji} <b>{signal_label}</b>\n"
        f"Entrée: <b>${trade['entry_price']:.8f}</b>\n"
        f"Taille: <b>${trade['position_usd']:.2f}</b>\n"
        f"Score IA: <b>{trade['ai_score']}/100
