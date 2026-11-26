#!/usr/bin/env python3
"""Veille « se faire embaucher » : nouvelles issues chez de jeunes startups YC qui recrutent.

Stratégie : être le premier à répondre aux issues que l'équipe veut voir résolues,
dans des startups jeunes et petites (le fondateur lit chaque PR) qui recrutent.

1. Découverte des cibles (1 fois par semaine, cache dans targets.json) :
   - startups YC « open source » (API publique yc-oss), actives, qui recrutent,
     promotion récente (>= MIN_BATCH_YEAR), équipe <= MAX_TEAM_SIZE ;
   - organisation GitHub retrouvée en comparant son site web à celui de la startup ;
   - dépôts actifs dans tes LANGUAGES, >= 50 étoiles.
   + les dépôts que tu ajoutes à la main dans targets.txt (un « owner/repo » par ligne).
2. À chaque passage : issues ouvertes depuis moins de ISSUE_MAX_AGE_HOURS, non
   assignées, peu commentées, et soit étiquetées (good first issue, help wanted, bug…)
   soit ouvertes par l'équipe elle-même.

Variables d'environnement (en plus de celles de hunter.py) :
  LANGUAGES                     défaut "python"
  MIN_BATCH_YEAR                défaut : année courante - 3
  MAX_TEAM_SIZE                 défaut 60
  REQUIRE_HIRING                défaut 1 (0 = aussi les startups qui ne recrutent pas)
  ISSUE_MAX_AGE_HOURS           défaut 72
  STARTUP_DAILY                 issues envoyées par jour, les meilleures (défaut 2)
  DISCORD_STARTUPS_WEBHOOK_URL  salon dédié (obligatoire pour Discord)

3. Contrôle « prête à démarrer » (seules celles qui passent tout sont envoyées) :
   description claire, personne ne l'a réclamée en commentaire, aucune PR liée,
   et le dépôt fusionne vraiment des PR de contributeurs externes.

Usage :
  python startups.py [--dry-run | --seed] [--refresh]
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

import hunter
from hunter import env_int, gh_get, log

ROOT = Path(__file__).parent
TARGETS_CACHE = ROOT / "targets.json"
TARGETS_MANUAL = ROOT / "targets.txt"
SEEN_FILE = ROOT / "seen_startup_issues.json"
YC_OSS_URL = "https://yc-oss.github.io/api/tags/open-source.json"

NOW = datetime.now(timezone.utc)
LANGUAGES = {l.lower() for l in hunter.LANGUAGES}
MIN_BATCH_YEAR = env_int("MIN_BATCH_YEAR", NOW.year - 3)
MAX_TEAM_SIZE = env_int("MAX_TEAM_SIZE", 60)
REQUIRE_HIRING = (os.environ.get("REQUIRE_HIRING") or "1") == "1"
ISSUE_MAX_AGE = timedelta(hours=env_int("ISSUE_MAX_AGE_HOURS", 72))
REFRESH_EVERY = timedelta(days=7)
MIN_REPO_STARS = 50
MAX_REPOS_PER_ORG = 3
MAX_ISSUE_COMMENTS = 3
STARTUP_DAILY = env_int("STARTUP_DAILY", 2)
MIN_BODY_LEN = 120
MIN_EXTERNAL_MERGES = 2

GOOD_LABELS = re.compile(
    r"good[ -]?first[ -]?issue|help[ -]?wanted|\bbug\b|beginner|easy|first[ -]?timers|"
    r"contributions? welcome|up[ -]for[ -]grabs|community", re.I)
BAD_LABELS = re.compile(r"wontfix|won't fix|duplicate|invalid|question|discussion|stale|blocked", re.I)
TEAM = {"OWNER", "MEMBER", "COLLABORATOR"}
CLAIM_RE = re.compile(
    r"\b(?:i'?d like to|i would like to|can i|could i|may i|let me|i'?ll|i will|i want to|i can)"
    r" (?:work on|take|pick|tackle|handle|fix|try)\b|\bworking on (?:this|it)\b"
    r"|\bassign (?:this|it|me)\b|\bi'?m on it\b|^\s*/(?:attempt|try|assign)\b", re.I | re.M)
TEAM_BUSY_RE = re.compile(
    r"\b(?:already|we'?re|we are|i'?m|i am) (?:working on|fixing|on) (?:this|it)\b"
    r"|\binternal(?:ly)?\b|\bnot (?:accepting|open to) (?:prs?|contributions)", re.I)


# --------------------------------------------------------------------------- Découverte

def domain(url):
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def batch_year(batch):
    m = re.search(r"(\d{4})", batch or "")
    return int(m.group(1)) if m else 0


def yc_companies():
    data = requests.get(YC_OSS_URL, timeout=60).json()
    out = []
    for c in data:
        if (c.get("status") or "").lower() != "active":
            continue
        if REQUIRE_HIRING and not c.get("isHiring"):
            continue
        if batch_year(c.get("batch")) < MIN_BATCH_YEAR:
            continue
        team = c.get("team_size")
        if team and team > MAX_TEAM_SIZE:
            continue
        if not domain(c.get("website")):
            continue
        out.append(c)
    return out


def find_org(session, company):
    """Organisation GitHub dont le site web correspond à celui de la startup."""
    want = domain(company["website"])
    terms = {company.get("slug", ""), company["name"], want.split(".")[0]}
    checked = set()
    for term in [t for t in terms if t]:
        res = gh_get(session, "/search/users", {"q": f"{term} type:org", "per_page": 3})
        for u in res.get("items", []):
            login = u["login"]
            if login in checked:
                continue
            checked.add(login)
            org = gh_get(session, f"/users/{login}")
            if domain(org.get("blog")) == want:
                return login
    return None


def contributor_count(session, repo):
    r = session.get(f"{hunter.API}/repos/{repo}/contributors",
                    params={"per_page": 1, "anon": "false"}, timeout=30)
    if r.status_code != 200:
        return None
    m = re.search(r'page=(\d+)>; rel="last"', r.headers.get("Link", ""))
    return int(m.group(1)) if m else len(r.json())


def org_repos(session, org):
    repos = gh_get(session, f"/orgs/{org}/repos", {"per_page": 100, "sort": "pushed"})
    good = [
        r for r in repos
        if not r["fork"] and not r["archived"] and r["stargazers_count"] >= MIN_REPO_STARS
        and (r.get("language") or "").lower() in LANGUAGES
        and NOW - hunter.parse_dt(r["pushed_at"]) < timedelta(days=30)
    ]
    good.sort(key=lambda r: r["stargazers_count"], reverse=True)
    return good[:MAX_REPOS_PER_ORG]


def discover_targets(session):
    companies = yc_companies()
    log(f"YC open source : {len(companies)} startup(s) actives"
        f"{' qui recrutent' if REQUIRE_HIRING else ''}, promo >= {MIN_BATCH_YEAR}, "
        f"équipe <= {MAX_TEAM_SIZE}.")
    targets = []
    for c in companies:
        try:
            org = find_org(session, c)
            if not org:
                log(f"  {c['name']} : organisation GitHub introuvable")
                continue
            for r in org_repos(session, org):
                targets.append({
                    "repo": r["full_name"],
                    "company": c["name"],
                    "batch": c.get("batch"),
                    "team_size": c.get("team_size"),
                    "hiring": bool(c.get("isHiring")),
                    "yc_url": c.get("url"),
                    "stars": r["stargazers_count"],
                    "language": r.get("language"),
                    "contributors": contributor_count(session, r["full_name"]),
                })
                log(f"  ✓ {c['name']} ({c.get('batch')}) → {r['full_name']} ⭐ {r['stargazers_count']}")
        except requests.RequestException as e:
            log(f"  {c['name']} : erreur réseau ({type(e).__name__}), ignorée")
    return targets


def manual_targets():
    if not TARGETS_MANUAL.exists():
        return []
    out = []
    for line in TARGETS_MANUAL.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if re.fullmatch(r"[\w.-]+/[\w.-]+", line):
            out.append({"repo": line, "company": line.split("/")[0], "batch": None,
                        "team_size": None, "hiring": None, "yc_url": None,
                        "stars": None, "language": None, "contributors": None, "manual": True})
    return out


def load_targets(session, refresh=False):
    cache = {}
    if TARGETS_CACHE.exists():
        try:
            cache = json.loads(TARGETS_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    stale = not cache.get("updated") or NOW - hunter.parse_dt(cache["updated"]) > REFRESH_EVERY
    if refresh or stale:
        log("Mise à jour de la liste des startups cibles…")
        try:
            targets = discover_targets(session)
            cache = {"updated": NOW.isoformat().replace("+00:00", "Z"), "targets": targets}
            TARGETS_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
        except (requests.RequestException, hunter.RateLimited, ValueError) as e:
            log(f"⚠ découverte impossible ({type(e).__name__}) : on garde l'ancienne liste.")
    targets = {t["repo"].lower(): t for t in cache.get("targets", [])}
    for t in manual_targets():
        targets.setdefault(t["repo"].lower(), t)
    return list(targets.values())


# --------------------------------------------------------------------------- Issues

def new_issues(session, target, seen):
    since = (NOW - ISSUE_MAX_AGE).isoformat().replace("+00:00", "Z")
    items = gh_get(session, f"/repos/{target['repo']}/issues",
                   {"state": "open", "sort": "created", "direction": "desc",
                    "since": since, "per_page": 50})
    found = []
    for it in items:
        if "pull_request" in it or it["html_url"] in seen:
            continue
        if NOW - hunter.parse_dt(it["created_at"]) > ISSUE_MAX_AGE:
            continue
        if it.get("assignees") or it.get("comments", 0) > MAX_ISSUE_COMMENTS:
            continue
        labels = [l["name"] for l in it.get("labels", [])]
        if any(BAD_LABELS.search(l) for l in labels):
            continue
        good_label = next((l for l in labels if GOOD_LABELS.search(l)), None)
        by_team = it.get("author_association") in TEAM
        if not (good_label or by_team):
            continue
        found.append({
            "url": it["html_url"],
            "title": it["title"],
            "number": it["number"],
            "labels": labels,
            "good_label": good_label,
            "by_team": by_team,
            "comments": it.get("comments", 0),
            "created_at": it["created_at"],
            "summary": hunter.summarize(it.get("body")),
            "body": it.get("body") or "",
            **{k: target.get(k) for k in ("repo", "company", "batch", "team_size", "hiring",
                                          "yc_url", "stars", "contributors")},
        })
    return found


def external_merges(session, repo, cache):
    """PR de contributeurs externes fusionnées parmi les 30 dernières PR fermées."""
    if repo not in cache:
        prs = gh_get(session, f"/repos/{repo}/pulls",
                     {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 30})
        cache[repo] = sum(1 for p in prs if p.get("merged_at")
                          and p.get("author_association") not in TEAM
                          and (p.get("user") or {}).get("type") != "Bot")
    return cache[repo]


def ready_check(session, i, cache):
    """None si l'issue est prête à démarrer, sinon la raison du refus."""
    if len((i["body"] or "").strip()) < MIN_BODY_LEN:
        return "description trop vague"
    if i["comments"]:
        comments = gh_get(session, f"/repos/{i['repo']}/issues/{i['number']}/comments", {"per_page": 30})
        for c in comments:
            body = c.get("body") or ""
            if c.get("author_association") in TEAM:
                if TEAM_BUSY_RE.search(body):
                    return "l'équipe s'en occupe"
            elif (c.get("user") or {}).get("type") != "Bot" and CLAIM_RE.search(body):
                return "déjà réclamée par quelqu'un"
    owner, name = i["repo"].split("/", 1)
    if hunter.has_linked_pr(session, owner, name, i["number"]):
        return "PR déjà liée"
    i["ext_merges"] = external_merges(session, i["repo"], cache)
    if i["ext_merges"] < MIN_EXTERNAL_MERGES:
        return "dépôt qui fusionne peu de PR externes"
    if not i["good_label"] and i["ext_merges"] < 2 * MIN_EXTERNAL_MERGES:
        return "pas de label d'appel à contribution"
    return None


def score(i):
    s = min(i.get("ext_merges", 0), 15)
    s += 15 if i["good_label"] and re.search(r"first|beginner|easy", i["good_label"], re.I) else 0
    s += 10 if i["good_label"] else 0
    s += 10 if i["by_team"] else 0
    s += 10 if i["hiring"] else 0
    s += 8 if i["comments"] == 0 else 0
    c = i["contributors"]
    s += 10 if c is not None and c <= 20 else 5 if c is not None and c <= 60 else 0
    s += max(0, 10 - (NOW - hunter.parse_dt(i["created_at"])).total_seconds() / 3600 / 7)
    return round(s, 1)


# --------------------------------------------------------------------------- Notifications

def who(i):
    parts = [i["company"]]
    if i["batch"]:
        parts.append(f"YC {i['batch']}")
    return " · ".join(parts)


def discord_payload(i):
    fields = [
        {"name": "🏢 Startup", "value": who(i) + (f"\n[Fiche YC]({i['yc_url']})" if i["yc_url"] else ""),
         "inline": True},
        {"name": "🧑‍💼 Recrute", "value": "Oui" if i["hiring"] else "Inconnu", "inline": True},
        {"name": "👥 Équipe", "value": str(i["team_size"] or "?"), "inline": True},
        {"name": "📦 Dépôt", "value": f"[{i['repo']}](https://github.com/{i['repo']})"
                                      + (f" · ⭐ {i['stars']}" if i["stars"] else ""), "inline": True},
        {"name": "🤝 Contributeurs", "value": str(i["contributors"] or "?"), "inline": True},
        {"name": "💬 Commentaires", "value": str(i["comments"]), "inline": True},
    ]
    why = []
    if i["good_label"]:
        why.append(f"label « {i['good_label']} »")
    if i["by_team"]:
        why.append("ouverte par l'équipe")
    fields.append({"name": "🎯 Pourquoi", "value": ", ".join(why), "inline": False})
    fields.append({"name": "✅ Déjà vérifié pour toi", "value": "\n".join([
        "• Description claire, issue de moins de 3 jours",
        "• Personne ne l'a réclamée, aucune PR liée, non assignée",
        f"• L'équipe fusionne les PR externes ({i.get('ext_merges', 0)} sur ses 30 dernières PR)",
        "• Startup jeune et petite" + (" qui recrute" if i["hiring"] else ""),
    ]), "inline": False})
    fields.append({"name": "👉 Démarrer maintenant", "value":
                   "1. Commente en anglais : « Hi! I'd like to work on this. My plan: … » (2-3 lignes).\n"
                   "2. Attends un 👍 ou une réponse de l'équipe (souvent moins de 24 h), puis code.\n"
                   f"3. PR petite et testée, avec « Fixes #{i['number']} » dans la description.",
                   "inline": False})
    return {
        "content": f"🎯 **Nouvelle issue chez {who(i)}**" + (" · 🧑‍💼 recrute" if i["hiring"] else ""),
        "embeds": [{"title": i["title"][:256], "url": i["url"], "description": i["summary"][:4000],
                    "color": 0x9B59B6, "fields": fields}],
    }


def telegram_text(i):
    e = html.escape
    return (f"🎯 <b>Nouvelle issue chez {e(who(i))}</b>{' · 🧑‍💼 recrute' if i['hiring'] else ''}\n\n"
            f"<b>{e(i['title'])}</b>\n📦 {e(i['repo'])} · 🤝 {i['contributors'] or '?'} contributeurs"
            f" · 💬 {i['comments']}\n🔗 {e(i['url'])}\n\n{e(i['summary'])}\n\n"
            f"👉 Commente ton plan avant de coder.")


# --------------------------------------------------------------------------- Main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="force la mise à jour de targets.json")
    args = parser.parse_args()
    hunter.setup_console()
    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"

    session = hunter.make_session()
    targets = load_targets(session, refresh=args.refresh or os.environ.get("TARGET_REFRESH") == "1")
    log(f"{len(targets)} dépôt(s) cible(s) surveillé(s).")

    seen_list = hunter.load_seen(SEEN_FILE)
    seen = set(seen_list)
    issues = []
    for t in targets:
        try:
            issues += new_issues(session, t, seen)
        except hunter.RateLimited as e:
            log(f"⚠ {e} : arrêt anticipé.")
            break
        except requests.RequestException as e:
            log(f"  {t['repo']} : ignoré ({type(e).__name__})")
    log(f"{len(issues)} issue(s) candidate(s) : contrôle « prête à démarrer »…")
    ready, cache, refused = [], {}, {}
    for i in issues:
        try:
            reason = ready_check(session, i, cache)
        except hunter.RateLimited as e:
            log(f"⚠ {e} : arrêt anticipé.")
            break
        except requests.RequestException as e:
            reason = f"erreur réseau ({type(e).__name__})"
        if reason:
            refused[reason] = refused.get(reason, 0) + 1
        else:
            ready.append(i)
    if refused:
        log("Écartées : " + ", ".join(f"{k}={v}" for k, v in refused.items()))
    issues = ready
    for i in issues:
        i["score"] = score(i)
    issues.sort(key=lambda i: i["score"], reverse=True)

    print(f"\n=== {len(issues)} issue(s) prête(s) à démarrer ===")
    for i in issues:
        print(f"\n• [{i['score']}] {who(i)} : {i['repo']} (🤝 {i['contributors'] or '?'})")
        print(f"  {i['title']}\n  {i['url']}")

    if dry_run:
        log("\n[dry-run] aucune notification envoyée, seen_startup_issues.json inchangé.")
        return 0
    if args.seed:
        hunter.save_seen(seen_list + [i["url"] for i in issues], SEEN_FILE)
        log(f"\n[seed] {len(issues)} issue(s) marquée(s) comme vues.")
        return 0
    left = hunter.quota_left("startups", STARTUP_DAILY)
    if issues and not left:
        log(f"Quota du jour atteint ({STARTUP_DAILY}) : les suivantes attendront demain.")
    if issues and left:
        sent = hunter.send_notifications(issues[:left], discord_payload, telegram_text,
                                         "DISCORD_STARTUPS_WEBHOOK_URL")
        hunter.quota_use("startups", len(sent))
        if sent:
            hunter.save_seen(seen_list + [i["url"] for i in sent], SEEN_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
