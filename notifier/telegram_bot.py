"""
Bot Telegram — notifications temps réel + commandes interactives.
Inclut : /close, /alert, /alerts, /blacklist, /historique,
rapport hebdo, alerte BTC, analyse post-trade.
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
        f"🎯 TP1: +20% (50%) | TP2: +40% (100%)\n"
        f"🛑 SL: -15% | Trailing: actif à +15%\n"
        f"⏰ Timeout: 24h"
    )
    await send(text)


async def notify_trade_close(trade: dict):
    pnl = trade.get("pnl_usd", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    reason = trade.get("reason", trade.get("close_reason", ""))
    partial = trade.get("partial", False)
    emoji = "🟢" if pnl > 0 else "🔴"
    reason_labels = {
        "take_profit":   "✅ Take Profit (TP2 +40%)",
        "tp1_partial":   "🟡 TP1 partiel (+20% — 50% fermé)",
        "trailing_stop": "📉 Trailing Stop",
        "stop_loss":     "🛑 Stop Loss",
        "timeout":       "⏰ Timeout 24h",
        "manual":        "👤 Fermeture manuelle",
    }
    if partial:
        text = (
            f"🟡 <b>TP1 PARTIEL</b> #{trade.get('id', '?')}\n\n"
            f"Token: <b>{trade.get('token_symbol', '?')}</b>\n"
            f"50% fermé à: <b>${trade.get('exit_price', 0):.8f}</b>\n"
            f"PnL partiel: <b>+{pnl:.2f} USD ({pnl_pct:+.2f}%)</b>\n\n"
            f"Le reste continue jusqu'à TP2 (+40%) ou SL/Trailing."
        )
    else:
        text = (
            f"{emoji} <b>TRADE FERMÉ</b> #{trade.get('id', '?')}\n\n"
            f"Token: <b>{trade.get('token_symbol', '?')}</b>\n"
            f"Raison: <b>{reason_labels.get(reason, reason)}</b>\n"
            f"PnL: <b>{'+' if pnl > 0 else ''}"
            f"{pnl:.2f} USD ({pnl_pct:+.2f}%)</b>\n\n"
            f"Entrée: ${trade.get('entry_price', 0):.8f}\n"
            f"Sortie: ${trade.get('exit_price', 0):.8f}"
        )
    await send(text)


async def notify_post_trade_analysis(result: dict):
    """Analyse post-trade envoyée 2h après la fermeture."""
    trade = result.get("trade", {})
    close_price = result.get("close_price", 0)
    final_price = result.get("final_price", 0)
    snapshots = result.get("snapshots", [])
    symbol = trade.get("token_symbol", "?")
    entry = trade.get("entry_price", 0)
    close_reason = trade.get("close_reason", "?")

    if close_price <= 0:
        return

    evolution_pct = (
        (final_price - close_price) / close_price * 100
    ) if close_price > 0 else 0
    evolution_depuis_entree = (
        (final_price - entry) / entry * 100
    ) if entry > 0 else 0

    emoji = "📈" if evolution_pct > 0 else "📉"

    reason_labels = {
        "stop_loss":     "🛑 Stop Loss",
        "trailing_stop": "📉 Trailing Stop",
        "take_profit":   "✅ Take Profit",
        "timeout":       "⏰ Timeout",
        "manual":        "👤 Manuel",
    }

    if close_reason == "stop_loss":
        if evolution_pct > 10:
            verdict = "⚠️ <b>SL trop serré</b> — le token a rebondi après la sortie"
        elif evolution_pct < -5:
            verdict = "✅ <b>SL correct</b> — le token a continué de chuter"
        else:
            verdict = "➡️ <b>SL neutre</b> — le token a stagné après la sortie"
    elif close_reason in ("trailing_stop", "take_profit"):
        if evolution_pct > 15:
            verdict = "⚠️ <b>Sortie trop tôt</b> — le token a continué de monter"
        elif evolution_pct < -10:
            verdict = "✅ <b>Sortie au bon moment</b> — le token a chuté après"
        else:
            verdict = "➡️ <b>Sortie correcte</b> — le token a stagné"
    else:
        verdict = ""

    snap_lines = ""
    if snapshots:
        snap_lines = "\n<b>Évolution :</b>\n"
        for s in snapshots:
            pct = (
                (s["price"] - close_price) / close_price * 100
            ) if close_price > 0 else 0
            snap_lines += f"  +{s['minutes']}min: ${s['price']:.8f} ({pct:+.1f}%)\n"

    text = (
        f"🔍 <b>ANALYSE POST-TRADE — {symbol}</b>\n\n"
        f"Raison: <b>{reason_labels.get(close_reason, close_reason)}</b>\n"
        f"Entrée: ${entry:.8f}\n"
        f"Fermeture: ${close_price:.8f}\n"
        f"Prix 2h après: ${final_price:.8f}\n\n"
        f"{emoji} Évolution après fermeture: <b>{evolution_pct:+.1f}%</b>\n"
        f"Par rapport à l'entrée: <b>{evolution_depuis_entree:+.1f}%</b>\n"
        f"{snap_lines}\n"
        f"{verdict}"
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
        f"📋 Trades: <b>{stats.get('total_trades', 0)}</b> | "
        f"<b>{stats.get('open_positions', 0)}</b> ouverts\n\n"
        f"🔥 Meilleur: <b>{stats.get('best_trade_pct', 0) or 0:+.1f}%</b>\n"
        f"💔 Pire: <b>{stats.get('worst_trade_pct', 0) or 0:+.1f}%</b>\n\n"
        f"🤖 Modèle: <b>{report.get('model_mode', 'heuristique')}</b>"
    )
    await send(text)


async def notify_weekly_report(stats: dict):
    pnl = stats.get("total_pnl_usd", 0)
    pnl_emoji = "📈" if pnl >= 0 else "📉"
    text = (
        f"📅 <b>RAPPORT HEBDOMADAIRE</b>\n"
        f"{'─' * 30}\n\n"
        f"{pnl_emoji} PnL semaine: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} USD</b>\n"
        f"🏆 Win Rate: <b>{stats.get('win_rate', 0):.1f}%</b> "
        f"({stats.get('wins', 0)}W / {stats.get('losses', 0)}L)\n"
        f"📋 Total trades: <b>{stats.get('total_trades', 0)}</b>\n\n"
        f"🔥 Meilleur: <b>{stats.get('best_trade_pct', 0) or 0:+.1f}%</b>\n"
        f"💔 Pire: <b>{stats.get('worst_trade_pct', 0) or 0:+.1f}%</b>\n"
        f"📊 Moy/trade: <b>{stats.get('avg_pnl_pct', 0) or 0:+.1f}%</b>"
    )
    await send(text)


async def check_btc_and_alert() -> Optional[float]:
    change = await _fetch_btc_change_1h()
    if change is not None and change <= -3.0:
        await send(
            f"⚠️ <b>ALERTE BTC</b>\n\n"
            f"BTC chute de <b>{change:.1f}%</b> sur 1h.\n"
            f"Les memecoins risquent de suivre — soyez prudent.\n"
            f"Le bot continue de scanner mais restez vigilant."
        )
    return change


async def check_price_alerts(current_prices: dict[str, float]):
    triggered = []
    for addr, target in list(_alerts.items()):
        current = current_prices.get(addr)
        if current and current >= target:
            symbol = _alert_symbols.get(addr, addr[:8])
            await send(
                f"🔔 <b>ALERTE PRIX</b>\n\n"
                f"<b>{symbol}</b> a atteint <b>${current:.8f}</b>\n"
                f"Cible: ${target:.8f}"
            )
            triggered.append(addr)
    for addr in triggered:
        _alerts.pop(addr, None)
        _alert_symbols.pop(addr, None)


# ── Commandes ─────────────────────────────────────────────────────────────────

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Meme Coin Bot actif!</b>\n\n"
        "/status — État du bot\n"
        "/positions — Positions + PnL live\n"
        "/pnl — Performances\n"
        "/historique — Historique complet\n"
        "/close <code>id</code> — Fermer une position\n"
        "/alert <code>adresse</code> <code>prix</code> — Alerte prix\n"
        "/alerts — Alertes actives\n"
        "/blacklist — Tokens en blacklist\n"
        "/pause — Mettre en pause\n"
        "/resume — Reprendre\n"
        "/help — Aide complète",
        parse_mode=ParseMode.HTML,
    )


@authorized_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_portfolio_stats()
    model_mode = "ML entraîné" if _get_model_trained() else "Heuristique"
    status_emoji = "⏸" if _paused else "▶️"
    bl_count = len(_trader_ref.get_blacklist()) if _trader_ref else 0
    text = (
        f"{status_emoji} <b>STATUT BOT</b>\n\n"
        f"Mode: <b>{config.TRADING_MODE.upper()}</b>\n"
        f"État: <b>{'En pause' if _paused else 'Actif'}</b>\n"
        f"IA: <b>{model_mode}</b>\n\n"
        f"Portfolio: <b>"
        f"${config.INITIAL_PORTFOLIO + (stats.get('total_pnl_usd') or 0):.2f}"
        f"</b>\n"
        f"PnL total: <b>"
        f"{'+' if (stats.get('total_pnl_usd') or 0) >= 0 else ''}"
        f"{(stats.get('total_pnl_usd') or 0):.2f} USD</b>\n"
        f"Positions: <b>{stats.get('open_positions', 0)}"
        f"/{config.MAX_OPEN_POSITIONS}</b>\n"
        f"Blacklist: <b>{bl_count} token(s)</b>\n\n"
        f"TP1: +20% (50%) | TP2: +40% | SL: -15%\n"
        f"Trailing: actif à +15% | ⏰ 24h"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_trades = db.get_open_trades()
    if not open_trades:
        await update.message.reply_text("Aucune position ouverte actuellement.")
        return

    await update.message.reply_text("⏳ Récupération des prix en cours...")
    prices = await _fetch_current_prices(open_trades)
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
            pnl_str = (
                f"{pnl_emoji} <b>{pnl_pct:+.1f}% "
                f"({'+' if pnl_usd > 0 else ''}{pnl_usd:.2f}$)</b>"
            )
        else:
            pnl_str = "⚪ Prix indisponible"
            pnl_usd = 0.0

        total_pnl_usd += pnl_usd

        open_at = datetime.fromisoformat(t["open_at"])
        if open_at.tzinfo is None:
            open_at = open_at.replace(tzinfo=timezone.utc)
        elapsed_h = (
            datetime.now(timezone.utc) - open_at
        ).total_seconds() / 3600
        remaining_h = max(0, 24 - elapsed_h)
        current_str = f"${current:.8f}" if current > 0 else "—"

        lines.append(
            f"• <b>{t['token_symbol']}</b> #{t['id']}\n"
            f"  Entrée: ${entry:.8f}\n"
            f"  Actuel: {current_str}\n"
            f"  PnL: {pnl_str}\n"
            f"  Taille: ${t['position_usd']:.2f} | ⏰ {remaining_h:.0f}h\n"
            f"  <i>/close {t['id']} pour fermer</i>\n"
        )

    total_emoji = "🟢" if total_pnl_usd > 0 else "🔴"
    lines.append(
        f"\n{total_emoji} <b>PnL total non réalisé: "
        f"{'+' if total_pnl_usd > 0 else ''}{total_pnl_usd:.2f}$</b>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_historique(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Historique complet : 20 derniers trades fermés + positions ouvertes
    + 10 derniers signaux non tradés. Conçu pour le diagnostic.
    """
    messages = []

    # ── 1. Trades fermés ──────────────────────────────────────────────────
    closed = db.get_closed_trades(limit=20)
    lines = ["📜 <b>20 DERNIERS TRADES FERMÉS</b>\n"]

    reason_short = {
        "stop_loss":     "🛑 SL",
        "trailing_stop": "📉 Trail",
        "take_profit":   "✅ TP2",
        "tp1_partial":   "🟡 TP1",
        "timeout":       "⏰ Timeout",
        "manual":        "👤 Manuel",
    }

    if not closed:
        lines.append("Aucun trade fermé.")
    else:
        for t in closed:
            pnl_pct = t.get("pnl_pct") or 0
            pnl_usd = t.get("pnl_usd") or 0
            reason = reason_short.get(
                t.get("close_reason", ""), t.get("close_reason", "?")
            )
            score = t.get("ai_score", "?")
            emoji = "🟢" if pnl_pct > 0 else "🔴"
            tp1_str = " [TP1✓]" if t.get("tp1_hit") else ""

            try:
                open_at = datetime.fromisoformat(t["open_at"])
                close_at = datetime.fromisoformat(t["close_at"])
                if open_at.tzinfo is None:
                    open_at = open_at.replace(tzinfo=timezone.utc)
                if close_at.tzinfo is None:
                    close_at = close_at.replace(tzinfo=timezone.utc)
                duration = close_at - open_at
                total_min = int(duration.total_seconds() // 60)
                hours = total_min // 60
                mins = total_min % 60
                duration_str = (
                    f"{hours}h{mins:02d}m" if hours > 0 else f"{mins}min"
                )
                open_str = open_at.strftime("%d/%m %H:%M")
            except Exception:
                duration_str = "?"
                open_str = "?"

            lines.append(
                f"{emoji} <b>#{t['id']} {t['token_symbol']}</b>"
                f"{tp1_str}\n"
                f"   {reason} | {pnl_pct:+.1f}% ({'+' if pnl_usd > 0 else ''}"
                f"{pnl_usd:.2f}$)\n"
                f"   Score: {score} | Durée: {duration_str} | {open_str}\n"
                f"   Entrée: ${t.get('entry_price', 0):.8f} → "
                f"${t.get('exit_price', 0):.8f}\n"
            )

    messages.append("\n".join(lines))

    # ── 2. Positions ouvertes ─────────────────────────────────────────────
    open_trades = db.get_open_trades()
    if open_trades:
        lines2 = ["\n📂 <b>POSITIONS OUVERTES</b>\n"]
        for t in open_trades:
            open_at = datetime.fromisoformat(t["open_at"])
            if open_at.tzinfo is None:
                open_at = open_at.replace(tzinfo=timezone.utc)
            elapsed_h = (
                datetime.now(timezone.utc) - open_at
            ).total_seconds() / 3600
            remaining_h = max(0, 24 - elapsed_h)
            lines2.append(
                f"• <b>#{t['id']} {t['token_symbol']}</b> "
                f"| Score: {t.get('ai_score', '?')}\n"
                f"   Entrée: ${t.get('entry_price', 0):.8f} "
                f"| Taille: ${t.get('position_usd', 0):.2f}\n"
                f"   Ouvert: {open_at.strftime('%d/%m %H:%M')} "
                f"| ⏰ {remaining_h:.0f}h restantes\n"
            )
        messages.append("\n".join(lines2))

    # ── 3. Derniers signaux non tradés ────────────────────────────────────
    with db.db_cursor() as cur:
        cur.execute("""
            SELECT * FROM signals
            WHERE acted = 0
            ORDER BY signal_at DESC
            LIMIT 10
        """)
        signals = [dict(r) for r in cur.fetchall()]

    if signals:
        lines3 = ["\n📡 <b>10 DERNIERS SIGNAUX NON TRADÉS</b>\n"]
        for s in signals:
            try:
                sig_at = datetime.fromisoformat(s["signal_at"])
                if sig_at.tzinfo is None:
                    sig_at = sig_at.replace(tzinfo=timezone.utc)
                sig_str = sig_at.strftime("%d/%m %H:%M")
            except Exception:
                sig_str = "?"
            lines3.append(
                f"• <b>{s['token_symbol']}</b> | Score: {s['ai_score']} "
                f"| {sig_str}\n"
                f"   Prix: ${s.get('price_usd', 0):.8f} "
                f"| {s.get('chain', '?')}\n"
            )
        messages.append("\n".join(lines3))

    # Telegram limite à 4096 chars par message — on envoie en plusieurs fois
    for msg in messages:
        if msg.strip():
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: /close <code>id</code>\n"
            "Ex: /close 3\n\nUtilise /positions pour voir les IDs.",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        trade_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID invalide.")
        return

    open_trades = db.get_open_trades()
    trade = next((t for t in open_trades if t["id"] == trade_id), None)
    if not trade:
        await update.message.reply_text(
            f"Aucune position ouverte avec l'ID #{trade_id}."
        )
        return

    prices = await _fetch_current_prices([trade])
    current_price = prices.get(trade["token_address"], 0)
    if current_price <= 0:
        current_price = trade["entry_price"]
        await update.message.reply_text(
            "⚠️ Prix introuvable — fermeture au prix d'entrée."
        )

    closed = db.close_trade(trade_id, current_price, "manual")
    pnl = closed.get("pnl_usd", 0)
    pnl_pct = closed.get("pnl_pct", 0)
    emoji = "🟢" if pnl > 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} <b>Position #{trade_id} fermée manuellement</b>\n\n"
        f"Token: <b>{trade['token_symbol']}</b>\n"
        f"Sortie: <b>${current_price:.8f}</b>\n"
        f"PnL: <b>{'+' if pnl > 0 else ''}{pnl:.2f} USD ({pnl_pct:+.2f}%)</b>",
        parse_mode=ParseMode.HTML,
    )


@authorized_only
async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /alert <code>adresse</code> <code>prix</code>\n"
            "Ex: /alert EPjFWdd...t1v 0.00025",
            parse_mode=ParseMode.HTML,
        )
        return
    addr = context.args[0]
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Prix invalide.")
        return

    _alerts[addr] = target
    _alert_symbols[addr] = addr[:8]
    await update.message.reply_text(
        f"🔔 <b>Alerte créée</b>\n\n"
        f"Adresse: <code>{addr[:20]}...</code>\n"
        f"Prix cible: <b>${target:.8f}</b>",
        parse_mode=ParseMode.HTML,
    )


@authorized_only
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _alerts:
        await update.message.reply_text("Aucune alerte active.")
        return
    lines = ["🔔 <b>ALERTES ACTIVES</b>\n"]
    for addr, target in _alerts.items():
        symbol = _alert_symbols.get(addr, addr[:8])
        lines.append(f"• <code>{symbol}</code> → ${target:.8f}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _trader_ref:
        await update.message.reply_text("Trader non disponible.")
        return
    bl = _trader_ref.get_blacklist()
    if not bl:
        await update.message.reply_text("Aucun token en blacklist.")
        return
    lines = ["🚫 <b>BLACKLIST</b>\n"]
    for item in bl:
        lines.append(
            f"• <code>{item['address'][:20]}...</code> "
            f"— expire dans {item['expires_in_h']}h"
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
    await update.message.reply_text(
        "⏸ Bot en <b>pause</b>. Aucun nouveau trade.",
        parse_mode=ParseMode.HTML,
    )


@authorized_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _paused
    _paused = False
    await update.message.reply_text(
        "▶️ Bot <b>repris</b>.", parse_mode=ParseMode.HTML
    )


@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 <b>AIDE COMPLÈTE</b>\n\n"
        "/start — Message d'accueil\n"
        "/status — État général + blacklist\n"
        "/positions — Positions + PnL live\n"
        "/pnl — Résultats réalisés\n"
        "/historique — 20 derniers trades + signaux\n"
        "/close <code>id</code> — Fermer une position\n"
        "/alert <code>adresse</code> <code>prix</code> — Alerte prix\n"
        "/alerts — Alertes actives\n"
        "/blacklist — Tokens en blacklist\n"
        "/pause — Suspendre les trades\n"
        "/resume — Reprendre\n"
        "/help — Cette aide\n\n"
        f"Mode: <b>{config.TRADING_MODE.upper()}</b>\n"
        f"Score min: <b>65/100</b>\n"
        f"TP1: +20% (50%) | TP2: +40% | SL: -15%\n"
        f"Trailing: actif à +15% | ⏰ 24h"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _score_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _get_model_trained() -> bool:
    try:
        from analyzer.model import get_model
        return get_model().is_trained
    except Exception:
        return False
