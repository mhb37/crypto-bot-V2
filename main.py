"""
Point d'entrée principal — Meme Coin Scanner & Trading Bot.

Architecture à 3 boucles parallèles :
- scan_and_trade_loop : scan + alimentation watchlist (5 min)
- position_monitor_loop : surveillance watchlist + positions (60s)
- report_scheduler : rapports + alertes BTC (60s)

Nouvelle stratégie :
- Les tokens avec fort h6/h24 entrent en watchlist
- La watchlist est surveillée toutes les 60s
- Les entrées se font sur micro-momentum + pression acheteuse
- TP/SL adaptatifs selon la force du signal
"""
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone, timedelta

_db_path = os.getenv("DB_PATH", "data/trading.db")
_model_path = os.getenv("MODEL_PATH", "models/scoring_model.pkl")
_data_dir = os.path.dirname(os.path.abspath(_db_path))
_model_dir = os.path.dirname(os.path.abspath(_model_path))

os.makedirs(_data_dir, exist_ok=True)
os.makedirs(os.path.join(_data_dir, "reports"), exist_ok=True)
os.makedirs(_model_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(_data_dir, "bot.log"), encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("main")

import config
import database as db
from scanner.dexscreener import scan_all
from scanner.filter import apply_filters
from scanner.watchlist import (
    add_to_watchlist, get_watchlist, cleanup_expired,
    fetch_realtime_data, update_price_history, compute_entry_signal,
    mark_entry, can_reenter, get_watchlist_count,
)
from analyzer.model import get_model
from trader.paper_trader import PaperTrader
from notifier import telegram_bot as tg
from reporter.daily_report import generate_daily_report
from reporter.learner import should_retrain, run_retraining
from reporter.optimizer import run_optimization
from reporter.ai_analyzer import run_ai_analysis

_running = True
_trader = None
_last_report_day = -1
_last_report_week = -1
POSITION_CHECK_INTERVAL_SECONDS = 60
MIN_SCORE_TO_WATCHLIST = 55  # score minimum pour entrer en watchlist


def handle_shutdown(signum, frame):
    global _running
    logger.info("Signal d'arrêt reçu (%d) — arrêt propre...", signum)
    _running = False


def _init_trader():
    if config.TRADING_MODE == "live":
        if not config.SOLANA_PRIVATE_KEY:
            logger.error("TRADING_MODE=live mais clé vide — fallback paper")
            return PaperTrader(), "paper (fallback)"
        try:
            from trader.live_trader import LiveTrader
            return LiveTrader(), "live"
        except Exception as e:
            logger.error("LiveTrader failed (%s) — fallback paper", e)
            return PaperTrader(), "paper (fallback)"
    return PaperTrader(), "paper"


# ── Boucle 1 : scan + alimentation watchlist ─────────────────────────────────

async def scan_and_trade_loop():
    logger.info(
        "🚀 Bot démarré | Mode: %s | Score watchlist: %d",
        config.TRADING_MODE, MIN_SCORE_TO_WATCHLIST
    )
    while _running:
        try:
            await _one_scan_cycle()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Erreur boucle scan: %s", e, exc_info=True)
        logger.info(
            "Prochain scan dans %ds... | Watchlist: %d tokens",
            config.SCAN_INTERVAL_SECONDS, get_watchlist_count()
        )
        for _ in range(config.SCAN_INTERVAL_SECONDS):
            if not _running:
                break
            await asyncio.sleep(1)


async def _one_scan_cycle():
    now = datetime.now(timezone.utc)
    logger.info("── Scan cycle %s ──", now.strftime("%H:%M:%S"))

    # 1. Scanner
    raw_tokens = await scan_all()
    if not raw_tokens:
        logger.info("Aucun token trouvé")
        return

    # 2. Filtrer
    filtered = apply_filters(raw_tokens)
    logger.info("%d tokens après filtrage", len(filtered))

    if not filtered:
        logger.info("Aucun token après filtrage")
        return

    # 3. Scorer
    model = get_model()
    scored = []
    for token in filtered:
        score = model.score(token)
        token["ai_score"] = score
        scored.append(token)

    scored.sort(key=lambda x: x["ai_score"], reverse=True)

    # 4. Alimenter la watchlist
    added = 0
    for token in scored:
        if token["ai_score"] >= MIN_SCORE_TO_WATCHLIST:
            if add_to_watchlist(token):
                added += 1
        db.record_signal(token, token["ai_score"])

    if added > 0:
        logger.info("Watchlist: +%d nouveau(x) token(s)", added)

    # 5. Nettoyer la watchlist expirée
    cleanup_expired()

    # 6. Prix courants pour alertes
    current_prices = {
        t["address"]: t["price_usd"]
        for t in raw_tokens
        if t.get("address") and t.get("price_usd", 0) > 0
    }
    await tg.check_price_alerts(current_prices)

    # 7. Réentraînement ML
    if should_retrain():
        was_trained = model.is_trained
        logger.info("Lancement réentraînement ML...")
        result = run_retraining()
        if result.get("status") == "trained":
            now_trained = get_model().is_trained
            if not was_trained and now_trained:
                await tg.send(
                    f"🧠 <b>BASCULE EN MODE MACHINE LEARNING</b> 🎉\n\n"
                    f"📊 Samples: <b>{result.get('samples', 0)}</b>\n"
                    f"🎯 Accuracy: <b>{result.get('accuracy', 0):.1%}</b>\n"
                    f"🎯 Precision: <b>{result.get('precision', 0):.1%}</b>\n\n"
                    f"<i>Le scoring se base maintenant sur tes trades passés."
                    f"</i>\n\nVérifiable avec /status."
                )
            else:
                await tg.send(
                    f"🧠 <b>Modèle ML réentraîné</b>\n"
                    f"Accuracy: <b>{result.get('accuracy', 0):.1%}</b>\n"
                    f"Precision: <b>{result.get('precision', 0):.1%}</b>\n"
                    f"Samples: <b>{result.get('samples', 0)}</b>"
                )


# ── Boucle 2 : surveillance watchlist + positions ─────────────────────────────

async def position_monitor_loop():
    logger.info(
        "🔍 Surveillance démarrée (toutes les %ds)",
        POSITION_CHECK_INTERVAL_SECONDS
    )
    while _running:
        try:
            await _monitor_cycle()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Erreur surveillance: %s", e, exc_info=True)
        for _ in range(POSITION_CHECK_INTERVAL_SECONDS):
            if not _running:
                break
            await asyncio.sleep(1)


async def _monitor_cycle():
    """
    Cycle de surveillance toutes les 60s :
    1. Met à jour les prix des tokens en watchlist
    2. Calcule les signaux d'entrée
    3. Ouvre des trades si signal détecté
    4. Vérifie les positions ouvertes (SL/TP/Trailing)
    5. Suivi post-trade
    """
    if tg.is_paused():
        return

    watchlist = get_watchlist()

    # 1. Mettre à jour l'historique des prix en watchlist
    for w in watchlist:
        addr = w["address"]
        chain = w["chain"]
        data = await fetch_realtime_data(addr, chain)
        if data:
            update_price_history(addr, data)

            # 2. Calculer le signal d'entrée
            if can_reenter(addr):
                signal = compute_entry_signal(addr)
                if signal:
                    await _try_open_from_signal(signal, data)

    # 3. Vérifier les positions ouvertes
    open_trades = db.get_open_trades()
    if open_trades:
        closed_trades = await _check_close_trades({})
        for ct in closed_trades:
            await tg.notify_trade_close(ct)

    # 4. Suivi post-trade
    if hasattr(_trader, "check_post_trade_tracking"):
        completed = await _trader.check_post_trade_tracking()
        for result in completed:
            await tg.notify_post_trade_analysis(result)


async def _try_open_from_signal(signal: dict, realtime_data: dict):
    """Tente d'ouvrir un trade basé sur un signal de la watchlist."""
    addr = signal["address"]
    symbol = signal["symbol"]
    current_price = signal["current_price"]

    if current_price <= 0:
        return

    # Reconstruit un token dict complet pour open_trade
    from scanner.watchlist import _watchlist
    w = _watchlist.get(addr)
    if not w:
        return

    token_data = dict(w["token_data"])
    token_data["price_usd"] = current_price
    token_data["liquidity_usd"] = realtime_data.get(
        "liquidity_usd", token_data.get("liquidity_usd", 0)
    )

    # Score IA
    model = get_model()
    score = model.score(token_data)
    token_data["ai_score"] = score

    logger.info(
        "🎯 Signal %s | %s | Force: %.2f | Prix 5m: +%.1f%% | "
        "Acheteurs: %.0f%% | TP: +%.0f%% | SL: -%.0f%%",
        signal["signal_label"], symbol,
        signal["signal_strength"],
        signal["price_change_5m"],
        signal["buy_ratio"] * 100,
        signal["tp_pct"], signal["sl_pct"]
    )

    trade = _trader.open_trade(
        token_data,
        ai_score=score,
        tp_pct=signal["tp_pct"],
        sl_pct=signal["sl_pct"],
        signal_label=signal["signal_label"],
    )

    if trade:
        mark_entry(addr)
        await tg.notify_trade_open(trade)
        db.record_signal(token_data, score, acted=True, reason="watchlist_signal")
        logger.info(
            "📈 Trade watchlist ouvert | %s | #%d",
            symbol, trade["id"]
        )


async def _check_close_trades(current_prices: dict) -> list:
    if hasattr(_trader, "check_and_close_trades"):
        return await _trader.check_and_close_trades(current_prices)
    return []


# ── Boucle 3 : rapports et alertes ───────────────────────────────────────────

async def report_scheduler():
    global _last_report_day, _last_report_week
    while _running:
        now = datetime.now(timezone.utc)

        if now.minute == 0:
            await tg.check_btc_and_alert()

        if now.hour == config.REPORT_HOUR_UTC and _last_report_day != now.day:

            try:
                report = generate_daily_report()
                await tg.notify_daily_report(report)
                _last_report_day = now.day
            except Exception as e:
                logger.error("Erreur rapport quotidien: %s", e)

            week_number = now.isocalendar()[1]
            if now.weekday() == 0 and _last_report_week != week_number:
                try:
                    stats = db.get_portfolio_stats()
                    await tg.notify_weekly_report(stats)
                    _last_report_week = week_number
                except Exception as e:
                    logger.error("Erreur rapport hebdo: %s", e)

            optimizer_result = {}
            try:
                optimizer_result = run_optimization()
                changes = optimizer_result.get("changes", [])
                if changes:
                    change_lines = "\n".join(
                        f"• {c['param']}: {c['old']} → <b>{c['new']}</b>\n"
                        f" ↳ {c['reason']}"
                        for c in changes
                    )
                    await tg.send(
                        f"🔧 <b>Paramètres optimisés</b>\n\n{change_lines}"
                    )
            except Exception as e:
                logger.error("Erreur optimiseur: %s", e)

            try:
                ai_message = await run_ai_analysis(optimizer_result)
                await tg.send(ai_message)
            except Exception as e:
                logger.error("Erreur analyse IA: %s", e)

        await asyncio.sleep(60)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _trader

    db.init_db()

    _trader, effective_mode = _init_trader()
    logger.info(
        "Trader: %s | Portfolio: $%.2f | Max positions: %d",
        effective_mode, config.INITIAL_PORTFOLIO, config.MAX_OPEN_POSITIONS,
    )

    tg_ok = await tg.init_telegram()
    if tg_ok:
        tg.set_trader(_trader)
        await tg.start_polling()
        await tg.send(
            f"🤖 <b>Meme Coin Bot démarré!</b>\n\n"
            f"Mode: <b>{effective_mode.upper()}</b>\n"
            f"Portfolio: <b>${config.INITIAL_PORTFOLIO:.0f}</b>\n"
            f"Scan: toutes les "
            f"<b>{config.SCAN_INTERVAL_SECONDS // 60}min</b>\n"
            f"Surveillance watchlist: toutes les "
            f"<b>{POSITION_CHECK_INTERVAL_SECONDS}s</b>\n\n"
            f"Nouvelle stratégie :\n"
            f"• Watchlist active sur tokens h6/h24 forts\n"
            f"• Entrée sur micro-momentum + pression acheteuse\n"
            f"• TP/SL adaptatifs selon force du signal\n"
            f"• Re-entrées possibles (cooldown 15min)\n\n"
            f"/help pour les commandes"
        )
    else:
        logger.warning("Telegram désactivé")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        await asyncio.gather(
            scan_and_trade_loop(),
            position_monitor_loop(),
            report_scheduler(),
            return_exceptions=True,
        )
    finally:
        logger.info("Arrêt du bot...")
        await tg.stop_polling()
        logger.info("Bot arrêté proprement.")


if __name__ == "__main__":
    asyncio.run(main())
