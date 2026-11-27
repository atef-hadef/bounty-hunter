#!/usr/bin/env python3
"""Offres d'emploi de startups : fil mensuel « Ask HN: Who is hiring? ».

Source : API publique Algolia de Hacker News (gratuite, sans clé).
Garde les offres qui contiennent tous les mots de JOB_REQUIRE et au moins un
mot de JOB_KEYWORDS, puis les envoie sur Discord/Telegram (10 max par passage).

Variables d'environnement :
  JOB_KEYWORDS                 défaut "python,django,fastapi,flask"
  JOB_REQUIRE                  défaut "remote"
  JOB_OPEN_ONLY                défaut 1 : seulement les offres ouvertes à l'international
  JOBS_DAILY                   offres envoyées par jour, les meilleures (défaut 3)
  DISCORD_JOBS_WEBHOOK_URL     salon dédié (obligatoire pour Discord)
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
JOBS_DAILY = hunter.env_int("JOBS_DAILY", 3)
OPEN_ONLY = (os.environ.get("JOB_OPEN_ONLY") or "1") == "1"
SUMMARY_LEN = 350
TOO_SENIOR_RE = re.compile(r"\b(?:staff|principal|director|vp|head of|cto|architect)\b", re.I)
JUNIOR_RE = re.compile(r"\b(?:junior|mid[- ]?level|intermediate|new grad|entry[- ]level|any level)\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LINK_RE = re.compile(r"https?://[^\s<>()]+")


def words(name, default):
    return [w.strip().lower() for w in (os.environ.get(name) or default).split(",") if w.strip()]


KEYWORDS = words("JOB_KEYWORDS", "python,django,fastapi,flask")
REQUIRE = words("JOB_REQUIRE", "remote")

_thread_cache = {}


def latest_thread():
    """Renvoie (titre, liste des offres de premier niveau) du dernier fil « Who is hiring »."""
    if "thread" in _thread_cache:
        return _thread_cache["thread"]
    hits = hunter.http_get_json(f"{ALGOLIA}/search_by_date",
                                {"tags": "story,author_whoishiring", "hitsPerPage": 10}, timeout=30)["hits"]
    story = next(h for h in hits if h["title"].lower().startswith("ask hn: who is hiring"))
    item = hunter.http_get_json(f"{ALGOLIA}/items/{story['objectID']}", timeout=90)
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
    if OPEN_ONLY and accessibility(text) != 2:
        return False
    header = text.split("\n", 1)[0]
    if TOO_SENIOR_RE.search(header) and not JUNIOR_RE.search(header):
        return False
    if not all(re.search(rf"\b{re.escape(w)}\b", low) for w in REQUIRE):
        return False
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in KEYWORDS)


def job_score(text):
    """Meilleure offre = ouverte au monde, Python dans l'intitulé, niveau accessible."""
    header, low = text.split("\n", 1)[0], text.lower()
    s = 20 if accessibility(text) == 2 else 0
    s += 15 if any(re.search(rf"\b{re.escape(w)}\b", header.lower()) for w in KEYWORDS) else 0
    s += 10 if JUNIOR_RE.search(text) else 0
    s += 5 if re.search(r"\bcontract(?:or)?\b|\bfreelance\b", low) else 0
    s += 5 if re.search(r"open[- ]source|github", low) else 0  # tes PR open source comptent
    s -= 10 if re.search(r"\b(?:10|8|7)\+? years\b", low) else 0
    return s


def how_to_apply(text):
    email = EMAIL_RE.search(text)
    if email:
        return f"Email : {email.group(0)}"
    for link in LINK_RE.findall(text):
        if re.search(r"jobs?|careers?|apply|hiring|lever\.co|greenhouse|ashbyhq|workable|forms?", link, re.I):
            return f"Lien : {link}"
    return "Voir l'offre complète sur HN (lien du titre)"


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
            "apply": how_to_apply(text),
            "score": job_score(text),
        })
    jobs.sort(key=lambda j: j["score"], reverse=True)
    hunter.log(f"{len(jobs)} nouvelle(s) offre(s) correspondante(s).")
    return jobs


def discord_payload(j):
    return {
        "content": f"🧑‍💼 **Offre startup** : {j['company']}"
                   + (" · 🌍 ouverte à l'international" if j["open"] else ""),
        "embeds": [{"title": j["header"], "url": j["url"],
                    "description": j["summary"][:4000], "color": 0x3498DB,
                    "fields": [
                        {"name": "✅ Déjà vérifié pour toi", "value":
                         "• Télétravail ouvert à l'international (pas réservé aux US)\n"
                         "• Python dans l'offre, pas un poste de direction\n"
                         "• Publiée ce mois-ci sur HN Who is hiring", "inline": False},
                        {"name": "👉 Postuler maintenant", "value":
                         f"{j['apply']}\nMessage court en anglais : 2 lignes sur toi + "
                         "lien vers tes PR open source + ton GitHub.", "inline": False},
                    ],
                    "footer": {"text": f"Score {j['score']} · Source : Hacker News · Who is hiring"}}],
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
        print(f"\n• [{j['score']}] {j['header']}\n  {j['url']}\n  {j['apply']}")

    if args.dry_run or os.environ.get("DRY_RUN") == "1":
        hunter.log("\n[dry-run] aucune notification envoyée, seen_jobs.json inchangé.")
        return 0
    if args.seed:
        hunter.save_seen(seen_list + [j["url"] for j in jobs], SEEN_FILE)
        hunter.log(f"\n[seed] {len(jobs)} offre(s) marquée(s) comme vues.")
        return 0
    left = hunter.quota_left("jobs", JOBS_DAILY)
    if jobs and not left:
        hunter.log(f"Quota du jour atteint ({JOBS_DAILY}) : les suivantes attendront demain.")
    if jobs and left:
        sent = hunter.send_notifications(jobs[:left], discord_payload, telegram_text,
                                         "DISCORD_JOBS_WEBHOOK_URL")
        hunter.quota_use("jobs", len(sent))
        if sent:
            hunter.save_seen(seen_list + [j["url"] for j in sent], SEEN_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
