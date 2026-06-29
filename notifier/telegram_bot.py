"""
Bot Telegram — notifications temps réel + commandes interactives.
"""
import logging
from datetime import datetime, timezone
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
_trader_ref = None  # référence au trader pour accéder aux prix


def set_trader(trader):
    """Appelé depuis main.py pour donner accès au trader."""
    global _trader_ref
    _trader_ref = trader


def is_paused() -> bool:
    return _paused


def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat:
            return
        caller_id = str(update.effective_chat.id)
        allowed_id = str(config.TELEGRAM_CHAT_ID).strip()
        if not allowed_id:
            return
        if caller_id != allowed_id:
            logger.warning("Commande non autorisée de %s", caller_id)
            return
        return await func(update, context)
    return wrapper


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


async def _fetch_current_prices(addresses: list[str]) -> dict[str, float]:
    """Fetch les prix live depuis DexScreener pour /positions."""
    prices = {}
    if not addresses:
        return prices
    try:
        chunk = ",".join(addresses[:30])
        url = f"https://api.dexscreener.com/latest/dex/tokens/{chunk}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for pair in data.get("pairs", []):
                        addr = pair.get("baseToken", {}).get("address", "")
                        price = float(pair.get("priceUsd", 0) or 0)
                        if addr and price > 0 and addr not in prices:
                            prices[addr] = price
    except Exception as e:
        logger.warning("Erreur fetch prix /positions: %s", e)
    return prices


# ── Messages formatés ─────────────────────────────────────────────────────────

async def notify_signal(token: dict, score: int):
    chain_emoji = {"solana": "◎", "bsc": "🟡", "eth": "🔷"}.get(token.get("chain", ""), "🔗")
    text = (
        f"🎯 <b>SIGNAL DÉTECTÉ</b> {chain_emoji}\n\n"
        f"<b>{token['name']}</b> (<code>{token['symbol']}</code>)\n"
        f"Score IA: {score}/100 {_score_bar(score)}\n\n"
        f"💰 Prix: <b>${token.get('price_usd', 0):.8f}</b>\n"
        f"📊 Variation: 1h <b>{token.get('price_change_1h', 0):+.1f}%</b> | "
        f"24h <b>{token.get('price_change_24h', 0):+.1f}%</b>\n"
        f"💧 Liquidité: <b>${token.get('liquidity_usd', 0):,.0f}</b>\n"
        f"📈 Volume 24h: <b>${token.get('volume_24h', 0):,.0f}</b>\n"
        f"⏱ Âge: <b>{token.get('age_hours', 0):.0f}h</b>\n\n"
        f"<a href='{token.get('url', '#')}'>Voir sur DexScreener</a>"
    )
    await send(text)


async def notify_trade_open(trade: dict):
    mode = "📋 PAPER" if config.TRADING_MODE == "paper" else "💸 LIVE"
    text = (
        f"{mode} <b>TRADE OUVERT</b> #{trade['id']}\n\n"
        f"Token: <b>{trade['symbol']}</b>\n"
        f"Entrée: <b>${trade['entry_price']:.8f}</b>\n"
        f"Taille: <b>${trade['position_usd']:.2f}</b>\n"
        f"Score IA: <b>{trade['ai_score']}/100</b>\n\n"
        f"🎯 TP: +30% | 🛑 SL: -10% | ⏰ 24h"
    )
    await send(text)


async def notify_trade_close(trade: dict):
    pnl = trade.get("pnl_usd", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    reason = trade.get("reason", trade.get("close_reason", ""))
    emoji = "🟢" if pnl > 0 else "🔴"
    reason_labels = {
        "take_profit": "✅ Take Profit",
        "stop_loss": "🛑 Stop Loss",
        "timeout": "⏰ Timeout",
        "manual": "👤 Manuel",
    }
    text = (
        f"{emoji} <b>TRADE FERMÉ</b> #{trade.get('id', '?')}\n\n"
        f"Token: <b>{trade.get('token_symbol', '?')}</b>\n"
        f"Raison: <b>{reason_labels.get(reason, reason)}</b>\n"
        f"PnL: <b>{'+' if pnl > 0 else ''}{pnl:.2f} USD ({pnl_pct:+.2f}%)</b>\n\n"
        f"Entrée: ${trade.get('entry_price', 0):.8f}\n"
        f"Sortie: ${trade.get('exit_price', 0):.8f}"
    )
    await send(text)


async def notify_daily_report(report: dict):
    stats = report.get("stats", {})
    pnl = stats.get("total_pnl_usd", 0)
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    text = (
        f"📊 <b>RAPPORT QUOTIDIEN</b> — {report.get('date', '')}\n"
        f"{'─' * 30}\n\n"
        f"{pnl_emoji} PnL: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USD</b>\n"
        f"🏆 Win Rate: <b>{stats.get('win_rate', 0):.1f}%</b> "
        f"({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)\n"
        f"📋 Trades: <b>{stats.get('total_trades', 0)}</b> total | "
        f"<b>{stats.get('open_positions', 0)}</b> ouverts\n\n"
        f"🔥 Meilleur: <b>{stats.get('best_trade_pct', 0) or 0:+.1f}%</b>\n"
        f"💔 Pire: <b>{stats.get('worst_trade_pct', 0) or 0:+.1f}%</b>\n\n"
        f"🤖 Modèle: <b>{report.get('model_mode', 'heuristique')}</b>"
    )
    await send(text)


# ── Commandes ─────────────────────────────────────────────────────────────────

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Meme Coin Bot actif!</b>\n\n"
        "/status — État du bot\n"
        "/positions — Positions + PnL live\n"
        "/pnl — Performances\n"
        "/pause — Mettre en pause\n"
        "/resume — Reprendre\n"
        "/help — Aide",
        parse_mode=ParseMode.HTML,
    )


@authorized_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_portfolio_stats()
    model_mode = "ML entraîné" if _get_model_trained() else "Heuristique"
    status_emoji = "⏸" if _paused else "▶️"
    text = (
        f"{status_emoji} <b>STATUT BOT</b>\n\n"
        f"Mode: <b>{config.TRADING_MODE.upper()}</b>\n"
        f"État: <b>{'En pause' if _paused else 'Actif'}</b>\n"
        f"IA: <b>{model_mode}</b>\n\n"
        f"Portfolio: <b>${config.INITIAL_PORTFOLIO + (stats.get('total_pnl_usd') or 0):.2f}</b>\n"
        f"PnL total: <b>{'+' if (stats.get('total_pnl_usd') or 0) >= 0 else ''}"
        f"{(stats.get('total_pnl_usd') or 0):.2f} USD</b>\n"
        f"Positions: <b>{stats.get('open_positions', 0)}/{config.MAX_OPEN_POSITIONS}</b>\n\n"
        f"Stratégie: TP +30% | SL -10% | ⏰ 24h"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche les positions avec PnL temps réel."""
    open_trades = db.get_open_trades()
    if not open_trades:
        await update.message.reply_text("Aucune position ouverte actuellement.")
        return

    await update.message.reply_text("⏳ Récupération des prix en cours...")

    # Fetch prix live
    addresses = [t["token_address"] for t in open_trades]
    prices = await _fetch_current_prices(addresses)

    lines = ["📋 <b>POSITIONS OUVERTES</b>\n"]
    total_pnl_usd = 0.0

    for t in open_trades:
        addr = t["token_address"]
        entry = t["entry_price"]
        current = prices.get(addr, 0)

        if current > 0 and entry > 0:
            pnl_pct = ((current - entry) / entry) * 100
            pnl_usd = t["position_usd"] * (pnl_pct / 100)
            pnl_emoji = "🟢" if pnl_pct > 0 else "🔴"
            pnl_str = f"{pnl_emoji} <b>{pnl_pct:+.1f}% ({'+' if pnl_usd > 0 else ''}{pnl_usd:.2f}$)</b>"
        else:
            pnl_str = "⚪ Prix indisponible"
            pnl_usd = 0.0

        total_pnl_usd += pnl_usd

        # Temps restant avant timeout
        open_at = datetime.fromisoformat(t["open_at"])
        if open_at.tzinfo is None:
            open_at = open_at.replace(tzinfo=timezone.utc)
        elapsed_h = (datetime.now(timezone.utc) - open_at).total_seconds() / 3600
        remaining_h = max(0, 24 - elapsed_h)

        lines.append(
            f"• <b>{t['token_symbol']}</b> #{t['id']}\n"
            f"  Entrée: ${entry:.8f}\n"
            f"  Actuel: ${current:.8f if current > 0 else '—'}\n"
            f"  PnL: {pnl_str}\n"
            f"  Taille: ${t['position_usd']:.2f} | ⏰ {remaining_h:.0f}h restantes\n"
        )

    total_emoji = "🟢" if total_pnl_usd > 0 else "🔴"
    lines.append(
        f"\n{total_emoji} <b>PnL total non réalisé: "
        f"{'+' if total_pnl_usd > 0 else ''}{total_pnl_usd:.2f}$</b>"
    )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_portfolio_stats()
    pnl = stats.get("total_pnl_usd", 0) or 0
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    recent = db.get_closed_trades(limit=5)
    recent_lines = [
        f"  {'🟢' if (t.get('pnl_pct') or 0) > 0 else '🔴'} "
        f"{t['token_symbol']}: {(t.get('pnl_pct') or 0):+.1f}%"
        for t in recent
    ]
    text = (
        f"{pnl_emoji} <b>PERFORMANCES</b>\n\n"
        f"PnL réalisé: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USD</b>\n"
        f"Win Rate: <b>{stats.get('win_rate', 0):.1f}%</b>\n"
        f"Total trades: <b>{stats.get('total_trades', 0)}</b>\n"
        f"Meilleur: <b>{stats.get('best_trade_pct', 0) or 0:+.1f}%</b>\n"
        f"Pire: <b>{stats.get('worst_trade_pct', 0) or 0:+.1f}%</b>\n\n"
        f"<b>5 derniers trades:</b>\n"
        + ("\n".join(recent_lines) if recent_lines else "  Aucun")
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = True
    await update.message.reply_text("⏸ Bot en <b>pause</b>.", parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = False
    await update.message.reply_text("▶️ Bot <b>repris</b>.", parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>AIDE</b>\n\n"
        "/start — Message d'accueil\n"
        "/status — État général\n"
        "/positions — Positions + PnL live\n"
        "/pnl — Résultats réalisés\n"
        "/pause — Suspendre les trades\n"
        "/resume — Reprendre\n"
        "/help — Cette aide\n\n"
        f"Mode: <b>{config.TRADING_MODE.upper()}</b>\n"
        f"Score min: <b>{config.MIN_SCORE_TO_TRADE}/100</b>\n"
        f"Stratégie: TP +30% | SL -10% | ⏰ 24h"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _score_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _get_model_trained() -> bool:
    try:
        from analyzer.model import get_model
        return get_model().is_trained
    except Exception:
        return False
