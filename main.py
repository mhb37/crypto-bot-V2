"""
Point d'entrée principal du Meme Coin Scanner & Trading Bot.
Lance le scan périodique, le trading paper/live, et les notifications Telegram.
"""
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

# ── Créer les dossiers avant TOUT (y compris le FileHandler) ─────────────────
os.makedirs("data", exist_ok=True)
os.makedirs("data/reports", exist_ok=True)
os.makedirs("models", exist_ok=True)

# ── Logging — après makedirs pour que FileHandler ne plante pas ───────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/bot.log", encoding="utf-8"),
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

# ── Global state ──────────────────────────────────────────────────────────────
_running = True
_trader = None          # PaperTrader | LiveTrader selon TRADING_MODE
_last_report_day = -1


def handle_shutdown(signum, frame):
    global _running
    logger.info("Signal d'arrêt reçu (%d) — arrêt propre...", signum)
    _running = False


def _init_trader():
    """
    Instancie le trader approprié selon TRADING_MODE.
    Retourne un PaperTrader si le mode live n'est pas correctement configuré.
    """
    if config.TRADING_MODE == "live":
        if not config.SOLANA_PRIVATE_KEY:
            logger.error(
                "TRADING_MODE=live mais SOLANA_PRIVATE_KEY vide — "
                "basculement sur paper trading par sécurité"
            )
            return PaperTrader(), "paper (fallback)"
        try:
            from trader.live_trader import LiveTrader
            lt = LiveTrader()
            return lt, "live"
        except Exception as e:
            logger.error(
                "LiveTrader initialization failed (%s) — "
                "basculement sur paper trading par sécurité", e
            )
            return PaperTrader(), "paper (fallback)"
    return PaperTrader(), "paper"


async def scan_and_trade_loop():
    """Boucle principale : scan → filtre → score → trade."""
    logger.info(
        "🚀 Bot démarré | Mode: %s | Score min: %d",
        config.TRADING_MODE, config.MIN_SCORE_TO_TRADE
    )

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
    """Exécute un cycle complet de scan."""
    now = datetime.now(timezone.utc)
    logger.info("── Scan cycle %s ──", now.strftime("%H:%M:%S"))

    # 1. Scanner les tokens
    raw_tokens = await scan_all()
    if not raw_tokens:
        logger.info("Aucun token trouvé lors du scan")
        return

    # 2. Appliquer les filtres
    filtered = apply_filters(raw_tokens)
    if not filtered:
        logger.info("Aucun token après filtrage")
        return

    logger.info("%d tokens après filtrage", len(filtered))

    # 3. Scorer avec le modèle ML
    model = get_model()
    scored = []
    for token in filtered:
        score = model.score(token)
        token["ai_score"] = score
        scored.append(token)

    scored.sort(key=lambda x: x["ai_score"], reverse=True)

    for t in scored[:5]:
        logger.info(
            "  📊 %s (%s) | Score: %d | +%.1f%% 1h | +%.1f%% 24h | Liq: $%.0f",
            t["symbol"], t["chain"], t["ai_score"],
            t.get("price_change_1h", 0), t.get("price_change_24h", 0),
            t.get("liquidity_usd", 0),
        )

    # 4. Signaux et trades
    if tg.is_paused():
        logger.info("Bot en pause — pas de nouveau trade")
    else:
        for token in scored:
            score = token["ai_score"]
            db.record_signal(token, score)

            if score < config.MIN_SCORE_TO_TRADE:
                continue

            await tg.notify_signal(token, score)

            trade = _open_trade(token, score)
            if trade:
                await tg.notify_trade_open(trade)
                db.record_signal(token, score, acted=True, reason="trade_opened")

    # 5. Vérifier les positions existantes
    current_prices = {t["address"]: t["price_usd"] for t in scored if t.get("address")}
    closed_trades = _check_close_trades(current_prices)
    for ct in closed_trades:
        await tg.notify_trade_close(ct)

    # 6. Réentraînement ML si nécessaire
    if should_retrain():
        logger.info("Lancement du réentraînement ML...")
        result = run_retraining()
        if result.get("status") == "trained":
            await tg.send(
                f"🧠 <b>Modèle ML réentraîné</b>\n"
                f"Accuracy: <b>{result.get('accuracy', 0):.1%}</b>\n"
                f"Precision: <b>{result.get('precision', 0):.1%}</b>\n"
                f"Samples: <b>{result.get('samples', 0)}</b>"
            )


def _open_trade(token: dict, score: int):
    """Délègue l'ouverture de trade au trader actif (paper ou live)."""
    if hasattr(_trader, "open_trade"):
        return _trader.open_trade(token, ai_score=score)
    return None


def _check_close_trades(current_prices: dict) -> list:
    """Délègue la vérification des SL/TP au trader actif."""
    if hasattr(_trader, "check_and_close_trades"):
        return _trader.check_and_close_trades(current_prices)
    return []


async def report_scheduler():
    """Envoie le rapport quotidien à l'heure configurée."""
    global _last_report_day
    while _running:
        now = datetime.now(timezone.utc)
        if now.hour == config.REPORT_HOUR_UTC and _last_report_day != now.day:
            logger.info("Génération du rapport quotidien...")
            try:
                report = generate_daily_report()
                await tg.notify_daily_report(report)
                _last_report_day = now.day
            except Exception as e:
                logger.error("Erreur rapport quotidien: %s", e)
        await asyncio.sleep(60)


async def main():
    global _trader

    # Initialiser la base de données
    db.init_db()

    # Initialiser le trader (paper ou live selon config)
    _trader, effective_mode = _init_trader()
    logger.info(
        "Trader: %s | Portfolio: $%.2f | Positions max: %d | TP: +%.0f%% | SL: -%.0f%%",
        effective_mode, config.INITIAL_PORTFOLIO, config.MAX_OPEN_POSITIONS,
        config.TAKE_PROFIT_PCT * 100, config.STOP_LOSS_PCT * 100,
    )

    # Initialiser Telegram
    tg_ok = await tg.init_telegram()
    if tg_ok:
        await tg.start_polling()
        await tg.send(
            f"🤖 <b>Meme Coin Bot démarré!</b>\n\n"
            f"Mode: <b>{effective_mode.upper()}</b>\n"
            f"Score min: <b>{config.MIN_SCORE_TO_TRADE}/100</b>\n"
            f"Portfolio: <b>${config.INITIAL_PORTFOLIO:.0f}</b>\n"
            f"Scan toutes les <b>{config.SCAN_INTERVAL_SECONDS // 60}min</b>\n\n"
            f"Commandes: /help"
        )
    else:
        logger.warning(
            "Telegram désactivé — configurez TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID"
        )

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
