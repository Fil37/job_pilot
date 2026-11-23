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
| agent007_gap_project_planner | trois types de projets (DeepSeek, vérifiés), 3 par passage dans la chaîne quotidienne : combler (une offre favorable à laquelle il manque 1 ou 2 compétences), renforcer (une compétence « pratique » demandée par au moins 10 offres, passée en « maîtrise »), élargir (une compétence acceptée à la place d'une des tiennes, comme Verilog pour VHDL) ; une place par jour réservée à renforcer ou élargir ; chaque brief liste les offres qu'il améliore et leur gain de score | construit |
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
| Greenhouse, Lever | API publiques de job boards des entreprises que tu suis (dans job_pilot.json) | active |
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

## Contrôle qualité du CV face à l'offre (CV_OFFER_MATCH)

Après compilation, agent006 relit le **texte du PDF** comme un logiciel de tri et écrit dans le dossier la section
« Correspondance du CV avec l'offre » : taux de couverture des compétences demandées, compétences de ton profil oubliées
dans le CV (le programme les ajoute lui-même dans une ligne « Également » si le CV tient toujours sur une page),
compétences absentes de ton profil (jamais ajoutées, seulement signalées) et reprise de l'intitulé de l'offre dans ton titre.

## Modifier un projet proposé

Sur la page d'un projet, « Modifier le projet » ouvre un formulaire avec tous ses champs. Le fichier du brief est réécrit,
la modification est tracée (`user_brief_edit`) ; seul le résumé public garde sa règle, puisqu'il va sur ton portfolio.

## Projet réalisé : ta carte de portfolio

La carte n'est plus fabriquée à partir de l'idée de projet. Sur la page d'un projet (ou depuis l'accueil),
« Projet réalisé » ouvre une fiche : date réelle, nom court, domaine, contexte, titre, ce que tu as construit,
jusqu'à 3 chiffres réels, volet dépliable, mots-clés, lien https, catégories de ton portfolio, place dans la grille
(en tête, après une carte précise ou à la fin) et image facultative (png, jpg, webp ou svg, 2 Mo au plus, sans script).
La carte suit exactement le balisage de tes cartes « Autres projets » ; la démo la montre avant toute publication et
la publication envoie la page et, au plus, ton image (contrôle PORTFOLIO_DIFF_SCOPE).

## Gain d'un projet pour son offre (PROJECT_GAIN)

Pour chaque projet proposé, job_pilot calcule deux fois le score de l'offre : avec ton profil actuel (le meilleur CV
possible aujourd'hui) puis avec les compétences du projet au niveau « pratique ». Couverture des compétences de
l'offre et score, avant et après, sont affichés dans le brief, sur la page de l'offre et dans le dossier de candidature.

## Compétences au choix (SKILL_ALTERNATIVES)

Quand une offre écrit « VHDL ou Verilog », « VHDL et/ou Verilog » ou « VHDL/Verilog » et que tu as l'une des deux,
l'autre n'est pas comptée manquante (le score l'indique : « vhdl accepté à la place de verilog »). Seuls des outils
interchangeables comptent (groupes de `skill_alternative_groups` dans words.json : langages HDL, CAO de cartes...).

## Modularité (2026-11-23)

- **Un agent n'importe jamais un autre agent.** Les outils partagés sont dans `app/agent/common_*.py` :
  `common_llm` (appel au modèle avec corrections), `common_profile` (profil, faits, tes compétences : une seule
  définition), `common_http` (client HTTP borné, robots.txt), `common_text` (intitulés, formes juridiques),
  `common_company` (fiches entreprise), `common_scoring` (score, compétences au choix, gain d'un projet),
  `common_projects` (date réelle, image, grille du portfolio, matériel).
- **Une source par fichier** : `source_base` (format commun d'une offre), `source_france_travail`, `source_ats`
  (Greenhouse, Lever), `source_arbeitnow`. Ajouter une source ne touche aucune autre.
- **L'interface ne modifie aucune table elle-même** : chaque action (ta décision sur une offre, un texte collé, un
  projet modifié, une fiche « Projet réalisé », une demande de publication...) est faite par l'agent propriétaire
  des données, via `agent000.act`. Les actions permises sont déclarées dans `routes.json` (`user_actions`) et tracées.
- **Quand le texte d'une offre change**, agent002 l'horodate (`text_updated_at`) ; agent003 et agent004 la rejugent
  d'eux-mêmes, sans jamais effacer une décision que tu as prise.
- **Tests de garde** (`app/tests/test_modularity.py`) : aucun import entre agents, l'interface ne parle qu'à
  l'orchestrateur et aux modules communs, chaque table n'est écrite que par son propriétaire (ou un co-écrivain
  déclaré dans `rows.json` sur ses champs : agent009 fait avancer le statut d'une candidature d'après tes mails),
  actions déclarées = actions codées.

## Pour une autre personne : premiers pas

1. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, puis Tectonic (CV en PDF) et, si possible,
   poppler-utils (sinon pypdf est utilisé).
2. Une clé de modèle dans `.env` : `DEEPSEEK_API_KEY=...` (voir `.env.example`).
3. `.venv/bin/python -m app.web.app` (ou `streamlit run app/web/streamlit_app.py`) : sans configuration, job_pilot démarre
   avec les exemples, puis la page **Premiers pas** indique ce qui manque et permet :
   - **Importer ton CV (PDF)** : brouillon de profil ; ton e-mail et ton téléphone sont masqués avant que le modèle lise
     le CV, chaque phrase du brouillon doit être écrite mot pour mot dans le CV ; tu relis et tu valides ;
   - **Ta recherche** : postes visés, familles de métiers, contrats, villes, mobilité, salaire, années, mots exclus,
     entreprises suivies et, si tu en as un, **ton portfolio** (dépôt GitHub Pages et ses catégories). Le lien public de
     ton portfolio se met dans ton profil (`identity.links.portfolio`) et apparaît sur ton CV.

## En ligne sur Streamlit, pour toi seul

Le code reste le même : l'interface Streamlit (`app/web/streamlit_app.py`) passe par les mêmes actions d'agents. Ton
dossier d'instance (base, profil, critères, fichiers) est enregistré comme **un instantané chiffré** dans Postgres
(Neon) ; GitHub Actions fait les tâches automatiques pendant que l'application dort (`.github/workflows/job_pilot.yml`).

1. **Neon** (gratuit) : crée un projet, copie sa chaîne de connexion `postgresql://...?sslmode=require`.
2. **Clé de chiffrement** : `.venv/bin/python -m app.agent.common_cloud new-key`. Garde-la précieusement (gestionnaire de
   mots de passe) : sans elle, tes données en ligne sont illisibles, y compris pour toi.
3. **Première mise en ligne de tes données** (depuis ta machine) :
   `JOB_PILOT_DATABASE_URL=... JOB_PILOT_SNAPSHOT_KEY=... .venv/bin/python -m app.agent.common_cloud push`
4. **GitHub** : un dépôt **privé** pour ce dossier (tes données et ton `.env` ne sont jamais versionnés). Dans Settings →
   Secrets and variables → Actions : `DEEPSEEK_API_KEY`, `JOB_PILOT_DATABASE_URL`, `JOB_PILOT_SNAPSHOT_KEY`, et si besoin
   `IMAP_PASSWORD`, `PORTFOLIO_TOKEN` (GitHub refuse les secrets nommés `GITHUB_*`). Les chaînes tournent : recherche du
   jour à 07:30 (heure de Paris en hiver), mails toutes les heures, analyse le lundi ; à la main : Actions → job_pilot →
   Run workflow.
5. **Streamlit Community Cloud** : New app → ce dépôt, fichier principal `app/web/streamlit_app.py`, Python 3.12 ;
   Advanced settings → Secrets (format TOML) : les mêmes clés, plus `JOB_PILOT_APP_PASSWORD = "..."` (mot de passe en
   plus de l'accès privé). Une application d'un dépôt privé est privée : invite seulement ton adresse.
6. Ouvre l'application, page **Premiers pas** : tout doit être « prêt » (le portfolio et la boîte mail sont facultatifs).

À savoir : l'application s'endort après 12 h sans visite (réveil d'environ 30 s) ; les alertes mail arrivent au rythme
d'Actions (toutes les heures, parfois en retard) ; le premier CV compilé en ligne télécharge Tectonic (empreinte
vérifiée) et ses polices, il prend une à deux minutes. Pendant une tâche automatique, l'interface est en lecture seule ;
si une tâche a enregistré entre-temps, ta dernière action t'est signalée à refaire (rien n'est écrasé).

Fichiers à la racine propres au déploiement (exceptions à la règle de racine, imposées par les plateformes) :
`.github/workflows/` (GitHub Actions) et `packages.txt` (paquets système de Streamlit Cloud).

