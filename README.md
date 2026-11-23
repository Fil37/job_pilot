# Job Pilot

Suit ta recherche d'emploi de bout en bout : trouve les offres, les filtre, les classe, prépare les candidatures,
comble les lacunes et suit les réponses. **Rien n'est jamais envoyé vers l'extérieur sans ta validation.**

Version 0.4 (2026-11-23) : les 12 agents sont construits et actifs (profil, collecte, filtre d'expérience,
score, récapitulatif, interface, supervision). L'interface lance un planificateur : chaîne quotidienne à 07:30, boîte mail toutes les 15 minutes, analyse
hebdomadaire le lundi, publications de portfolio dues toutes les heures (tant que l'application tourne).

## Démarrer

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp app/assets/job_pilot.example.json app/instance/job_pilot.json    # tes critères
cp app/assets/profile.example.yaml   app/instance/profile.yaml      # TON profil (remplacer l'exemple)
cp .env.example .env                                                # clés (France Travail…)
app/agent/run                     # chaîne quotidienne : collecter, filtrer, classer
.venv/bin/python -m app.web.app   # interface : http://127.0.0.1:8795/
```

Autres lancements : `app/agent/run --chain rescore` (refiltrer et reclasser), `--only agent003` (un agent seul),
`--skip agent002` (l'agent sauté est enregistré « skipped »), `--list`. Chaque agent se lance aussi seul :
`.venv/bin/python -m app.agent.agent003_experience_filter`.

## Agents

| Agent | Mission | État |
|---|---|---|
| agent000_job_pilot | orchestrateur : chaînes, dépendances, arrêt sur échec critique, incidents, rapport, récapitulatif | construit |
| agent001_profile_builder | profil → empreinte, expérience comptable, mots-clés, intitulés voisins, critères éliminatoires | construit |
| agent002_offer_collector | France Travail (API), offres collées à la main, Greenhouse / Lever des entreprises cibles ; normalisation, doublons | construit |
| agent003_experience_filter | lit tout le texte : années, « hors stage / alternance », junior / senior, contradictions → garder / rejeter / à vérifier | construit |
| agent004_offer_scorer | score = somme pondérée, satisfait / manques, éliminations avec raison | construit |
| agent005_company_researcher | fiche entreprise : registre officiel (API Recherche d'entreprises), Wikipédia, signal de recrutement ; chaque fait sourcé | construit |
| agent006_application_builder | CV LaTeX (Tectonic), lettre si demandée, paquet ; DeepSeek propose titre / accroche / résumé, le programme vérifie (faits du profil, 1 page, ATS) | construit |
| agent007_gap_project_planner | chaque offre favorable à laquelle il manque 1 ou 2 compétences reçoit son propre brief de projet (DeepSeek, vérifié), dans la chaîne quotidienne, 3 au plus par passage ; les compétences les plus demandées sur 30 jours restent affichées | construit |
| agent008_portfolio_publisher | démo de ton vrai portfolio avec la carte ; publication datée et retrait après validation (index.html seulement, entre balises) | construit |
| agent009_inbox_tracker | Gmail en lecture seule (IMAP EXAMINE + BODY.PEEK), rattachement, classement avec confiance, alertes ; aucun corps de mail stocké | construit (attend IMAP_PASSWORD) |
| agent010_followup_coach | fiche d'entretien, proposition d'agenda (.ics après validation), brouillon de relance (jamais envoyé) | construit |
| agent011_pipeline_analyst | statistiques, proposition de pondérations (validation) | construit |

## Sources et conditions d'utilisation (vérifiées le 2026-11-21)

| Source | Accès | État |
|---|---|---|
| Arbeitnow (API publique gratuite, sans clé) | 3 pages de 300 offres au plus par jour, texte complet ; tous les pays gardés, l'étranger est marqué « hors de France » au score | active |
| France Travail (pages publiques) | pages métier × département des plans du site officiels (tous les départements avec `mobility_france`, tes zones d'abord, 200 requêtes au plus) ; `robots.txt` relu à chaque passage (recherche par mots-clés interdite aux robots : jamais demandée) ; 1 requête/s ; sans connexion ni cookie ; **texte complet** de chaque offre lu ensuite dans le JSON que sa page publique charge elle-même (`/api-descriptifoffre/offre/<id>`, autorisé par le robots.txt), 250 offres au plus par passage, les plus récentes d'abord | active |
| France Travail (API) | fermée aux nouvelles applications depuis 2026-10 (data.gouv.fr) ; tes identifiants n'obtiennent que le droit « application » | désactivée |
| Offres collées à la main | formulaire de l'accueil (LinkedIn, Indeed, tout site) | active |
| Greenhouse, Lever | API publiques de job boards des entreprises cibles (Alice & Bob) | active |
| HelloWork | CGU 8.2 : scraping « strictement interdit, commerciales ou non » | jamais (alertes e-mail) |
| Welcome to the Jungle | CGU : robots / extraction automatisée interdits | jamais (alertes e-mail) |
| Meteojob | CGU : extraction des bases de données interdite | jamais (alertes e-mail) |
| APEC | protection anti-robot (même `robots.txt` en 403) : jamais contournée | jamais (alertes e-mail) |
| JobTeaser | derrière une connexion école | jamais (alertes e-mail) |
| LinkedIn, Indeed | CGU : accès automatisé interdit ; pas de session ni de cookies réutilisés | alertes e-mail (étape 8) |

Le journal des sources réellement interrogées est sur la page Supervision.

## Modèle et LaTeX

- Modèle : DeepSeek `deepseek-flash` (`settings.json` → `llm`, réglage `thinking: disabled`), clé `DEEPSEEK_API_KEY` dans `.env`.
  Aucune bascule automatique ; le coût de chaque appel est rangé par run (page Supervision).
- LaTeX : Tectonic 0.17.0 dans `~/.local/bin` (sans root), toujours lancé avec `--untrusted`.
  Modèle de CV : `app/assets/cv_template.tex`, reconstruit d'après ton CV PDF.
- Préparer une candidature : bouton sur la page de l'offre, ou `app/agent/run --chain application --offer <offer_id> [--letter]`.
  Rien n'est envoyé : tu relis le paquet, tu envoies toi-même, puis « Marquer comme envoyée ».

## Données

- `app/instance/job_pilot.db` : base unique (offres, décisions, scores, candidatures, runs, tâches, incidents, skills, validations). Fichier privé (0600).
- `app/instance/job_pilot.json` : critères et préférences, sans secret (refusé au démarrage s'il en contient).
- `app/instance/profile.yaml` : ton profil, seule source de ce qui peut figurer dans un CV.
- `app/instance/runs/<run_id>/` : `report.json` et `digest.md` de chaque run.
- `app/instance/backups/` : sauvegarde quotidienne vérifiée de la base.
- Les contrats sont dans `app/schemas/` : `rows.json` (tables et propriétaire unique), `routes.json` (registre des agents, chaînes, actions à valider), `settings.json`, `words.json`, `reports.json`.

## Tests

```bash
.venv/bin/python -m pytest -p no:cacheprovider app/tests -q   # toute la suite (sans cache à la racine)
.venv/bin/python app/tests/test_experience.py    # un fichier, PASS / FAIL par cas
```

Contrôles nommés issus de vrais cas : `EXP_IGNORE_COMPANY_HORIZON`, `EXP_CUMULATIVE_REQUIREMENTS`, `EXP_JUNIOR_STRICT`
(offre Datadog « Staff Software Engineer », 2026-11-21) et `SOURCE_INTERNAL_DUPLICATES`.

## Socle

Style et supervision issus de `studio-kit` 2.2 : `app/static/css/job_pilot_master.css` (+ une classe `.pre-line`
proposée pour le kit), `app/agent/supervision.py` et `app/tests/test_supervision.py`.

## Offres connues par un extrait seulement

Une offre dont job_pilot n'a qu'un extrait n'est jamais rejetée faute d'information : elle va « à revoir ». Si elle est
prometteuse (non éliminée, score d'au moins 55), une alerte « texte complet » te l'amène une seule fois. Sur sa page,
ouvre l'offre sur son site, copie tout le texte et colle-le dans « Coller le texte complet » : il remplace l'extrait
(événement `user_full_text` tracé), l'offre est rejugée (expérience, score) et une décision que tu avais prise reste.
Quand la même offre arrive par une autre source avec son texte complet, ce texte remplace aussi l'extrait.

