"""
Point d'entrée principal — Meme Coin Scanner & Trading Bot.
Inclut : rapport hebdo, alerte BTC, vérification alertes prix,
notification spéciale lors du premier passage en mode ML.
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
        logging.FileHandler(os.path.join(_data_dir, "bot.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

import config
import database as db
from scanner.dexscreener import scan_all
from scanner.filter import apply_filters
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
_notified_signals: dict = {}
_SIGNAL_COOLDOWN_HOURS = 4


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


async def scan_and_trade_loop():
    logger.info("🚀 Bot démarré | Mode: %s | Score min: %d",
                config.TRADING_MODE, config.MIN_SCORE_TO_TRADE)
    while _running:
        try:
            await _one_scan_cycle()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Erreur boucle principale: %s", e, exc_info=True)
        logger.info("Prochain scan dans %ds...", config.SCAN_INTERVAL_SECONDS)
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
    if not filtered:
        logger.info("Aucun token après filtrage")
        return

    logger.info("%d tokens après filtrage", len(filtered))

    # 3. Scorer
    model = get_model()
    scored = []
    for token in filtered:
        score = model.score(token)
        token["ai_score"] = score
        scored.append(token)

    scored.sort(key=lambda x: x["ai_score"], reverse=True)

    for t in scored[:5]:
        logger.info(
            " 📊 %s (%s) | Score: %d | +%.1f%% 1h | Liq: $%.0f",
            t["symbol"], t["chain"], t["ai_score"],
            t.get("price_change_1h", 0), t.get("liquidity_usd", 0),
        )

    # 4. Prix courants pour alertes et positions
    current_prices = {
        t["address"]: t["price_usd"]
        for t in scored
        if t.get("address") and t.get("price_usd", 0) > 0
    }

    # 5. Vérifier alertes de prix
    await tg.check_price_alerts(current_prices)

    # 6. Signaux et trades
    if tg.is_paused():
        logger.info("Bot en pause — pas de nouveau trade")
    else:
        for token in scored:
            score = token["ai_score"]
            address = token.get("address", "")

            db.record_signal(token, score)

            if score < config.MIN_SCORE_TO_TRADE:
                continue

            last_notified = _notified_signals.get(address)
            cooldown = timedelta(hours=_SIGNAL_COOLDOWN_HOURS)
            already_notified = last_notified and (now - last_notified) < cooldown

            if already_notified:
                continue

            _notified_signals[address] = now

            cutoff = now - timedelta(hours=24)
            stale = [a for a, t in _notified_signals.items() if t < cutoff]
            for a in stale:
                del _notified_signals[a]

            await tg.notify_signal(token, score)

            trade = _open_trade(token, score)
            if trade:
                await tg.notify_trade_open(trade)
                db.record_signal(token, score, acted=True, reason="trade_opened")

    # 7. Vérifier positions (SL / Trailing / Multi-TP / Timeout)
    closed_trades = await _check_close_trades(current_prices)
    for ct in closed_trades:
        await tg.notify_trade_close(ct)

    # 8. Réentraînement ML
    if should_retrain():
        was_trained = model.is_trained  # ← état avant réentraînement
        logger.info("Lancement réentraînement ML...")
        result = run_retraining()

        if result.get("status") == "trained":
            now_trained = get_model().is_trained  # ← état après (recharge l'instance)

            if not was_trained and now_trained:
                # ← Premier passage en mode ML : notification spéciale
                await tg.send(
                    f"🧠 <b>BASCULE EN MODE MACHINE LEARNING</b> 🎉\n\n"
                    f"Le bot vient de passer du scoring heuristique au "
                    f"RandomForest entraîné sur tes trades historiques.\n\n"
                    f"📊 Samples: <b>{result.get('samples', 0)}</b>\n"
                    f"🎯 Accuracy: <b>{result.get('accuracy', 0):.1%}</b>\n"
                    f"🎯 Precision: <b>{result.get('precision', 0):.1%}</b>\n\n"
                    f"<i>À partir de maintenant, le scoring des tokens va se "
                    f"baser sur les patterns appris de tes trades passés, "
                    f"pas sur la formule fixe d'avant. Surveille bien les "
                    f"prochains résultats — le comportement du bot peut "
                    f"changer.</i>\n\n"
                    f"Vérifiable à tout moment avec /status."
                )
            else:
                # Réentraînement classique (déjà en ML, juste mis à jour)
                await tg.send(
                    f"🧠 <b>Modèle ML réentraîné</b>\n"
                    f"Accuracy: <b>{result.get('accuracy', 0):.1%}</b>\n"
                    f"Precision: <b>{result.get('precision', 0):.1%}</b>\n"
                    f"Samples: <b>{result.get('samples', 0)}</b>"
                )


def _open_trade(token: dict, score: int):
    if hasattr(_trader, "open_trade"):
        return _trader.open_trade(token, ai_score=score)
    return None


async def _check_close_trades(current_prices: dict) -> list:
    if hasattr(_trader, "check_and_close_trades"):
        return await _trader.check_and_close_trades(current_prices)
    return []


async def report_scheduler():
    """Rapport quotidien + hebdo + alerte BTC + optimisation."""
    global _last_report_day, _last_report_week
    while _running:
        now = datetime.now(timezone.utc)

        # ── Alerte BTC toutes les heures ──────────────────────────────────
        if now.minute == 0:
            await tg.check_btc_and_alert()

        if now.hour == config.REPORT_HOUR_UTC and _last_report_day != now.day:

            # ── Rapport quotidien ──────────────────────────────────────────
            try:
                report = generate_daily_report()
                await tg.notify_daily_report(report)
                _last_report_day = now.day
            except Exception as e:
                logger.error("Erreur rapport quotidien: %s", e)

            # ── Rapport hebdomadaire (lundi) ───────────────────────────────
            week_number = now.isocalendar()[1]
            if now.weekday() == 0 and _last_report_week != week_number:
                try:
                    stats = db.get_portfolio_stats()
                    await tg.notify_weekly_report(stats)
                    _last_report_week = week_number
                except Exception as e:
                    logger.error("Erreur rapport hebdo: %s", e)

            # ── Optimiseur ─────────────────────────────────────────────────
            optimizer_result = {}
            try:
                optimizer_result = run_optimization()
                changes = optimizer_result.get("changes", [])
                if changes:
                    change_lines = "\n".join(
                        f"• {c['param']}: {c['old']} → <b>{c['new']}</b>\n ↳ {c['reason']}"
                        for c in changes
                    )
                    await tg.send(
                        f"🔧 <b>Paramètres optimisés</b>\n\n{change_lines}"
                    )
            except Exception as e:
                logger.error("Erreur optimiseur: %s", e)

            # ── Analyse IA ─────────────────────────────────────────────────
            try:
                ai_message = await run_ai_analysis(optimizer_result)
                await tg.send(ai_message)
            except Exception as e:
                logger.error("Erreur analyse IA: %s", e)

        await asyncio.sleep(60)


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
            f"Score min: <b>{config.MIN_SCORE_TO_TRADE}/100</b>\n"
            f"Portfolio: <b>${config.INITIAL_PORTFOLIO:.0f}</b>\n"
            f"Scan: toutes les <b>{config.SCAN_INTERVAL_SECONDS // 60}min</b>\n\n"
            f"TP1: +20% (50%) | TP2: +40% | SL: -10%\n"
            f"Trailing actif à +15% | ⏰ 24h\n\n"
            f"/help pour les commandes"
        )
    else:
        logger.warning("Telegram désactivé")

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        await asyncio.gather(
            scan_and_trade_loop(),
            report_scheduler(),
            return_exceptions=True,
        )
    finally:
        logger.info("Arrêt du bot...")
        await tg.stop_polling()
        logger.info("Bot arrêté proprement.")


if __name__ == "__main__":
    asyncio.run(main())
