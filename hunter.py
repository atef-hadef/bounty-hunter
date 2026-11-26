#!/usr/bin/env python3
"""Veille de GitHub Bounties (issues Python ouvertes avec bounty / Algora / Opire).

Variables d'environnement :
  GITHUB_TOKEN           token GitHub (fortement recommandé : sans lui, 60 req/h)
  DISCORD_WEBHOOK_URL    webhook Discord (optionnel)
  TELEGRAM_BOT_TOKEN     token du bot Telegram (optionnel)
  TELEGRAM_CHAT_ID       chat Telegram (optionnel, avec le token)
  DRY_RUN=1              équivalent à --dry-run

Usage :
  python hunter.py              # recherche, notifie, met à jour seen_issues.json
  python hunter.py --dry-run    # recherche et affiche, sans notifier ni écrire
  python hunter.py --seed       # marque les résultats actuels comme vus, sans notifier
  EXCLUDED_REPOS="a/b,c/d"      # dépôts supplémentaires à ignorer
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.github.com"
SEEN_FILE = Path(__file__).with_name("seen_issues.json")
SEEN_MAX = 5000  # borne la taille du fichier

SEARCH_QUERIES = [
    "is:issue is:open language:python label:bounty",
    "is:issue is:open language:python algora",
    "is:issue is:open language:python opire",
]
SEARCH_PAGES = 2  # 2 x 100 résultats max par requête

MAX_COMMENTS = 8  # strictement moins de
MIN_STARS, MAX_STARS = 50, 3000
MAX_PUSH_AGE = timedelta(days=14)
SUMMARY_LEN = 280
MAX_NOTIFICATIONS = 10  # par passage ; le reste attend le passage suivant

# Dépôts ignorés (bounties fictives / à 0 $). Complétable via la variable
# d'environnement EXCLUDED_REPOS="owner/repo,owner2/repo2".
EXCLUDED_REPOS = {
    "ikalus1988/misakanet",
    "phankhoa95/oss-bot-trap-benchmark",
}
EXCLUDED_REPOS |= {
    r.strip().lower() for r in os.environ.get("EXCLUDED_REPOS", "").split(",") if r.strip()
}

# Bounties explicitement non rémunérées.
NO_MONEY_RE = re.compile(
    r"\$\s?0(?![.,\d])"            # "$0" mais pas "$0.50" ni "$05"
    r"|\b0\s?(?:usd|\$)(?![.,\d])"  # "0 USD", "0$"
    r"|pays? no money|zero bounty|no monetary|unpaid bounty|credit only",
    re.I,
)


class RateLimited(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- GitHub

def make_session():
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bounty-hunter-script",
    })
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    else:
        log("⚠ GITHUB_TOKEN absent : quota limité (10 recherches/min, 60 appels/h).")
    return s


def gh_get(session, path, params=None):
    url = path if path.startswith("http") else f"{API}{path}"
    for attempt in range(3):
        r = session.get(url, params=params, timeout=30)
        if r.status_code in (403, 429) and (
            r.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in r.text.lower()
        ):
            reset = int(r.headers.get("X-RateLimit-Reset", "0") or 0)
            wait = max(0, reset - int(time.time())) + 1 if reset else int(r.headers.get("Retry-After", "60"))
            if wait <= 65 and attempt < 2:
                log(f"  rate limit, attente {wait}s…")
                time.sleep(wait)
                continue
            raise RateLimited(f"quota GitHub épuisé (reset dans {wait}s)")
        if r.status_code >= 500 and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"échec après plusieurs essais : {url}")


def search_issues(session):
    found = {}
    for q in SEARCH_QUERIES:
        for page in range(1, SEARCH_PAGES + 1):
            data = gh_get(session, "/search/issues", {
                "q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page,
            })
            items = data.get("items", [])
            for it in items:
                if "pull_request" not in it:  # sécurité : uniquement des issues
                    found.setdefault(it["html_url"], it)
            log(f"  « {q} » page {page} : {len(items)} résultats")
            if len(items) < 100:
                break
    return list(found.values())


def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def has_linked_pr(session, owner, repo, number):
    """Vrai si une PR référence l'issue ou y est connectée (timeline API)."""
    page = 1
    while True:
        events = gh_get(session, f"/repos/{owner}/{repo}/issues/{number}/timeline",
                        {"per_page": 100, "page": page})
        for ev in events:
            kind = ev.get("event")
            if kind in ("connected", "cross-referenced"):
                src = (ev.get("source") or {}).get("issue") or {}
                if kind == "connected" or "pull_request" in src:
                    return True
        if len(events) < 100:
            return False
        page += 1


def summarize(body):
    text = body or ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)      # commentaires HTML
    text = re.sub(r"```.*?```", " ", text, flags=re.S)       # blocs de code
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)        # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)     # liens -> texte
    text = re.sub(r"<[^>]+>", " ", text)                     # balises HTML
    text = re.sub(r"[#>*_`|~-]{1,}", " ", text)              # syntaxe markdown
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "(pas de description)"
    return text if len(text) <= SUMMARY_LEN else text[:SUMMARY_LEN].rsplit(" ", 1)[0] + "…"


def find_candidates(session, seen):
    issues = search_issues(session)
    log(f"{len(issues)} issues uniques trouvées.")
    now = datetime.now(timezone.utc)
    repo_cache = {}
    stats = {"seen": 0, "comments": 0, "excluded_repo": 0, "no_money": 0, "stars": 0, "push": 0, "linked_pr": 0, "repo_err": 0}
    kept = []

    # Filtres du moins coûteux au plus coûteux.
    try:
        _filter_issues(session, issues, seen, now, repo_cache, stats, kept)
    except RateLimited as e:
        log(f"⚠ {e} : arrêt anticipé, {len(kept)} issue(s) déjà validée(s) conservée(s).")

    log("Écartées : " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return kept


def _filter_issues(session, issues, seen, now, repo_cache, stats, kept):
    for it in issues:
        url = it["html_url"]
        if url in seen:
            stats["seen"] += 1
            continue
        if it.get("comments", 0) >= MAX_COMMENTS:
            stats["comments"] += 1
            continue
        repo_full = it["repository_url"].split("/repos/", 1)[1]
        if repo_full.lower() in EXCLUDED_REPOS:
            stats["excluded_repo"] += 1
            continue
        if NO_MONEY_RE.search(f"{it.get('title', '')}\n{it.get('body') or ''}"):
            stats["no_money"] += 1
            continue

        repo_url = it["repository_url"]
        if repo_url not in repo_cache:
            try:
                repo_cache[repo_url] = gh_get(session, repo_url)
            except requests.HTTPError as e:
                log(f"  dépôt inaccessible {repo_url}: {e}")
                repo_cache[repo_url] = None
        repo = repo_cache[repo_url]
        if repo is None:
            stats["repo_err"] += 1
            continue
        stars = repo.get("stargazers_count", 0)
        if not (MIN_STARS <= stars <= MAX_STARS):
            stats["stars"] += 1
            continue
        pushed = repo.get("pushed_at")
        if not pushed or now - parse_dt(pushed) > MAX_PUSH_AGE:
            stats["push"] += 1
            continue

        owner, name = repo["full_name"].split("/", 1)
        if has_linked_pr(session, owner, name, it["number"]):
            stats["linked_pr"] += 1
            continue

        kept.append({
            "url": url,
            "title": it["title"],
            "repo": repo["full_name"],
            "repo_url": repo["html_url"],
            "stars": stars,
            "comments": it.get("comments", 0),
            "labels": [l["name"] for l in it.get("labels", [])],
            "summary": summarize(it.get("body")),
        })


# --------------------------------------------------------------------------- Notifications

def notify_discord(webhook, issue):
    embed = {
        "title": issue["title"][:256],
        "url": issue["url"],
        "description": issue["summary"][:4000],
        "color": 0x2ECC71,
        "fields": [
            {"name": "Dépôt", "value": f"[{issue['repo']}]({issue['repo_url']})", "inline": True},
            {"name": "⭐ Étoiles", "value": str(issue["stars"]), "inline": True},
            {"name": "💬 Commentaires", "value": str(issue["comments"]), "inline": True},
        ],
    }
    if issue["labels"]:
        embed["fields"].append({"name": "Labels", "value": ", ".join(issue["labels"])[:1024]})
    payload = {"content": "💰 Nouvelle bounty Python", "embeds": [embed]}
    for _ in range(3):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        r.raise_for_status()
        return


def notify_telegram(token, chat_id, issue):
    e = html.escape
    text = (
        f"💰 <b>Nouvelle bounty Python</b>\n\n"
        f"<b>{e(issue['title'])}</b>\n"
        f"📦 <a href=\"{e(issue['repo_url'])}\">{e(issue['repo'])}</a> · ⭐ {issue['stars']}"
        f" · 💬 {issue['comments']}\n"
        f"🔗 {e(issue['url'])}\n\n"
        f"{e(issue['summary'])}"
    )
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def send_notifications(issues):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    """Envoie les notifications ; renvoie les issues effectivement notifiées."""
    if not webhook and not (tg_token and tg_chat):
        log("⚠ Aucun canal configuré (Discord ou Telegram) : pas de notification.")
        return []
    sent = []
    for issue in issues:
        try:
            if webhook:
                notify_discord(webhook, issue)
            if tg_token and tg_chat:
                notify_telegram(tg_token, tg_chat, issue)
            sent.append(issue)
        except requests.RequestException as e:
            # On ne loggue pas l'URL complète (elle contient des secrets).
            log(f"  échec de notification pour {issue['url']}: {type(e).__name__}")
        time.sleep(1)
    return sent


# --------------------------------------------------------------------------- Main

def load_seen():
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(seen_list):
    SEEN_FILE.write_text(json.dumps(seen_list[-SEEN_MAX:], indent=2) + "\n", encoding="utf-8")


def print_report(issues):
    print(f"\n=== {len(issues)} nouvelle(s) issue(s) retenue(s) ===")
    for i in issues:
        print(f"\n• {i['repo']} (⭐ {i['stars']}, 💬 {i['comments']})")
        print(f"  {i['title']}")
        print(f"  {i['url']}")
        print(f"  {i['summary']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="n'envoie aucune notification et n'écrit pas seen_issues.json")
    parser.add_argument("--seed", action="store_true",
                        help="marque tous les résultats actuels comme vus, sans notifier "
                             "(à lancer une fois avant la mise en service)")
    args = parser.parse_args()
    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"

    if hasattr(sys.stdout, "reconfigure"):  # emojis sur consoles Windows
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    seen_list = load_seen()
    session = make_session()
    try:
        issues = find_candidates(session, set(seen_list))
    except RateLimited as e:
        log(f"✖ {e}")
        return 1

    print_report(issues)
    if dry_run:
        log("\n[dry-run] aucune notification envoyée, seen_issues.json inchangé.")
        return 0

    if args.seed:
        save_seen(seen_list + [i["url"] for i in issues])
        log(f"\n[seed] {len(issues)} issue(s) marquée(s) comme vues, aucune notification envoyée.")
        return 0

    if issues:
        batch = issues[:MAX_NOTIFICATIONS]
        if len(issues) > MAX_NOTIFICATIONS:
            log(f"Limite de {MAX_NOTIFICATIONS} notifications : "
                f"{len(issues) - MAX_NOTIFICATIONS} issue(s) reportée(s) au prochain passage.")
        sent = send_notifications(batch)
        if sent:
            save_seen(seen_list + [i["url"] for i in sent])
    return 0


if __name__ == "__main__":
    sys.exit(main())
