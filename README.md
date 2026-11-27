# Bounty Hunter

Veille automatique (GitHub Actions) qui t'envoie sur Discord **uniquement les opportunités déjà vérifiées** :
des primes vraiment payées, des offres d'emploi ouvertes à l'international et des issues de jeunes
startups qui recrutent. Chaque type d'annonce a son propre salon, et seules les meilleures du jour sont
envoyées.

## Les 4 salons

| Salon | Script | Ce que tu reçois | Maximum |
|---|---|---|---|
| `#bounties` | `hunter.py` | Primes Algora/Opire : argent confirmé par le bot, 0 concurrent, aucune PR liée, financeur qui a déjà payé | 3 / jour |
| `#jobs` | `jobs.py` | Offres « Who is hiring » de Hacker News : télétravail ouvert à l'international, Python, pas de poste de direction | 3 / jour |
| `#startup` | `startups.py` | Nouvelles issues chez de jeunes startups YC qui recrutent, prêtes à démarrer | 2 / jour |
| `#statut` | `status.py` | Alerte si un flux plante, message quand il se rétablit, bilan chaque matin vers 8 h (heure de Tunis) | — |

Chaque annonce contient **« ✅ Déjà vérifié pour toi »** (ce que le système a contrôlé) et
**« 👉 Démarrer maintenant »** (les étapes exactes).

### Contrôles d'une issue startup avant envoi
- startup YC active, promotion récente, petite équipe, qui recrute ;
- issue de moins de 72 h, non assignée, 3 commentaires au plus, label d'appel à contribution ou ouverte par l'équipe ;
- description claire ; personne n'a écrit « can I work on this » ou `/attempt` ; l'équipe ne dit pas qu'elle s'en occupe ;
- aucune PR liée ; le dépôt fusionne vraiment des PR de contributeurs externes.

## Fréquence

- **Dépôt privé** (par défaut) : un passage complet toutes les heures. Cela consomme environ 1 440 des
  2 000 minutes gratuites par mois.
- **Dépôt public** + variable `FAST_SCHEDULE=1` : en plus, les issues startup sont vérifiées toutes les
  15 minutes. Les minutes sont illimitées pour un dépôt public.

GitHub peut retarder un passage planifié, et sur certains comptes récents il ne le lance pas du tout.
**Déclencheur de secours** : un service externe gratuit (cron-job.org) lance le workflow toutes les
15 minutes avec `mode=auto` (passage complet dans le premier quart d'heure, issues startup seulement sinon) :

- URL : `POST https://api.github.com/repos/atef-hadef/bounty-hunter/actions/workflows/hunt.yml/dispatches`
- En-têtes : `Accept: application/vnd.github+json` et `Authorization: Bearer <token>`
- Corps : `{"ref":"main","inputs":{"mode":"auto"}}`
- Token : *fine-grained*, limité au dépôt `bounty-hunter`, permission **Actions : Read and write** uniquement.
  Il n'est stocké que chez cron-job.org. À renouveler avant son expiration.

Si les deux déclencheurs tournent à la même heure, ce n'est pas grave : les passages s'enchaînent
et une annonce n'est jamais envoyée deux fois.

## Configuration (Settings → Secrets and variables → Actions)

### Secrets
| Nom | Rôle |
|---|---|
| `DISCORD_WEBHOOK_URL` | webhook du salon `#bounties` |
| `DISCORD_JOBS_WEBHOOK_URL` | webhook du salon `#jobs` |
| `DISCORD_STARTUPS_WEBHOOK_URL` | webhook du salon `#startup` |
| `DISCORD_STATUS_WEBHOOK_URL` | webhook du salon `#statut` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | optionnel : tout recevoir aussi sur Telegram |

Ne colle jamais un webhook ou un token ailleurs que dans ces secrets.

### Variables (toutes optionnelles)
| Nom | Défaut | Effet |
|---|---|---|
| `LANGUAGES` | `python` | langages surveillés, séparés par des virgules |
| `FAST_SCHEDULE` | — | `1` = issues startup toutes les 15 min (**dépôt public uniquement**) |
| `BOUNTY_DAILY` / `JOBS_DAILY` / `STARTUP_DAILY` | 3 / 3 / 2 | annonces envoyées par jour et par salon |
| `MIN_AMOUNT` | 50 | prime minimale en $ |
| `MAX_ATTEMPTS` | 0 | concurrents tolérés sur une prime |
| `REQUIRE_PAYER_HISTORY` | 1 | `0` = accepter aussi les financeurs sans historique de paiement |
| `EXCLUDED_REPOS` | — | dépôts à ignorer, `owner/repo,owner/repo` |
| `MIN_STARS` / `MAX_STARS` | 50 / 3000 | taille des dépôts pour les primes |
| `MAX_COMMENTS` / `MAX_PUSH_DAYS` | 8 / 14 | activité des primes |
| `JOB_KEYWORDS` / `JOB_REQUIRE` | `python,django,fastapi,flask` / `remote` | mots-clés des offres |
| `JOB_OPEN_ONLY` | 1 | `0` = accepter aussi les offres sans zone précisée |
| `MIN_BATCH_YEAR` / `MAX_TEAM_SIZE` / `REQUIRE_HIRING` | année − 3 / 60 / 1 | choix des startups |
| `ISSUE_MAX_AGE_HOURS` | 72 | âge maximal d'une issue startup |
| `STATUS_HOUR_UTC` | 7 | heure du bilan quotidien (UTC) |

Une variable absente reprend la valeur par défaut. Pour revenir au défaut, supprime la variable.

## Ajouter une startup à la main
Écris `owner/repo` sur une ligne de `targets.txt`. La liste des startups YC se met à jour seule chaque semaine.

## Tester
- **Sur GitHub** : Actions → Bounty Hunt → Run workflow → coche `dry_run`. Tout s'affiche dans le journal, rien n'est envoyé.
- **En local** :
  ```powershell
  pip install -r requirements.txt
  python -m unittest discover -s tests -v
  python jobs.py --dry-run
  ```
  `hunter.py` et `startups.py` ont besoin d'un `GITHUB_TOKEN` dans ton terminal (jamais dans un fichier).

Options de chaque script : `--dry-run` (afficher sans envoyer), `--seed` (tout marquer comme vu sans envoyer).
`startups.py --refresh` force la mise à jour de la liste des startups.

## Fichiers d'état (mis à jour par le robot, ne pas modifier)
`seen_issues.json`, `seen_jobs.json`, `seen_startup_issues.json` (annonces déjà envoyées),
`daily_quota.json` (compteurs du jour), `targets.json` (startups surveillées), `status_state.json` (alertes et bilans).

## Dépannage
| Symptôme | Cause probable |
|---|---|
| Pas de bilan dans `#statut` un matin | Le système est arrêté : regarde l'onglet Actions (quota de minutes épuisé, workflow désactivé). |
| `⚠ Secret … absent` dans le journal | Le webhook de ce salon n'est pas configuré. |
| `#bounties` vide pendant des jours | Normal : les primes payées et sans concurrence sont rares. |
| Exécution rouge dans Actions | Un flux a planté. L'alerte détaillée est dans `#statut`, les autres flux ont continué. |
