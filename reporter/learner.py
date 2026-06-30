"""
Boucle d'apprentissage — réentraîne le modèle ML sur les trades historiques.
Le label 'success' tient désormais compte des sorties TP1 partielles.
Le réentraînement automatique est espacé pour éviter un modèle instable
sur un petit échantillon.
"""
import json
import logging
from datetime import datetime, timezone

import database as db
from analyzer.model import get_model

logger = logging.getLogger(__name__)

# ← CHANGEMENT : espacement minimum entre 2 réentraînements automatiques
#    déclenchés par accumulation de nouveaux trades (au lieu de tous les 20)
MIN_NEW_TRADES_BETWEEN_RETRAINS = 40


def prepare_training_data() -> list[dict]:
    """
    Prépare les données d'entraînement depuis les trades fermés.
    Chaque entrée = données du token au moment du signal + label success.

    Le label success est désormais True si :
    - le trade a touché TP1 (tp1_hit=1), peu importe la suite, OU
    - le PnL final est positif ET la sortie n'est pas un stop_loss
    """
    closed_trades = db.get_all_closed_trades()
    training_data = []

    for trade in closed_trades:
        try:
            token_data_raw = trade.get("token_data")
            if not token_data_raw:
                continue

            if isinstance(token_data_raw, str):
                token_data = json.loads(token_data_raw)
            else:
                token_data = token_data_raw

            pnl_usd = trade.get("pnl_usd") or 0
            close_reason = trade.get("close_reason", "")
            tp1_hit = bool(trade.get("tp1_hit"))

            # ← CHANGEMENT : un trade qui a touché TP1 est considéré comme
            # un succès même si la suite a fini en stop_loss ou timeout léger,
            # car le signal d'entrée était bon (le token a fait +20%).
            success = tp1_hit or (pnl_usd > 0 and close_reason != "stop_loss")

            training_data.append({
                **token_data,
                "success": success,
                "trade_id": trade["id"],
                "pnl_pct": trade.get("pnl_pct", 0),
                "close_reason": close_reason,
                "tp1_hit": tp1_hit,
            })
        except Exception as e:
            logger.debug("Erreur préparation trade #%s: %s", trade.get("id"), e)

    logger.info(
        "Données d'entraînement: %d trades (positifs: %d, négatifs: %d, dont TP1 atteint: %d)",
        len(training_data),
        sum(1 for t in training_data if t.get("success")),
        sum(1 for t in training_data if not t.get("success")),
        sum(1 for t in training_data if t.get("tp1_hit")),
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
    """
    Vérifie si un réentraînement est nécessaire.

    Changements :
    - Premier entraînement : déclenché à config.MIN_TRADES_FOR_RETRAIN
      (recommandé : 50 minimum, à configurer dans config.py ou Railway)
    - Réentraînements suivants : espacés d'au moins
      MIN_NEW_TRADES_BETWEEN_RETRAINS nouveaux trades (40 par défaut)
      au lieu de 20, pour stabiliser le modèle sur un échantillon
      encore en croissance.
    """
    import config
    model = get_model()

    # Jamais entraîné — premier seuil
    if not model.is_trained:
        closed_count = len(db.get_all_closed_trades())
        return closed_count >= config.MIN_TRADES_FOR_RETRAIN

    # Réentraînement périodique (basé sur le temps)
    if model.last_trained:
        from datetime import timedelta
        age = datetime.now(timezone.utc) - model.last_trained
        if age.days >= config.RETRAIN_INTERVAL_DAYS:
            return True

    # ← CHANGEMENT : espacement à 40 nouveaux trades minimum au lieu de 20
    total_trades = len(db.get_all_closed_trades())
    if total_trades >= model.training_samples + MIN_NEW_TRADES_BETWEEN_RETRAINS:
        return True

    return False
