"""
Extraction de features pour le modèle ML.
Toutes les features sont normalisées entre 0 et 1 ou représentent des ratios.
"""
import math
import numpy as np


FEATURE_NAMES = [
    "momentum_score",
    "volume_mcap_ratio",
    "volume_liq_ratio",
    "vol_1h_vs_24h",
    "buy_sell_ratio_1h",
    "price_change_1h_norm",
    "price_change_6h_norm",
    "price_change_24h_norm",
    "liquidity_score",
    "age_score",
    "txn_activity_score",
    "consistency_score",
]


def extract_features(token: dict) -> np.ndarray:
    """
    Extrait un vecteur de features normalisé depuis un token.
    Retourne un array numpy de shape (len(FEATURE_NAMES),).
    """
    h1 = token.get("price_change_1h", 0)
    h6 = token.get("price_change_6h", 0)
    h24 = token.get("price_change_24h", 0)
    liq = max(token.get("liquidity_usd", 1), 1)
    vol24 = max(token.get("volume_24h", 0), 0)
    vol6 = max(token.get("volume_6h", 0), 0)
    vol1 = max(token.get("volume_1h", 0), 0)
    mcap = max(token.get("market_cap", 0), 1)
    age_h = max(token.get("age_hours", 1), 1)
    buys1 = max(token.get("buys_1h", 0), 0)
    sells1 = max(token.get("sells_1h", 0), 0)

    # 1. Momentum score : tendance haussière cohérente sur toutes les périodes
    momentum_score = _sigmoid((h1 * 0.5 + h6 * 0.3 + h24 * 0.2) / 10)

    # 2. Volume / Market cap ratio (activité relative)
    volume_mcap_ratio = min(vol24 / mcap, 10) / 10

    # 3. Volume / Liquidité ratio (signal d'intérêt)
    volume_liq_ratio = min(vol24 / liq, 20) / 20

    # 4. Volume 1h vs volume moyen par heure sur 24h (accélération)
    avg_vol_per_hour = vol24 / 24
    vol_1h_vs_24h = min(vol1 / max(avg_vol_per_hour, 1), 10) / 10

    # 5. Ratio acheteurs / vendeurs (pression acheteuse)
    buy_sell_ratio = min(buys1 / max(sells1, 1), 10) / 10

    # 6-8. Price changes normalisés (sigmoïde)
    pc_1h = _sigmoid(h1 / 20)
    pc_6h = _sigmoid(h6 / 30)
    pc_24h = _sigmoid(h24 / 50)

    # 9. Score de liquidité (log-scale, optimal entre 10K et 500K USD)
    liq_score = _sigmoid((math.log10(max(liq, 1)) - 3) / 2)

    # 10. Score d'âge (optimal entre 24h et 72h)
    age_score = _bell_curve(age_h, center=48, width=48)

    # 11. Activité transactionnelle (nombre de txns par heure)
    txn_per_hour = (buys1 + sells1)
    txn_score = _sigmoid(txn_per_hour / 50)

    # 12. Consistance (toutes les périodes en hausse)
    consistency = float(h1 > 0) * 0.33 + float(h6 > 0) * 0.33 + float(h24 > 0) * 0.34

    features = np.array([
        momentum_score,
        volume_mcap_ratio,
        volume_liq_ratio,
        vol_1h_vs_24h,
        buy_sell_ratio,
        pc_1h,
        pc_6h,
        pc_24h,
        liq_score,
        age_score,
        txn_score,
        consistency,
    ], dtype=np.float32)

    # Remplacer NaN/Inf
    features = np.nan_to_num(features, nan=0.0, posinf=1.0, neginf=0.0)
    features = np.clip(features, 0.0, 1.0)

    return features


def _sigmoid(x: float) -> float:
    """Sigmoïde centrée en 0."""
    try:
        return 1 / (1 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _bell_curve(x: float, center: float, width: float) -> float:
    """Courbe en cloche gaussienne normalisée entre 0 et 1."""
    return math.exp(-((x - center) ** 2) / (2 * width ** 2))
