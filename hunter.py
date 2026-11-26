#!/usr/bin/env python3
"""Veille de GitHub Bounties RÉELLEMENT financées et peu disputées.

Une issue n'est retenue que si :
  - un bot de plateforme (Algora ou Opire) confirme un montant >= MIN_AMOUNT ;
  - personne ne l'a réclamée (/attempt, /try, PR liée, assignation) ;
  - < 8 commentaires, dépôt de 50 à 3000 étoiles, push de moins de 14 jours.
Chaque issue reçoit un score (montant, historique de paiement du financeur,
fraîcheur) et les meilleures sont notifiées en premier.

Variables d'environnement :
  GITHUB_TOKEN             token GitHub (fortement recommandé)
  DISCORD_WEBHOOK_URL      webhook Discord (optionnel)
  TELEGRAM_BOT_TOKEN       + TELEGRAM_CHAT_ID : bot Telegram (optionnel)
  LANGUAGES                langages, séparés par des virgules (défaut : python)
  MIN_AMOUNT               montant minimum en $ (défaut : 50)
  MAX_ATTEMPTS             concurrents tolérés (défaut : 0)
  REQUIRE_PAYER_HISTORY=1  n'accepter que les financeurs ayant déjà payé
  EXCLUDED_REPOS           "owner/repo,owner2/repo2" à ignorer
  MIN_STARS / MAX_STARS    étoiles du dépôt (défaut : 50 / 3000)
  MAX_COMMENTS             commentaires max, exclus (défaut : 8)
  MAX_PUSH_DAYS            dernier push max en jours (défaut : 14)
  DRY_RUN=1                équivalent à --dry-run

Usage :
  python hunter.py              # recherche, notifie, met à jour seen_issues.json
  python hunter.py --dry-run    # recherche et affiche, sans notifier ni écrire
  python hunter.py --seed       # marque les résultats actuels comme vus, sans notifier
"""

import argparse
import html
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.github.com"
SEEN_FILE = Path(__file__).with_name("seen_issues.json")
SEEN_MAX = 5000


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


LANGUAGES = [l.strip() for l in (os.environ.get("LANGUAGES") or "python").split(",") if l.strip()]
MIN_AMOUNT = env_int("MIN_AMOUNT", 50)
MAX_ATTEMPTS = env_int("MAX_ATTEMPTS", 0)
REQUIRE_PAYER_HISTORY = os.environ.get("REQUIRE_PAYER_HISTORY") == "1"

QUERY_TEMPLATES = [
    'is:issue is:open language:{lang} label:"💎 Bounty"',
    "is:issue is:open language:{lang} commenter:app/algora-pbc",
    "is:issue is:open language:{lang} commenter:app/opirebot",
    "is:issue is:open language:{lang} label:bounty",
]
SEARCH_PAGES = 2

MAX_COMMENTS = env_int("MAX_COMMENTS", 8)
MIN_STARS, MAX_STARS = env_int("MIN_STARS", 50), env_int("MAX_STARS", 3000)
MAX_PUSH_AGE = timedelta(days=env_int("MAX_PUSH_DAYS", 14))
SUMMARY_LEN = 280
MAX_NOTIFICATIONS = 10

EXCLUDED_REPOS = {
    "ikalus1988/misakanet",
    "phankhoa95/oss-bot-trap-benchmark",
}
EXCLUDED_REPOS |= {
    r.strip().lower() for r in os.environ.get("EXCLUDED_REPOS", "").split(",") if r.strip()
}

NO_MONEY_RE = re.compile(
    r"\$\s?0(?![.,\d])|\b0\s?(?:usd|\$)(?![.,\d])"
    r"|pays? no money|zero bounty|no monetary|unpaid bounty|credit only",
    re.I,
)
# Dépôts "miroirs" qui refusent les humains (pièges à bots).
BOT_TRAP_RE = re.compile(
    r"pull requests from human engineers are not reviewed|ai benchmark arena", re.I)

# --- Signaux des plateformes (vérifiés sur de vraies issues) ---
ALGORA_BOT = "algora-pbc[bot]"
OPIRE_BOT = "opirebot[bot]"
AMOUNT = r"\$\s?([\d,]+(?:\.\d+)?)"
ALGORA_FUNDED_RE = re.compile(r"💎\s*\**" + AMOUNT + r"\**\s*bounty", re.I)
ALGORA_COMPANY_RE = re.compile(r"\[•\s*([^\]]+?)\s*\]\(https://algora\.io/")
OPIRE_FUNDED_RE = re.compile(r"created an? " + AMOUNT + r" reward", re.I)
# Quelqu'un a déjà soumis une solution qui réclame la prime.
CLAIMED_RE = re.compile(
    r"submitted a \[?pull request\]?.{0,80}claims the bounty|has claimed (?:all )?rewards?", re.I)
ATTEMPT_RE = re.compile(r"^\s*/(?:attempt|try)\b", re.I | re.M)


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
    for lang in LANGUAGES:
        for tpl in QUERY_TEMPLATES:
            q = tpl.format(lang=lang)
            for page in range(1, SEARCH_PAGES + 1):
                data = gh_get(session, "/search/issues", {
                    "q": q, "sort": "updated", "order": "desc", "per_page": 100, "page": page,
                })
                items = data.get("items", [])
                for it in items:
                    if "pull_request" not in it:
                        found.setdefault(it["html_url"], it)
                log(f"  « {q} » page {page} : {len(items)} résultats")
                if len(items) < 100:
                    break
    return list(found.values())


def search_count(session, q):
    return gh_get(session, "/search/issues", {"q": q, "per_page": 1}).get("total_count", 0)


def parse_dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def to_amount(text):
    return float(text.replace(",", ""))


def analyze_comments(session, issue):
    """Lit les commentaires : financement confirmé, concurrents, réclamation."""
    comments = gh_get(session, issue["comments_url"], {"per_page": 100})
    info = {"amount": 0.0, "platform": None, "company": None,
            "attempters": set(), "claimed": False}
    opire_total = 0.0
    for c in comments:
        login = c["user"]["login"]
        body = c.get("body") or ""
        if login == ALGORA_BOT:
            m = ALGORA_FUNDED_RE.search(body)
            if m:
                info["amount"] = max(info["amount"], to_amount(m.group(1)))
                info["platform"] = "Algora"
                cm = ALGORA_COMPANY_RE.search(body)
                if cm:
                    info["company"] = cm.group(1)
        elif login == OPIRE_BOT:
            m = OPIRE_FUNDED_RE.search(body)
            if m:
                opire_total += to_amount(m.group(1))
                info["platform"] = info["platform"] or "Opire"
        if c["user"].get("type") == "Bot":
            if CLAIMED_RE.search(body):
                info["claimed"] = True
        elif ATTEMPT_RE.search(body):
            info["attempters"].add(login)
    if opire_total and info["platform"] == "Opire":
        info["amount"] = opire_total
    return info


def has_linked_pr(session, owner, repo, number):
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


def payer_history(session, owner, platform):
    """Nombre de primes déjà payées par ce propriétaire (tous dépôts confondus)."""
    if platform == "Algora":
        q = f'user:{owner} is:issue label:"💰 Rewarded"'
    else:  # Opire : issues fermées où le bot a annoncé une réclamation
        q = f'user:{owner} is:issue is:closed commenter:app/opirebot "claimed"'
    try:
        return search_count(session, q)
    except requests.HTTPError:
        return 0


def summarize(body):
    text = body or ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#>*_`|~-]{1,}", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "(pas de description)"
    return text if len(text) <= SUMMARY_LEN else text[:SUMMARY_LEN].rsplit(" ", 1)[0] + "…"


def score(c, now):
    """Plus c'est haut, mieux c'est : argent, financeur fiable, peu disputé, récent."""
    age_days = (now - parse_dt(c["created_at"])).days
    s = 20 * math.log10(max(c["amount"], 1))            # $100 -> 40, $1000 -> 60
    s += min(c["paid_before"], 10) * 3                    # historique de paiement
    s += 10 if c["attempts"] == 0 else 0                  # aucun concurrent
    s += 8 if c["comments"] <= 2 else 0                   # peu d'activité
    s += max(0, 10 - age_days / 3)                        # fraîcheur (30 jours)
    s += 5 if c["hiring"] else 0                          # startup qui recrute
    return round(s, 1)


def find_candidates(session, seen, hiring=frozenset()):
    issues = search_issues(session)
    log(f"{len(issues)} issues uniques trouvées.")
    now = datetime.now(timezone.utc)
    ctx = {"repos": {}, "payers": {}}
    stats = dict.fromkeys(["seen", "comments", "assigned", "excluded_repo", "no_money", "bot_trap",
                           "stars", "push", "not_funded", "too_small", "claimed", "competition",
                           "linked_pr", "no_history", "repo_err"], 0)
    kept = []
    try:
        _filter_issues(session, issues, seen, now, ctx, stats, kept, hiring)
    except RateLimited as e:
        log(f"⚠ {e} : arrêt anticipé, {len(kept)} issue(s) déjà validée(s) conservée(s).")
    log("Écartées : " + ", ".join(f"{k}={v}" for k, v in stats.items() if v))
    for c in kept:
        c["score"] = score(c, now)
    kept.sort(key=lambda c: c["score"], reverse=True)
    return kept


def _filter_issues(session, issues, seen, now, ctx, stats, kept, hiring):
    for it in issues:
        # 1. Filtres gratuits (données déjà présentes dans la recherche)
        if it["html_url"] in seen:
            stats["seen"] += 1; continue
        if it.get("comments", 0) >= MAX_COMMENTS:
            stats["comments"] += 1; continue
        if it.get("assignees"):
            stats["assigned"] += 1; continue
        repo_full = it["repository_url"].split("/repos/", 1)[1]
        if repo_full.lower() in EXCLUDED_REPOS:
            stats["excluded_repo"] += 1; continue
        text = f"{it.get('title', '')}\n{it.get('body') or ''}"
        if NO_MONEY_RE.search(text):
            stats["no_money"] += 1; continue
        if BOT_TRAP_RE.search(text):
            stats["bot_trap"] += 1; continue

        # 2. Dépôt (1 appel, mis en cache)
        repo_url = it["repository_url"]
        if repo_url not in ctx["repos"]:
            try:
                ctx["repos"][repo_url] = gh_get(session, repo_url)
            except requests.HTTPError:
                ctx["repos"][repo_url] = None
        repo = ctx["repos"][repo_url]
        if repo is None:
            stats["repo_err"] += 1; continue
        stars = repo.get("stargazers_count", 0)
        if not (MIN_STARS <= stars <= MAX_STARS):
            stats["stars"] += 1; continue
        pushed = repo.get("pushed_at")
        if not pushed or now - parse_dt(pushed) > MAX_PUSH_AGE:
            stats["push"] += 1; continue

        # 3. Commentaires : financement réel + concurrence (1 appel)
        info = analyze_comments(session, it)
        if not info["platform"]:
            stats["not_funded"] += 1; continue
        if info["amount"] < MIN_AMOUNT:
            stats["too_small"] += 1; continue
        if info["claimed"]:
            stats["claimed"] += 1; continue
        if len(info["attempters"]) > MAX_ATTEMPTS:
            stats["competition"] += 1; continue

        # 4. PR liée (1 appel)
        owner, name = repo["full_name"].split("/", 1)
        if has_linked_pr(session, owner, name, it["number"]):
            stats["linked_pr"] += 1; continue

        # 5. Historique de paiement du financeur (1 recherche, en cache)
        key = (owner.lower(), info["platform"])
        if key not in ctx["payers"]:
            ctx["payers"][key] = payer_history(session, owner, info["platform"])
        paid_before = ctx["payers"][key]
        if REQUIRE_PAYER_HISTORY and paid_before == 0:
            stats["no_history"] += 1; continue

        company = info["company"] or repo.get("owner", {}).get("login", owner)
        company_key = re.sub(r"\s*\(.*?\)", "", company).strip().lower()
        kept.append({
            "url": it["html_url"],
            "title": it["title"],
            "repo": repo["full_name"],
            "repo_url": repo["html_url"],
            "stars": stars,
            "comments": it.get("comments", 0),
            "created_at": it["created_at"],
            "amount": info["amount"],
            "platform": info["platform"],
            "company": info["company"],
            "attempts": len(info["attempters"]),
            "paid_before": paid_before,
            "hiring": company_key in hiring or owner.lower() in hiring,
            "summary": summarize(it.get("body")),
        })


# --------------------------------------------------------------------------- Notifications

def discord_post(webhook, payload):
    for _ in range(3):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        r.raise_for_status()
        return


def telegram_post(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    r.raise_for_status()


def fmt_amount(a):
    return f"${a:,.0f}" if a == int(a) else f"${a:,.2f}"


def trust_label(c):
    if c["paid_before"] > 0:
        return f"✅ A déjà payé {c['paid_before']} prime(s)"
    return "🆕 Aucun paiement passé trouvé : prudence"


def competition_label(c):
    return "🟢 Aucun concurrent" if c["attempts"] == 0 else f"🟠 {c['attempts']} concurrent(s)"


def discord_payload(c):
    color = 0x2ECC71 if c["paid_before"] > 0 else 0xF1C40F
    fields = [
        {"name": "💵 Montant", "value": f"**{fmt_amount(c['amount'])}** via {c['platform']}", "inline": True},
        {"name": "🧾 Financeur", "value": trust_label(c), "inline": True},
        {"name": "🏁 Concurrence", "value": competition_label(c), "inline": True},
        {"name": "📦 Dépôt", "value": f"[{c['repo']}]({c['repo_url']}) · ⭐ {c['stars']}", "inline": True},
        {"name": "💬 Commentaires", "value": str(c["comments"]), "inline": True},
        {"name": "📊 Score", "value": str(c["score"]), "inline": True},
    ]
    if c["company"]:
        fields.append({"name": "🚀 Startup", "value": c["company"], "inline": True})
    if c["hiring"]:
        fields.append({"name": "🧑‍💼 Recrute", "value": "Cette entreprise recrute (HN Who is hiring) : "
                       "une bounty réussie est une excellente candidature.", "inline": False})
    return {
        "content": f"💰 **{fmt_amount(c['amount'])}** : nouvelle bounty",
        "embeds": [{"title": c["title"][:256], "url": c["url"],
                    "description": c["summary"][:4000], "color": color, "fields": fields}],
    }


def telegram_text(c):
    e = html.escape
    lines = [
        f"💰 <b>{fmt_amount(c['amount'])}</b> via {c['platform']} : nouvelle bounty\n",
        f"<b>{e(c['title'])}</b>",
        f"📦 <a href=\"{e(c['repo_url'])}\">{e(c['repo'])}</a> · ⭐ {c['stars']} · 💬 {c['comments']}",
        f"{trust_label(c)}\n{competition_label(c)}",
    ]
    if c["company"]:
        lines.append(f"🚀 {e(c['company'])}" + (" · 🧑‍💼 recrute" if c["hiring"] else ""))
    lines += [f"🔗 {e(c['url'])}", "", e(c["summary"])]
    return "\n".join(lines)


def channels():
    return (os.environ.get("DISCORD_WEBHOOK_URL", "").strip(),
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def send_notifications(items, to_discord=discord_payload, to_telegram=telegram_text, webhook=None):
    """Envoie les notifications ; renvoie les éléments effectivement notifiés."""
    default_hook, tg_token, tg_chat = channels()
    webhook = webhook or default_hook
    if not webhook and not (tg_token and tg_chat):
        log("⚠ Aucun canal configuré (Discord ou Telegram) : pas de notification.")
        return []
    sent = []
    for item in items:
        try:
            if webhook:
                discord_post(webhook, to_discord(item))
            if tg_token and tg_chat:
                telegram_post(tg_token, tg_chat, to_telegram(item))
            sent.append(item)
        except requests.RequestException as e:
            log(f"  échec de notification pour {item['url']}: {type(e).__name__}")
        time.sleep(1)
    return sent


# --------------------------------------------------------------------------- Main

def load_seen(path=SEEN_FILE):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(seen_list, path=SEEN_FILE):
    path.write_text(json.dumps(seen_list[-SEEN_MAX:], indent=2) + "\n", encoding="utf-8")


def print_report(items):
    print(f"\n=== {len(items)} bounty(s) retenue(s) ===")
    for c in items:
        print(f"\n• [{c['score']}] {fmt_amount(c['amount'])} via {c['platform']} : {c['repo']}"
              f" (⭐ {c['stars']}, 💬 {c['comments']})")
        print(f"  {c['title']}")
        print(f"  {c['url']}")
        extra = f" · 🚀 {c['company']}" if c["company"] else ""
        extra += " · 🧑‍💼 recrute" if c["hiring"] else ""
        print(f"  {trust_label(c)} · {competition_label(c)}{extra}")


def setup_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="n'envoie aucune notification et n'écrit pas seen_issues.json")
    parser.add_argument("--seed", action="store_true",
                        help="marque tous les résultats actuels comme vus, sans notifier")
    args = parser.parse_args()
    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"
    setup_console()

    try:
        from jobs import hiring_companies
        hiring = hiring_companies()
    except Exception as e:  # le signal « recrute » est un bonus, jamais bloquant
        log(f"  (liste des entreprises qui recrutent indisponible : {type(e).__name__})")
        hiring = frozenset()

    seen_list = load_seen()
    session = make_session()
    items = find_candidates(session, set(seen_list), hiring)
    print_report(items)

    if dry_run:
        log("\n[dry-run] aucune notification envoyée, seen_issues.json inchangé.")
        return 0
    if args.seed:
        save_seen(seen_list + [c["url"] for c in items])
        log(f"\n[seed] {len(items)} issue(s) marquée(s) comme vues, aucune notification envoyée.")
        return 0
    if items:
        batch = items[:MAX_NOTIFICATIONS]
        if len(items) > MAX_NOTIFICATIONS:
            log(f"Limite de {MAX_NOTIFICATIONS} notifications : "
                f"{len(items) - MAX_NOTIFICATIONS} reportée(s) au prochain passage.")
        sent = send_notifications(batch)
        if sent:
            save_seen(seen_list + [c["url"] for c in sent])
    return 0


if __name__ == "__main__":
    sys.exit(main())
