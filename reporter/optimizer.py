"""
Auto-optimiseur — analyse les trades passés et ajuste les paramètres du bot.
S'exécute après chaque rapport quotidien dès qu'il y a assez de trades.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import database as db

logger = logging.getLogger(__name__)

import os as _os, config as _cfg
OPTIMIZER_STATE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(_cfg.DB_PATH)), "optimizer_state.json")
MIN_TRADES_TO_OPTIMIZE = 10


def _load_state() -> dict:
    if os.path.exists(OPTIMIZER_STATE_PATH):
        try:
            with open(OPTIMIZER_STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "min_score": None,
        "last_run": None,
        "history": [],
    }


def _save_state(state: dict):
    os.makedirs(os.path.dirname(OPTIMIZER_STATE_PATH), exist_ok=True)
    with open(OPTIMIZER_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def _win_rate_by_score_range(trades: list[dict]) -> dict:
    """Calcule le win rate par tranche de score."""
    buckets = {
        "50-59": {"wins": 0, "total": 0},
        "60-69": {"wins": 0, "total": 0},
        "70-79": {"wins": 0, "total": 0},
        "80+":   {"wins": 0, "total": 0},
    }
    for t in trades:
        score = t.get("ai_score", 0)
        success = t.get("success", 0)
        if score >= 80:
            key = "80+"
        elif score >= 70:
            key = "70-79"
        elif score >= 60:
            key = "60-69"
        elif score >= 50:
            key = "50-59"
        else:
            continue
        buckets[key]["total"] += 1
        if success:
            buckets[key]["wins"] += 1
    return {
        k: {
            "win_rate": round(v["wins"] / v["total"] * 100, 1) if v["total"] > 0 else None,
            "total": v["total"],
        }
        for k, v in buckets.items()
    }


def _win_rate_by_chain(trades: list[dict]) -> dict:
    """Calcule le win rate par blockchain."""
    chains: dict = {}
    for t in trades:
        chain = t.get("chain", "unknown")
        if chain not in chains:
            chains[chain] = {"wins": 0, "total": 0}
        chains[chain]["total"] += 1
        if t.get("success"):
            chains[chain]["wins"] += 1
    return {
        chain: {
            "win_rate": round(v["wins"] / v["total"] * 100, 1) if v["total"] > 0 else None,
            "total": v["total"],
        }
        for chain, v in chains.items()
    }


def _best_score_threshold(score_stats: dict) -> Optional[int]:
    """
    Trouve le seuil de score optimal :
    - Cherche la tranche avec win rate ≥ 50% et au moins 3 trades
    - Prend la tranche la plus basse qui satisfait la condition
    """
    order = ["50-59", "60-69", "70-79", "80+"]
    score_mins = {"50-59": 50, "60-69": 60, "70-79": 70, "80+": 80}
    for bucket in order:
        stats = score_stats.get(bucket, {})
        wr = stats.get("win_rate")
        total = stats.get("total", 0)
        if wr is not None and wr >= 50 and total >= 3:
            return score_mins[bucket]
    return None


def run_optimization() -> dict:
    """
    Analyse les trades et ajuste les paramètres du bot.
    Retourne un dict avec les changements effectués et les insights.
    """
    import config

    trades = db.get_all_closed_trades()
    if len(trades) < MIN_TRADES_TO_OPTIMIZE:
        return {
            "status": "skipped",
            "reason": f"Pas assez de trades ({len(trades)}/{MIN_TRADES_TO_OPTIMIZE})",
        }

    state = _load_state()
    stats = db.get_portfolio_stats()
    score_stats = _win_rate_by_score_range(trades)
    chain_stats = _win_rate_by_chain(trades)

    changes = []
    insights = []
    current_score = state.get("min_score") or config.MIN_SCORE_TO_TRADE

    # ── Ajustement du seuil de score ────────────────────────────────────────────
    optimal_threshold = _best_score_threshold(score_stats)
    if optimal_threshold is not None and optimal_threshold != current_score:
        old = current_score
        new = optimal_threshold
        changes.append({
            "param": "MIN_SCORE_TO_TRADE",
            "old": old,
            "new": new,
            "reason": f"Win rate optimal à score ≥ {new}",
        })
        state["min_score"] = new
        config.MIN_SCORE_TO_TRADE = new
        logger.info("🔧 Ajustement: MIN_SCORE_TO_TRADE %d → %d", old, new)

    # ── Insights sur les scores ──────────────────────────────────────────────────
    for bucket, s in score_stats.items():
        if s["total"] >= 3 and s["win_rate"] is not None:
            emoji = "✅" if s["win_rate"] >= 55 else "⚠️" if s["win_rate"] >= 40 else "❌"
            insights.append(f"{emoji} Score {bucket}: {s['win_rate']}% win rate ({s['total']} trades)")

    # ── Insights sur les chains ──────────────────────────────────────────────────
    best_chain = None
    best_chain_wr = 0
    for chain, s in chain_stats.items():
        if s["total"] >= 3 and s["win_rate"] is not None:
            emoji = "✅" if s["win_rate"] >= 55 else "⚠️" if s["win_rate"] >= 40 else "❌"
            insights.append(f"{emoji} {chain.upper()}: {s['win_rate']}% win rate ({s['total']} trades)")
            if s["win_rate"] > best_chain_wr:
                best_chain_wr = s["win_rate"]
                best_chain = chain

    if best_chain:
        insights.append(f"🏆 Meilleure chain: <b>{best_chain.upper()}</b> ({best_chain_wr}%)")

    # ── Analyse stop-loss vs take-profit ────────────────────────────────────────
    sl_count = sum(1 for t in trades if t.get("close_reason") == "stop_loss")
    tp_count = sum(1 for t in trades if t.get("close_reason") == "take_profit")
    to_count = sum(1 for t in trades if t.get("close_reason") == "timeout")
    total = len(trades)
    if total > 0:
        if sl_count / total > 0.5:
            insights.append(f"⚠️ Trop de stop-loss ({sl_count}/{total}) — marché difficile ou seuil trop bas")
        if tp_count / total > 0.4:
            insights.append(f"🎯 Bons take-profits ({tp_count}/{total}) — strategy efficace")
        if to_count / total > 0.4:
            insights.append(f"⏱️ Beaucoup de timeouts ({to_count}/{total}) — envisager réduire MAX_HOLD_HOURS")

    # ── Sauvegarder l'état ───────────────────────────────────────────────────────
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["history"].append({
        "date": state["last_run"],
        "win_rate": stats.get("win_rate"),
        "total_trades": total,
        "changes": changes,
    })
    state["history"] = state["history"][-30:]
    _save_state(state)

    result = {
        "status": "ok",
        "trades_analyzed": total,
        "win_rate": stats.get("win_rate"),
        "changes": changes,
        "insights": insights,
        "score_stats": score_stats,
        "chain_stats": chain_stats,
    }

    if changes:
        logger.info("✅ Optimisation: %d changement(s) appliqué(s)", len(changes))
    else:
        logger.info("✅ Optimisation: paramètres déjà optimaux (win rate %.1f%%)", stats.get("win_rate", 0))

    return result
