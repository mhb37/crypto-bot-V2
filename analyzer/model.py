"""
Modèle ML de scoring des tokens (0-100).
RandomForest par défaut, avec fallback sur scoring heuristique si pas assez de données.
"""
import os
import logging
import pickle
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score

import config
from analyzer.features import extract_features, FEATURE_NAMES

logger = logging.getLogger(__name__)


class ScoringModel:
    def __init__(self):
        self.model: Optional[RandomForestClassifier] = None
        self.scaler = StandardScaler()
        self.is_trained = False
        self.last_trained: Optional[datetime] = None
        self.training_samples = 0
        self._load_or_init()

    def _load_or_init(self):
        """Charge le modèle sauvegardé ou initialise un nouveau."""
        os.makedirs(os.path.dirname(config.MODEL_PATH), exist_ok=True)
        if os.path.exists(config.MODEL_PATH):
            try:
                with open(config.MODEL_PATH, "rb") as f:
                    data = pickle.load(f)
                self.model = data["model"]
                self.scaler = data["scaler"]
                self.is_trained = True
                self.last_trained = data.get("last_trained")
                self.training_samples = data.get("training_samples", 0)
                logger.info(
                    "Modèle chargé — entraîné le %s sur %d exemples",
                    self.last_trained, self.training_samples
                )
            except Exception as e:
                logger.warning("Impossible de charger le modèle: %s — nouveau modèle", e)
                self._init_new_model()
        else:
            logger.info("Aucun modèle existant — scoring heuristique actif")
            self._init_new_model()

    def _init_new_model(self):
        """Initialise un modèle RandomForest vierge."""
        self.model = RandomForestClassifier(
            n_estimators=200,
            max_depth=8,
            min_samples_leaf=3,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        )
        self.is_trained = False

    def score(self, token: dict) -> int:
        """
        Score un token de 0 à 100.
        Si le modèle est entraîné, utilise ML.
        Sinon, utilise le scoring heuristique.
        """
        features = extract_features(token)

        if self.is_trained:
            try:
                X = self.scaler.transform([features])
                proba = self.model.predict_proba(X)[0]
                # proba[1] = probabilité de succès
                score = int(round(proba[1] * 100))
                return max(0, min(100, score))
            except Exception as e:
                logger.warning("Erreur scoring ML: %s — fallback heuristique", e)

        return self._heuristic_score(token, features)

    def _heuristic_score(self, token: dict, features: np.ndarray) -> int:
        """
        Scoring heuristique basé sur les features extraites.
        Utilisé quand le modèle n'est pas encore entraîné.
        """
        weights = {
            "momentum_score": 25,
            "volume_liq_ratio": 15,
            "buy_sell_ratio_1h": 20,
            "vol_1h_vs_24h": 15,
            "consistency_score": 15,
            "age_score": 10,
        }
        feature_dict = dict(zip(FEATURE_NAMES, features))
        total_weight = sum(weights.values())
        score = sum(feature_dict.get(k, 0) * w for k, w in weights.items())
        return int(round((score / total_weight) * 100))

    def train(self, trades: list[dict]) -> dict:
        """
        Entraîne le modèle sur les trades historiques.
        trades: liste de dicts avec les données du token + label 'success' (bool).
        """
        if len(trades) < config.MIN_TRADES_FOR_RETRAIN:
            logger.info(
                "Pas assez de trades (%d < %d) pour réentraîner",
                len(trades), config.MIN_TRADES_FOR_RETRAIN
            )
            return {"status": "skipped", "reason": "not_enough_data"}

        X = np.array([extract_features(t) for t in trades])
        y = np.array([1 if t.get("success", False) else 0 for t in trades])

        logger.info("Entraînement sur %d exemples (positifs: %d)", len(y), y.sum())

        if len(set(y)) < 2:
            logger.warning("Classe unique dans les labels — entraînement annulé")
            return {"status": "skipped", "reason": "single_class"}

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        self.scaler.fit(X_train)
        X_train_s = self.scaler.transform(X_train)
        X_test_s = self.scaler.transform(X_test)

        self.model.fit(X_train_s, y_train)

        y_pred = self.model.predict(X_test_s)
        acc = accuracy_score(y_test, y_pred)
        try:
            prec = precision_score(y_test, y_pred, zero_division=0)
        except Exception:
            prec = 0.0

        self.is_trained = True
        self.last_trained = datetime.now(timezone.utc)
        self.training_samples = len(trades)
        self._save()

        # Feature importance
        importances = dict(zip(FEATURE_NAMES, self.model.feature_importances_))
        top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:5]

        result = {
            "status": "trained",
            "samples": len(trades),
            "accuracy": round(acc, 3),
            "precision": round(prec, 3),
            "top_features": top_features,
        }
        logger.info("Modèle entraîné: acc=%.3f, prec=%.3f", acc, prec)
        return result

    def _save(self):
        """Sauvegarde le modèle et le scaler."""
        try:
            os.makedirs(os.path.dirname(config.MODEL_PATH), exist_ok=True)
            with open(config.MODEL_PATH, "wb") as f:
                pickle.dump({
                    "model": self.model,
                    "scaler": self.scaler,
                    "last_trained": self.last_trained,
                    "training_samples": self.training_samples,
                }, f)
            logger.info("Modèle sauvegardé → %s", config.MODEL_PATH)
        except Exception as e:
            logger.error("Erreur sauvegarde modèle: %s", e)

    def info(self) -> dict:
        return {
            "is_trained": self.is_trained,
            "last_trained": self.last_trained.isoformat() if self.last_trained else None,
            "training_samples": self.training_samples,
            "mode": "ml" if self.is_trained else "heuristic",
        }


# Singleton global
_model_instance: Optional[ScoringModel] = None


def get_model() -> ScoringModel:
    global _model_instance
    if _model_instance is None:
        _model_instance = ScoringModel()
    return _model_instance
