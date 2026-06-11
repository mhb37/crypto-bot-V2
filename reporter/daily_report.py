"""
Génération du rapport quotidien — stats, PnL, erreurs apprises.
Sauvegarde à la fois en JSON et en HTML.
"""
import json
import logging
import os
from datetime import datetime, timezone

import database as db
from analyzer.model import get_model

logger = logging.getLogger(__name__)

import config as _cfg
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(_cfg.DB_PATH)), "reports")


def generate_daily_report() -> dict:
    """Génère le rapport quotidien complet et le sauvegarde (JSON + HTML)."""
    now = datetime.now(timezone.utc)
    stats = db.get_portfolio_stats()
    recent_trades = db.get_closed_trades(limit=20)
    model = get_model()

    losing_trades = [t for t in recent_trades if (t.get("pnl_pct") or 0) < 0]
    error_patterns = _analyze_error_patterns(losing_trades)

    sorted_trades = sorted(recent_trades, key=lambda x: x.get("pnl_pct") or 0, reverse=True)
    best_trades = sorted_trades[:3]
    worst_trades = sorted_trades[-3:] if len(sorted_trades) >= 3 else sorted_trades

    close_reasons: dict = {}
    for t in recent_trades:
        r = t.get("close_reason", "unknown")
        close_reasons[r] = close_reasons.get(r, 0) + 1

    report = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "stats": stats,
        "error_patterns": error_patterns,
        "best_trades": [_format_trade_summary(t) for t in best_trades],
        "worst_trades": [_format_trade_summary(t) for t in worst_trades],
        "close_reasons": close_reasons,
        "model_mode": "ML" if model.is_trained else "Heuristique",
        "model_samples": model.training_samples,
        "lessons_learned": _extract_lessons(error_patterns, stats),
    }

    os.makedirs(REPORTS_DIR, exist_ok=True)
    _save_json(report)
    _save_html(report)

    return report


# ── Sauvegarde ────────────────────────────────────────────────────────────────

def _save_json(report: dict):
    path = f"{REPORTS_DIR}/report_{report['date']}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        logger.info("Rapport JSON: %s", path)
    except Exception as e:
        logger.error("Erreur sauvegarde JSON: %s", e)


def _save_html(report: dict):
    """Génère un rapport HTML autonome (aucune dépendance externe)."""
    path = f"{REPORTS_DIR}/report_{report['date']}.html"
    try:
        html = _build_html(report)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Rapport HTML: %s", path)
    except Exception as e:
        logger.error("Erreur sauvegarde HTML: %s", e)


def _build_html(report: dict) -> str:
    stats = report.get("stats", {})
    pnl = stats.get("total_pnl_usd", 0) or 0
    win_rate = stats.get("win_rate", 0) or 0
    total_trades = stats.get("total_trades", 0) or 0
    wins = stats.get("wins", 0) or 0
    losses = stats.get("losses", 0) or 0
    pnl_color = "#22c55e" if pnl >= 0 else "#ef4444"
    pnl_sign = "+" if pnl >= 0 else ""

    best_rows = "".join(
        f"<tr><td>{t.get('symbol','?')}</td>"
        f"<td style='color:#22c55e'>{t.get('pnl_pct',0):+.2f}%</td>"
        f"<td>${t.get('pnl_usd',0):+.2f}</td>"
        f"<td>{t.get('close_reason','?')}</td></tr>"
        for t in report.get("best_trades", [])
    )
    worst_rows = "".join(
        f"<tr><td>{t.get('symbol','?')}</td>"
        f"<td style='color:#ef4444'>{t.get('pnl_pct',0):+.2f}%</td>"
        f"<td>${t.get('pnl_usd',0):+.2f}</td>"
        f"<td>{t.get('close_reason','?')}</td></tr>"
        for t in report.get("worst_trades", [])
    )
    patterns_html = "".join(
        f"<li>{p}</li>" for p in report.get("error_patterns", [])
    )
    lessons_html = "".join(
        f"<li>{l}</li>" for l in report.get("lessons_learned", [])
    )
    reasons_html = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in report.get("close_reasons", {}).items()
    )

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Rapport Bot — {report['date']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; padding: 24px; }}
    h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
    .subtitle {{ color: #64748b; margin-bottom: 32px; font-size: 0.9rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 32px; }}
    .card {{ background: #1e293b; border-radius: 12px; padding: 20px; }}
    .card .label {{ color: #94a3b8; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
    .card .value {{ font-size: 1.8rem; font-weight: 700; }}
    .section {{ background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
    .section h2 {{ font-size: 1rem; margin-bottom: 16px; color: #94a3b8; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; color: #64748b; font-size: 0.75rem; text-transform: uppercase; padding: 6px 12px; border-bottom: 1px solid #334155; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #1e293b; font-size: 0.875rem; }}
    tr:last-child td {{ border-bottom: none; }}
    ul {{ padding-left: 20px; }}
    li {{ margin-bottom: 8px; font-size: 0.875rem; line-height: 1.5; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }}
    .badge-ml {{ background: #1d4ed8; color: #bfdbfe; }}
    .badge-heuristic {{ background: #374151; color: #9ca3af; }}
    .footer {{ color: #475569; font-size: 0.75rem; text-align: center; margin-top: 32px; }}
  </style>
</head>
<body>
  <h1>🤖 Rapport Meme Coin Bot</h1>
  <div class="subtitle">Généré le {report['date']} à {report['generated_at'][:19].replace('T', ' ')} UTC &nbsp;|&nbsp;
    Modèle: <span class="badge {'badge-ml' if report.get('model_mode') == 'ML' else 'badge-heuristic'}">{report.get('model_mode','?')} ({report.get('model_samples', 0)} samples)</span>
  </div>

  <div class="grid">
    <div class="card">
      <div class="label">PnL Total</div>
      <div class="value" style="color:{pnl_color}">{pnl_sign}{pnl:.2f} $</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value">{win_rate:.1f}%</div>
    </div>
    <div class="card">
      <div class="label">Trades</div>
      <div class="value">{total_trades}</div>
    </div>
    <div class="card">
      <div class="label">Wins / Losses</div>
      <div class="value"><span style="color:#22c55e">{wins}</span> / <span style="color:#ef4444">{losses}</span></div>
    </div>
    <div class="card">
      <div class="label">Positions ouvertes</div>
      <div class="value">{stats.get('open_positions', 0)}</div>
    </div>
    <div class="card">
      <div class="label">Meilleur trade</div>
      <div class="value" style="color:#22c55e">{stats.get('best_trade_pct', 0) or 0:+.1f}%</div>
    </div>
  </div>

  <div class="section">
    <h2>🏆 Meilleurs trades</h2>
    <table>
      <tr><th>Token</th><th>PnL %</th><th>PnL USD</th><th>Raison</th></tr>
      {best_rows if best_rows else '<tr><td colspan="4" style="color:#64748b">Aucun</td></tr>'}
    </table>
  </div>

  <div class="section">
    <h2>💔 Pires trades</h2>
    <table>
      <tr><th>Token</th><th>PnL %</th><th>PnL USD</th><th>Raison</th></tr>
      {worst_rows if worst_rows else '<tr><td colspan="4" style="color:#64748b">Aucun</td></tr>'}
    </table>
  </div>

  <div class="section">
    <h2>📊 Raisons de clôture</h2>
    <table>
      <tr><th>Raison</th><th>Nombre</th></tr>
      {reasons_html if reasons_html else '<tr><td colspan="2" style="color:#64748b">Aucun trade fermé</td></tr>'}
    </table>
  </div>

  <div class="section">
    <h2>⚠️ Patterns d'erreur détectés</h2>
    <ul>{patterns_html}</ul>
  </div>

  <div class="section">
    <h2>📚 Leçons apprises</h2>
    <ul>{lessons_html}</ul>
  </div>

  <div class="footer">Meme Coin Bot — rapport du {report['date']}</div>
</body>
</html>"""


# ── Analyse ───────────────────────────────────────────────────────────────────

def _analyze_error_patterns(losing_trades: list) -> list:
    if not losing_trades:
        return ["Aucun trade perdant récent — continuez ainsi!"]

    patterns = []

    timeout_losses = [t for t in losing_trades if t.get("close_reason") == "timeout"]
    if len(timeout_losses) > max(len(losing_trades) * 0.4, 1):
        patterns.append(
            f"⚠️  {len(timeout_losses)} trades fermés par timeout — "
            "considérer réduire MAX_HOLD_HOURS ou MIN_SCORE_TO_TRADE"
        )

    high_score_losses = [t for t in losing_trades if (t.get("ai_score") or 0) >= 75]
    if high_score_losses:
        patterns.append(
            f"⚠️  {len(high_score_losses)} trades score ≥75 se sont avérés perdants — "
            "réévaluer les features du modèle"
        )

    sl_trades = [t for t in losing_trades if t.get("close_reason") == "stop_loss"]
    if len(sl_trades) > 3:
        patterns.append(
            f"ℹ️  {len(sl_trades)} stop-loss déclenchés — "
            "envisager un stop-loss plus large ou un score minimum plus élevé"
        )

    if not patterns:
        patterns.append("✅ Aucun pattern d'erreur significatif détecté")

    return patterns


def _extract_lessons(patterns: list, stats: dict) -> list:
    lessons = []
    win_rate = stats.get("win_rate", 0) or 0

    if win_rate < 40:
        lessons.append("Win rate bas (<40%) → augmenter MIN_SCORE_TO_TRADE à 75+")
    elif win_rate > 65:
        lessons.append(
            "Win rate élevé (>65%) → possible de réduire MIN_SCORE_TO_TRADE pour plus d'opportunités"
        )

    avg_pnl = stats.get("avg_pnl_pct", 0) or 0
    if avg_pnl < -5:
        lessons.append("PnL moyen négatif → reconsidérer les critères de filtre de tokens")

    if not lessons:
        lessons.append("Paramètres actuels semblent bien calibrés — continuez la collecte de données")

    return lessons


def _format_trade_summary(trade: dict) -> dict:
    return {
        "symbol": trade.get("token_symbol"),
        "pnl_pct": round(trade.get("pnl_pct") or 0, 2),
        "pnl_usd": round(trade.get("pnl_usd") or 0, 2),
        "score": trade.get("ai_score"),
        "close_reason": trade.get("close_reason"),
    }
