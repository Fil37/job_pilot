# Job Pilot

Suit ta recherche d'emploi de bout en bout : trouve les offres, les filtre, les classe, prépare les candidatures,
comble les lacunes et suit les réponses. **Rien n'est jamais envoyé vers l'extérieur sans ta validation.**

Version 0.2 (2026-11-21) : les étapes 1 à 5 et 7 du plan sont construites (CV et paquet de candidature) (profil, collecte, filtre d'expérience,
score, récapitulatif, interface, supervision). Les agents 005 à 011 ont leur contrat et leurs skills, mais restent
désactivés tant que leur étape n'est pas construite.

## Démarrer

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp app/assets/job_pilot.example.json app/instance/job_pilot.json    # tes critères
cp app/assets/profile.example.yaml   app/instance/profile.yaml      # TON profil (remplacer l'exemple)
cp .env.example .env                                                # clés (France Travail…)
app/agent/run                     # chaîne quotidienne : collecter, filtrer, classer
.venv/bin/python -m app.web.app   # interface : http://127.0.0.1:8791/
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
| agent005_company_researcher | fiche entreprise sourcée | étape 6 |
| agent006_application_builder | CV LaTeX (Tectonic), lettre si demandée, paquet ; DeepSeek propose titre / accroche / résumé, le programme vérifie (faits du profil, 1 page, ATS) | construit |
| agent007_gap_project_planner | lacunes sur 30 jours → brief de projet | étape 9 |
| agent008_portfolio_publisher | démo locale, publication datée après validation | étape 9 |
| agent009_inbox_tracker | boîte mail en lecture seule, classement avec confiance, alertes | étape 8 |
| agent010_followup_coach | fiche d'entretien, brouillon de relance | étape 8 |
| agent011_pipeline_analyst | statistiques, proposition de pondérations (validation) | étape 9 |

## Sources et conditions d'utilisation

| Source | Accès | État |
|---|---|---|
| France Travail | API officielle (`FT_CLIENT_ID` / `FT_CLIENT_SECRET` dans `.env`) | prête, à configurer |
| Offres collées à la main | formulaire de l'accueil (LinkedIn, Indeed, tout site) | prête |
| Greenhouse, Lever | API publiques de job boards, entreprises de `criteria.target_companies` | prêtes |
| HelloWork, Welcome to the Jungle, APEC | conditions non vérifiées | désactivées |
| JobTeaser | derrière une connexion école | désactivée (alertes e-mail) |
| Alertes LinkedIn / Indeed | lues dans ta boîte par agent009 | étape 8 |

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
