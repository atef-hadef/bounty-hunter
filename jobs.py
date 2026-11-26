#!/usr/bin/env python3
"""Offres d'emploi de startups : fil mensuel « Ask HN: Who is hiring? ».

Source : API publique Algolia de Hacker News (gratuite, sans clé).
Garde les offres qui contiennent tous les mots de JOB_REQUIRE et au moins un
mot de JOB_KEYWORDS, puis les envoie sur Discord/Telegram (10 max par passage).

Variables d'environnement :
  JOB_KEYWORDS                 défaut "python,django,fastapi,flask"
  JOB_REQUIRE                  défaut "remote"
  DISCORD_JOBS_WEBHOOK_URL     salon dédié (sinon DISCORD_WEBHOOK_URL)
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID

Usage :
  python jobs.py [--dry-run | --seed]
"""

import argparse
import html
import os
import re
import sys
from pathlib import Path

import requests

import hunter

ALGOLIA = "https://hn.algolia.com/api/v1"
SEEN_FILE = Path(__file__).with_name("seen_jobs.json")
MAX_NOTIFICATIONS = 10
SUMMARY_LEN = 350


def words(name, default):
    return [w.strip().lower() for w in (os.environ.get(name) or default).split(",") if w.strip()]


KEYWORDS = words("JOB_KEYWORDS", "python,django,fastapi,flask")
REQUIRE = words("JOB_REQUIRE", "remote")

_thread_cache = {}


def latest_thread():
    """Renvoie (titre, liste des offres de premier niveau) du dernier fil « Who is hiring »."""
    if "thread" in _thread_cache:
        return _thread_cache["thread"]
    hits = requests.get(f"{ALGOLIA}/search_by_date",
                        params={"tags": "story,author_whoishiring", "hitsPerPage": 10},
                        timeout=30).json()["hits"]
    story = next(h for h in hits if h["title"].lower().startswith("ask hn: who is hiring"))
    item = requests.get(f"{ALGOLIA}/items/{story['objectID']}", timeout=90).json()
    posts = [c for c in item.get("children", []) if c.get("text")]
    _thread_cache["thread"] = (story["title"], posts)
    return _thread_cache["thread"]


def to_text(raw):
    text = re.sub(r"<p>", "\n", raw or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def company_of(text):
    first = text.split("\n", 1)[0]
    return first.split("|", 1)[0].strip()


def hiring_companies():
    """Noms (minuscules) des entreprises qui recrutent ce mois-ci, pour hunter.py."""
    _, posts = latest_thread()
    names = set()
    for p in posts:
        name = company_of(to_text(p["text"])).lower()
        name = re.sub(r"\s*\(.*?\)", "", name).strip()
        if 1 < len(name) <= 40:
            names.add(name)
    return frozenset(names)


OPEN_RE = re.compile(
    r"worldwide|global|\bworld\b|anywhere|any time ?zone|europe|emea|africa|international"
    r"|\bcet\b|\butc\b|\bgmt\b", re.I)
REMOTE_SCOPE_RE = re.compile(r"remote\s*[(:]\s*([^)|\n]*)", re.I)
HARD_LOCK_RE = re.compile(
    r"\b(?:us|u\.s\.?) citizens?|security clearance|\b(?:us|u\.s\.?)[\s-]only\b"
    r"|must (?:be|reside|live) (?:based |located )?in the (?:us|u\.s\.?|united states)", re.I)


def accessibility(text):
    """2 = ouvert au monde/Europe/Afrique, 1 = non précisé, 0 = réservé à un pays/zone."""
    if HARD_LOCK_RE.search(text):
        return 0
    header = text.split("\n", 1)[0]
    scope = REMOTE_SCOPE_RE.search(header)
    if scope:  # « REMOTE (US/Canada) », « REMOTE (EEA) », « REMOTE (anywhere) »…
        return 2 if OPEN_RE.search(scope.group(1)) else 0
    return 2 if OPEN_RE.search(header) else 1


def is_job_post(text):
    """Écarte les candidats qui postent dans le mauvais fil (« Location: … »)."""
    head = text.lstrip().lower()
    return "|" in text.split("\n", 1)[0] and not head.startswith(("location:", "seeking"))


def matches(text):
    low = text.lower()
    if not is_job_post(text):
        return False
    if os.environ.get("JOB_ALLOW_US_ONLY") != "1" and accessibility(text) == 0:
        return False
    if not all(re.search(rf"\b{re.escape(w)}\b", low) for w in REQUIRE):
        return False
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in KEYWORDS)


def find_jobs(seen):
    title, posts = latest_thread()
    hunter.log(f"{title} : {len(posts)} offres.")
    jobs = []
    for p in posts:
        url = f"https://news.ycombinator.com/item?id={p['id']}"
        if url in seen:
            continue
        text = to_text(p["text"])
        if not matches(text):
            continue
        header, _, body = text.partition("\n")
        summary = re.sub(r"\s+", " ", body).strip()
        if len(summary) > SUMMARY_LEN:
            summary = summary[:SUMMARY_LEN].rsplit(" ", 1)[0] + "…"
        jobs.append({
            "url": url,
            "company": company_of(text),
            "header": header[:256],
            "summary": summary or "(voir l'offre)",
            "created_at": p.get("created_at", ""),
            "open": accessibility(text) == 2,
        })
    jobs.sort(key=lambda j: j["open"], reverse=True)  # ouvertes à l'international d'abord
    hunter.log(f"{len(jobs)} nouvelle(s) offre(s) correspondante(s).")
    return jobs


def discord_payload(j):
    return {
        "content": f"🧑‍💼 **Offre startup** : {j['company']}"
                   + (" · 🌍 ouverte à l'international" if j["open"] else ""),
        "embeds": [{"title": j["header"], "url": j["url"],
                    "description": j["summary"][:4000], "color": 0x3498DB,
                    "footer": {"text": "Source : Hacker News · Who is hiring"}}],
    }


def telegram_text(j):
    e = html.escape
    return (f"🧑‍💼 <b>Offre startup : {e(j['company'])}</b>\n\n{e(j['header'])}\n"
            f"🔗 {e(j['url'])}\n\n{e(j['summary'])}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    hunter.setup_console()

    seen_list = hunter.load_seen(SEEN_FILE)
    jobs = find_jobs(set(seen_list))
    print(f"\n=== {len(jobs)} offre(s) ===")
    for j in jobs:
        print(f"\n• {j['header']}\n  {j['url']}")

    if args.dry_run or os.environ.get("DRY_RUN") == "1":
        hunter.log("\n[dry-run] aucune notification envoyée, seen_jobs.json inchangé.")
        return 0
    if args.seed:
        hunter.save_seen(seen_list + [j["url"] for j in jobs], SEEN_FILE)
        hunter.log(f"\n[seed] {len(jobs)} offre(s) marquée(s) comme vues.")
        return 0
    if jobs:
        webhook = os.environ.get("DISCORD_JOBS_WEBHOOK_URL", "").strip() or None
        sent = hunter.send_notifications(jobs[:MAX_NOTIFICATIONS], discord_payload, telegram_text, webhook)
        if sent:
            hunter.save_seen(seen_list + [j["url"] for j in sent], SEEN_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
