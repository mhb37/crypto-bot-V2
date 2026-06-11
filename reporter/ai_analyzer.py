"""
Analyseur IA — génère une analyse intelligente des performances et l'envoie sur Telegram.
Fonctionne sans clé API externe : analyse déterministe enrichie avec patterns avancés.
Si GROQ_API_KEY est configurée, utilise Groq (llama3, gratuit) pour une analyse narrative.
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import database as db

logger = logging.getLogger(__name__)


def _build_performance_summary(trades: list[dict], stats: dict) -> str:
    """Construit un résumé structuré des performances pour l'analyse."""
    total = stats.get("total_trades", 0)
    if total == 0:
        return "Aucun trade fermé pour l'instant."

    win_rate = stats.get("win_rate", 0)
    total_pnl = stats.get("total_pnl_usd", 0)
    avg_pnl = stats.get("avg_pnl_pct", 0)
    best = stats.get("best_trade_pct", 0)
    worst = stats.get("worst_trade_pct", 0)

    # Analyse par raison de fermeture
    reasons = {}
    for t in trades:
        r = t.get("close_reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1

    # Analyse par chain
    chains: dict = {}
    for t in trades:
        c = t.get("chain", "?")
        if c not in chains:
            chains[c] = {"wins": 0, "total": 0, "pnl": 0}
        chains[c]["total"] += 1
        if t.get("success"):
            chains[c]["wins"] += 1
        chains[c]["pnl"] += t.get("pnl_usd", 0) or 0

    # Analyse temporelle (heure du jour)
    hour_perf: dict = {}
    for t in trades:
        try:
            open_at = t.get("open_at", "")
            h = int(open_at[11:13]) if len(open_at) >= 13 else -1
            if h < 0:
                continue
            bucket = f"{(h // 4) * 4:02d}h-{(h // 4) * 4 + 4:02d}h"
            if bucket not in hour_perf:
                hour_perf[bucket] = {"wins": 0, "total": 0}
            hour_perf[bucket]["total"] += 1
            if t.get("success"):
                hour_perf[bucket]["wins"] += 1
        except Exception:
            pass

    # Trouver la meilleure heure
    best_hour = max(
        [(k, v["wins"] / v["total"] if v["total"] >= 2 else 0) for k, v in hour_perf.items()],
        key=lambda x: x[1],
        default=("N/A", 0)
    )

    lines = [
        f"Trades: {total} | Win rate: {win_rate}% | PnL total: ${total_pnl:+.2f}",
        f"PnL moyen: {avg_pnl:+.1f}% | Meilleur: {best:+.1f}% | Pire: {worst:+.1f}%",
        f"Fermetures: {json.dumps(reasons)}",
        f"Meilleure plage horaire: {best_hour[0]} ({best_hour[1]:.0%} win rate)",
    ]
    for chain, v in chains.items():
        wr = v['wins'] / v['total'] * 100 if v['total'] > 0 else 0
        lines.append(f"Chain {chain}: {v['total']} trades, {wr:.0f}% win rate, ${v['pnl']:+.2f} PnL")

    return "\n".join(lines)


def _deterministic_analysis(trades: list[dict], stats: dict, optimizer_result: dict) -> str:
    """
    Génère une analyse intelligente sans API externe.
    Retourne un message Telegram formaté HTML.
    """
    total = stats.get("total_trades", 0)
    win_rate = stats.get("win_rate", 0)
    total_pnl = stats.get("total_pnl_usd", 0)

    lines = ["🤖 <b>Analyse IA des performances</b>\n"]

    # ── Diagnostic global ────────────────────────────────────────────────────────
    if total == 0:
        lines.append("📊 Pas encore assez de trades pour analyser.\nLaissez le bot tourner — il apprend au fur et à mesure !")
        return "\n".join(lines)

    if win_rate >= 60:
        lines.append(f"✅ <b>Stratégie efficace</b> — {win_rate}% de trades gagnants")
    elif win_rate >= 45:
        lines.append(f"⚠️ <b>Performance correcte</b> — {win_rate}% de trades gagnants (cible: 55%+)")
    else:
        lines.append(f"❌ <b>Attention</b> — seulement {win_rate}% de trades gagnants. Ajustements nécessaires.")

    lines.append(f"💰 PnL cumulé: <b>${total_pnl:+.2f}</b> sur {total} trades\n")

    # ── Insights de l'optimiseur ─────────────────────────────────────────────────
    insights = optimizer_result.get("insights", [])
    if insights:
        lines.append("📈 <b>Patterns détectés:</b>")
        for insight in insights[:5]:
            lines.append(f"  {insight}")
        lines.append("")

    # ── Changements appliqués ────────────────────────────────────────────────────
    changes = optimizer_result.get("changes", [])
    if changes:
        lines.append("🔧 <b>Paramètres ajustés automatiquement:</b>")
        for c in changes:
            lines.append(f"  • {c['param']}: {c['old']} → <b>{c['new']}</b>")
            lines.append(f"    ↳ {c['reason']}")
        lines.append("")
    else:
        lines.append("✅ <b>Paramètres déjà optimaux</b> — aucun ajustement nécessaire\n")

    # ── Recommandations concrètes ────────────────────────────────────────────────
    lines.append("💡 <b>Recommandations:</b>")
    recommendations = _generate_recommendations(trades, stats, optimizer_result)
    for rec in recommendations:
        lines.append(f"  • {rec}")

    return "\n".join(lines)


def _generate_recommendations(trades: list[dict], stats: dict, optimizer_result: dict) -> list[str]:
    """Génère des recommandations basées sur les patterns."""
    recs = []
    win_rate = stats.get("win_rate", 0)
    total = stats.get("total_trades", 0)

    if total < 5:
        recs.append("Laissez le bot faire plus de trades avant d'optimiser (cible: 20+)")
        return recs

    # Analyse des stop-loss
    sl_count = sum(1 for t in trades if t.get("close_reason") == "stop_loss")
    tp_count = sum(1 for t in trades if t.get("close_reason") == "take_profit")
    to_count = sum(1 for t in trades if t.get("close_reason") == "timeout")

    if total > 0:
        sl_rate = sl_count / total
        tp_rate = tp_count / total
        to_rate = to_count / total

        if sl_rate > 0.5:
            recs.append("Trop de stop-loss → augmentez MIN_SCORE_TO_TRADE ou réduisez POSITION_SIZE_PCT")
        if tp_rate > 0.35:
            recs.append("Bon taux de take-profit → envisagez augmenter TAKE_PROFIT_PCT à 60-70%")
        if to_rate > 0.4:
            recs.append("Beaucoup de timeouts → réduisez MAX_HOLD_HOURS de 48h à 24h")

    # Analyse par chain
    chain_stats = optimizer_result.get("chain_stats", {})
    weak_chains = [c for c, s in chain_stats.items() if s.get("win_rate") is not None and s["win_rate"] < 35 and s["total"] >= 3]
    strong_chains = [c for c, s in chain_stats.items() if s.get("win_rate") is not None and s["win_rate"] >= 60 and s["total"] >= 3]

    if weak_chains:
        recs.append(f"Chain(s) faible(s): {', '.join(weak_chains).upper()} — envisagez les retirer de TARGET_CHAINS")
    if strong_chains:
        recs.append(f"Chain(s) forte(s): {', '.join(strong_chains).upper()} — concentrez-y plus de capital")

    # Win rate global
    if win_rate < 40 and total >= 10:
        recs.append("Win rate faible → attendez le réentraînement ML (il s'améliore avec le temps)")
    elif win_rate >= 65:
        recs.append("Excellentes performances ! Vous pouvez augmenter MAX_OPEN_POSITIONS à 7-8")

    if not recs:
        recs.append("Bot bien calibré — continuez à collecter des données pour affiner le ML")

    return recs[:4]


async def _groq_analysis(summary: str) -> Optional[str]:
    """Appel optionnel à Groq (llama3 gratuit) si GROQ_API_KEY configurée."""
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        return None

    try:
        import aiohttp
        prompt = f"""Tu es un expert en trading de meme coins. Voici les stats du bot:

{summary}

En 3-4 phrases courtes, donne une analyse claire et des conseils concrets pour améliorer les performances. Sois direct et pratique."""

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.7,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.debug("Groq API error: %s", e)
    return None


async def run_ai_analysis(optimizer_result: dict) -> str:
    """
    Analyse complète : déterministe + Groq optionnel.
    Retourne le message Telegram formaté.
    """
    trades = db.get_all_closed_trades()
    stats = db.get_portfolio_stats()

    base_analysis = _deterministic_analysis(trades, stats, optimizer_result)

    # Enrichissement Groq si clé disponible
    if os.getenv("GROQ_API_KEY"):
        summary = _build_performance_summary(trades, stats)
        groq_text = await _groq_analysis(summary)
        if groq_text:
            base_analysis += f"\n\n🧠 <b>Analyse narrative:</b>\n{groq_text}"

    return base_analysis
