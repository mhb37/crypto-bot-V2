"""
Boucle d'apprentissage — réentraîne le modèle ML sur les trades historiques.
"""
import json
import logging
from datetime import datetime, timezone

import database as db
from analyzer.model import get_model

logger = logging.getLogger(__name__)


def prepare_training_data() -> list[dict]:
    """
    Prépare les données d'entraînement depuis les trades fermés.
    Chaque entrée = données du token au moment du signal + label success.
    """
    closed_trades = db.get_all_closed_trades()
    training_data = []

    for trade in closed_trades:
        try:
            # Récupérer les données du token stockées au moment du trade
            token_data_raw = trade.get("token_data")
            if not token_data_raw:
                continue

            if isinstance(token_data_raw, str):
                token_data = json.loads(token_data_raw)
            else:
                token_data = token_data_raw

            # Label: succès si PnL > 0 ET raison != stop_loss
            pnl_usd = trade.get("pnl_usd") or 0
            close_reason = trade.get("close_reason", "")
            success = pnl_usd > 0 and close_reason != "stop_loss"

            training_data.append({
                **token_data,
                "success": success,
                "trade_id": trade["id"],
                "pnl_pct": trade.get("pnl_pct", 0),
                "close_reason": close_reason,
            })
        except Exception as e:
            logger.debug("Erreur préparation trade #%s: %s", trade.get("id"), e)

    logger.info(
        "Données d'entraînement: %d trades (positifs: %d, négatifs: %d)",
        len(training_data),
        sum(1 for t in training_data if t.get("success")),
        sum(1 for t in training_data if not t.get("success")),
    )
    return training_data


def run_retraining() -> dict:
    """
    Lance le réentraînement du modèle ML.
    Retourne un dict avec les métriques.
    """
    logger.info("Début du réentraînement du modèle ML...")
    training_data = prepare_training_data()

    model = get_model()
    result = model.train(training_data)

    if result["status"] == "trained":
        # Enregistrer en DB
        db.record_model_run(
            samples=result.get("samples", 0),
            accuracy=result.get("accuracy", 0),
            precision=result.get("precision", 0),
            status="success",
            notes=f"Top features: {result.get('top_features', [])}",
        )
        logger.info(
            "✅ Réentraînement réussi — accuracy=%.3f, precision=%.3f, samples=%d",
            result.get("accuracy", 0), result.get("precision", 0), result.get("samples", 0),
        )
    else:
        logger.info("Réentraînement non effectué: %s", result.get("reason"))

    return result


def should_retrain() -> bool:
    """Vérifie si un réentraînement est nécessaire."""
    import config
    model = get_model()

    # Jamais entraîné
    if not model.is_trained:
        closed_count = len(db.get_all_closed_trades())
        return closed_count >= config.MIN_TRADES_FOR_RETRAIN

    # Réentraînement périodique
    if model.last_trained:
        from datetime import timedelta
        age = datetime.now(timezone.utc) - model.last_trained
        if age.days >= config.RETRAIN_INTERVAL_DAYS:
            return True

    # Beaucoup de nouveaux trades depuis le dernier entraînement
    total_trades = len(db.get_all_closed_trades())
    if total_trades >= model.training_samples + 20:
        return True

    return False
