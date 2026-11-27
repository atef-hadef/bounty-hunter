#!/usr/bin/env python3
"""Santé du système, dans le salon #statut.

- Alerte immédiate quand un flux plante (au plus une fois toutes les 6 h par flux),
  puis un message quand il refonctionne.
- Bilan quotidien (vers 8 h, heure de Tunis) : ce qui a été envoyé la veille,
  et les éventuels échecs. Si ce bilan n'arrive pas, le système est arrêté.

Variables d'environnement :
  DISCORD_STATUS_WEBHOOK_URL   salon #statut (sinon : seulement Telegram ou le log)
  STATUS_HOUR_UTC              heure du bilan quotidien, en UTC (défaut 7)

Usage :
  python status.py --outcomes "bounties=success,jobs=failure,startups=skipped" [--run-url URL]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import hunter

STATE_FILE = Path(__file__).with_name("status_state.json")
ALERT_COOLDOWN = timedelta(hours=6)
HEARTBEAT_HOUR_UTC = hunter.env_int("STATUS_HOUR_UTC", 7)
FEEDS = {"bounties": "💰 Bounties", "jobs": "🧑‍💼 Offres d'emploi", "startups": "🎯 Issues startup"}
RED, GREEN, GREY = 0xE74C3C, 0x2ECC71, 0x95A5A6


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_outcomes(text):
    out = {}
    for part in (text or "").split(","):
        name, _, value = part.partition("=")
        if name.strip() in FEEDS:
            out[name.strip()] = value.strip() or "skipped"
    return out


def post(embed, dry_run):
    if dry_run:
        hunter.log(f"[dry-run] #statut : {embed['title']}")
        return True
    webhook = os.environ.get("DISCORD_STATUS_WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not webhook and not (tg_token and tg_chat):
        hunter.log(f"⚠ DISCORD_STATUS_WEBHOOK_URL absent : « {embed['title']} » non envoyé.")
        return False
    try:
        if webhook:
            hunter.discord_post(webhook, {"embeds": [embed]})
        if tg_token and tg_chat:
            hunter.telegram_post(tg_token, tg_chat, f"<b>{embed['title']}</b>\n{embed.get('description', '')}")
        return True
    except requests.RequestException as e:
        hunter.log(f"⚠ envoi #statut impossible ({type(e).__name__})")
        return False


def handle_outcomes(state, outcomes, run_url, now, dry_run):
    """Alerte sur échec (avec délai entre deux alertes), message quand le flux se rétablit."""
    changed = False
    alerts = state.setdefault("last_alert", {})
    today = now.date().isoformat()
    for feed, outcome in outcomes.items():
        if outcome == "failure":
            day = state.setdefault("failures", {}).setdefault(today, {})
            day[feed] = day.get(feed, 0) + 1
            changed = True
            last = alerts.get(feed)
            if last and now - hunter.parse_dt(last) < ALERT_COOLDOWN:
                continue
            link = f"\n[Voir le journal de l'exécution]({run_url})" if run_url else ""
            if post({"title": f"⚠️ {FEEDS[feed]} : échec", "color": RED,
                     "description": "Ce flux a planté pendant ce passage. Les autres flux continuent, "
                                    "et il sera relancé automatiquement au prochain passage." + link},
                    dry_run):
                alerts[feed] = now.isoformat().replace("+00:00", "Z")
        elif outcome == "success" and feed in alerts:
            post({"title": f"✅ {FEEDS[feed]} : rétabli", "color": GREEN,
                  "description": "Ce flux fonctionne de nouveau normalement."}, dry_run)
            del alerts[feed]
            changed = True
    return changed


def daily_report(state, now, dry_run):
    """Un bilan par jour, au premier passage après HEARTBEAT_HOUR_UTC."""
    today = now.date().isoformat()
    if state.get("last_report") == today or now.hour < HEARTBEAT_HOUR_UTC:
        return False
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    sent = hunter.sent_on(yesterday)
    failures = state.get("failures", {}).get(yesterday, {})
    lines = [f"{label} : **{sent.get(feed, 0)}** envoyée(s)" for feed, label in FEEDS.items()]
    if failures:
        lines.append("\n⚠️ Échecs hier : " + ", ".join(f"{FEEDS[f]} ×{n}" for f, n in failures.items()))
    else:
        lines.append("\n✅ Aucun échec hier.")
    lines.append("Si ce bilan n'arrive pas un matin, le système est arrêté : regarde l'onglet Actions.")
    if post({"title": f"📊 Bilan du {yesterday} : système actif", "color": GREY,
             "description": "\n".join(lines)}, dry_run):
        state["last_report"] = today
        keep = sorted(state.get("failures", {}))[-14:]
        state["failures"] = {d: state["failures"][d] for d in keep}
        return True
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outcomes", default="")
    parser.add_argument("--run-url", default="")
    args = parser.parse_args()
    hunter.setup_console()
    dry_run = os.environ.get("DRY_RUN") == "1"

    outcomes = parse_outcomes(args.outcomes)
    hunter.log("Résultat des flux : " + ", ".join(f"{k}={v}" for k, v in outcomes.items()))
    now = datetime.now(timezone.utc)
    state = load_state()
    changed = handle_outcomes(state, outcomes, args.run_url, now, dry_run)
    changed = daily_report(state, now, dry_run) or changed
    if changed and not dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
