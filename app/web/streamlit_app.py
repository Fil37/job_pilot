"""job_pilot online (Streamlit), for one person (2026-11-23).

    streamlit run app/web/streamlit_app.py

Same agents, same actions as the Flask interface (agent000.act); this file only reads the base and asks the owning
agents to act. Online, the instance folder (your base, profile, criteria, files) is restored from its encrypted snapshot
(common_cloud.py) at the start and saved after each action; while GitHub Actions runs a chain, the page is read-only.
Secrets (Streamlit settings → Secrets): DEEPSEEK_API_KEY, JOB_PILOT_DATABASE_URL, JOB_PILOT_SNAPSHOT_KEY, optional
JOB_PILOT_APP_PASSWORD, IMAP_PASSWORD, PORTFOLIO_TOKEN. Locally without them: your usual app/instance folder.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# STREAMLIT_IMPORT_PATH (real case 2026-11-23): `streamlit run app/web/streamlit_app.py` puts app/web/ first on the path,
# where app.py (the Flask interface) would shadow the package `app`. The project root goes first, app/web/ out.
_here = Path(__file__).resolve().parent
sys.path[:] = [x for x in sys.path if Path(x or ".").resolve() != _here]
sys.path.insert(0, str(ROOT))
if "app" in sys.modules and not hasattr(sys.modules["app"], "__path__"):
    del sys.modules["app"]

import streamlit as st  # noqa: E402


def _fresh_code() -> None:
    """STALE_ORCHESTRATOR: when an update changed a file of the agents or the schemas since the last run of this
    process, job_pilot's modules are imported again from scratch (Streamlit only reruns this script)."""
    files = [*(ROOT / "app" / "agent").glob("*.py"), *(ROOT / "app" / "schemas").glob("*.json")]
    stamp = str(max((f.stat().st_mtime_ns for f in files), default=0))
    if os.environ.get("_JOB_PILOT_CODE_STAMP") not in (None, stamp):
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
    os.environ["_JOB_PILOT_CODE_STAMP"] = stamp


_fresh_code()

st.set_page_config(page_title="Job Pilot", page_icon="🧭", layout="wide")


def _secrets_to_env() -> None:
    """Streamlit secrets become environment variables BEFORE job_pilot is imported (paths and keys are read there)."""
    try:
        items = {k: v for k, v in st.secrets.items() if isinstance(v, (str, int, float))}
    except Exception:  # noqa: BLE001 - no secrets file locally
        items = {}
    for k, v in items.items():
        os.environ.setdefault(k, str(v))
    if os.environ.get("JOB_PILOT_DATABASE_URL"):            # online: the instance lives in a temporary folder
        os.environ.setdefault("JOB_PILOT_INSTANCE_DIR", str(Path(tempfile.gettempdir()) / "job_pilot_instance"))


_secrets_to_env()

from app.agent import INSTANCE_DIR, NotFound, db_connect, load_schemas, runs_path  # noqa: E402
from app.agent import common_cloud as cloud  # noqa: E402
from app.agent.agent000_job_pilot import Orchestrator, application_params, build_digest, setup_checks  # noqa: E402
from app.agent.common_projects import hw_item  # noqa: E402
from app.agent.common_scoring import project_gain, skill_label  # noqa: E402

WORDS = load_schemas()["words"]
FR = {**WORDS["ui_text"]["fr"], **WORDS.get("skill_text", {}).get("fr", {})}
PATTERNS = [(re.compile(a), b) for a, b in WORDS.get("ui_patterns", {}).get("fr", [])]


def tr(text) -> str:
    """The French label of an interface or agent message (the same tables as the Flask interface)."""
    if not isinstance(text, str) or not text:
        return text
    if text in FR:
        return FR[text]
    for rx, rep in PATTERNS:
        text = rx.sub(rep, text)
    return text


MD_SPECIAL = re.compile(r"([\\`*_\[\]$~|<>#])")


def md(text) -> str:
    """MD_ESCAPE (real case 2026-11-25: offer and model texts broke the page layout: a '$' opened a formula, '*' or '_'
    italics, '[' a link). Texts from offers, companies and models are escaped before going into Markdown."""
    return MD_SPECIAL.sub(r"\\\1", str(text or ""))


STATE_COLOR = {"keep": "green", "succeeded": "green", "healthy": "green", "ready": "green", "ok": "green",
               "reject": "red", "failed": "red", "critical": "red", "missing": "red",
               "review": "orange", "partial": "orange", "degraded": "orange",
               "running": "blue", "sent": "blue", "optional": "gray"}


def skin() -> None:
    """Studio Kit look on top of the Streamlit theme (.streamlit/config.toml): hero band, cards, metrics, buttons."""
    st.markdown("""<style>
      .block-container { padding-top: 2.2rem; max-width: 1180px; }
      .jp-hero { background: #14161a; color: #f2f3ef; border-radius: 16px; padding: 22px 26px; margin-bottom: 18px;
                 box-shadow: 0 1px 2px rgba(30,28,20,.06), 0 8px 22px rgba(30,28,20,.07); position: relative; overflow: hidden; }
      .jp-hero:after { content: ""; position: absolute; right: -60px; top: -80px; width: 260px; height: 260px;
                       background: radial-gradient(circle, rgba(198,239,90,.22), transparent 65%); }
      .jp-hero h1 { font-family: "Space Grotesk", Inter, sans-serif; font-size: 1.9rem; margin: 0; color: #f2f3ef; }
      .jp-hero p { margin: 4px 0 0; color: #a9aeb5; font-size: .95rem; }
      .jp-hero .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; background: #c6ef5a;
                      margin-right: 10px; box-shadow: 0 0 0 4px rgba(198,239,90,.18); vertical-align: middle; }
      [data-testid="stMetric"] { background: var(--secondary-background-color, #efede7); border-radius: 14px;
                                 padding: 14px 16px; border: 1px solid rgba(128,128,128,.22); }
      [data-testid="stMetricValue"] { font-family: "Space Grotesk", Inter, sans-serif; font-weight: 600; }
      .stButton > button, .stDownloadButton > button, .stLinkButton > a { border-radius: 999px; font-weight: 600; }
      h2, h3 { font-family: "Space Grotesk", Inter, sans-serif; letter-spacing: -.01em; }
    </style>""", unsafe_allow_html=True)


def hero(title: str, subtitle: str) -> None:
    """The dark Studio Kit band; texts are HTML-escaped (they may come from an offer)."""
    import html as _html
    st.markdown(f'<div class="jp-hero"><h1><span class="dot"></span>{_html.escape(title)}</h1>'
                f'<p>{_html.escape(subtitle)}</p></div>', unsafe_allow_html=True)


# =========================================================================== access, snapshot, orchestrator
def gate() -> None:
    """Optional password (JOB_PILOT_APP_PASSWORD) on top of the private Streamlit app."""
    pw = os.environ.get("JOB_PILOT_APP_PASSWORD")
    if not pw or st.session_state.get("auth"):
        return
    st.title("🧭 Job Pilot")
    entered = st.text_input("Mot de passe", type="password")
    if entered:
        if hmac.compare_digest(entered.encode(), pw.encode()):
            st.session_state.auth = True
            st.rerun()
        st.error("Mot de passe incorrect.")
    st.stop()


@st.cache_resource
def store():
    return cloud.store_from_env()


@st.cache_resource
def _orch(version: int, code: int) -> Orchestrator:
    """STALE_ORCHESTRATOR (real case 2026-11-25: after an update, the cached orchestrator was built from the OLD code:
    "AttributeError: approve_all_skills"). The cache key includes the identity of the Orchestrator class: when an
    update reloads the code, a new orchestrator is built."""
    o = Orchestrator(backup=store() is None)
    o.startup()
    return o


def orch() -> Orchestrator:
    s = store()
    if s and not st.session_state.get("pulled"):
        cloud.pull(s, INSTANCE_DIR)
        st.session_state.pulled = True
    return _orch(cloud.local_version(INSTANCE_DIR) if s else 0, id(Orchestrator))


def busy_by() -> str | None:
    s = store()
    return s.lease_holder() if s else None


def save_online() -> None:
    """After a change: save the snapshot; if GitHub Actions saved in between, reload (the change must be redone)."""
    s = store()
    if not s:
        return
    try:
        cloud.push(s, INSTANCE_DIR)
    except cloud.SnapshotConflict:
        cloud.pull(s, INSTANCE_DIR)
        st.session_state.flash = ("warning", "Les données ont été mises à jour entre-temps par une tâche automatique : "
                                             "elles sont rechargées, refais ton action.")


def act(agent_id: str, action: str, ok: str | None = None, **params):
    """USER_ACTIONS through the orchestrator, then saved online. Returns the result or None (message shown)."""
    holder = busy_by()
    if holder:
        st.warning(f"Lecture seule : une tâche automatique travaille ({holder}). Réessaie dans quelques minutes.")
        return None
    try:
        res = orch().act(agent_id, action, **params)
    except (ValueError, RuntimeError) as exc:
        st.error(tr(str(exc)))
        return None
    except NotFound:
        st.error("Introuvable.")
        return None
    save_online()
    if ok:
        st.session_state.flash = ("success", ok)
    return res


def run_chain(chain: str, params: dict | None = None, title: str | None = None):
    """A chain run from the page (lease held meanwhile, then saved online)."""
    s, owner = store(), "streamlit"
    if s and not s.lease(owner, 1800):
        st.warning(f"Une tâche automatique travaille déjà ({s.lease_holder()}).")
        return None
    try:
        with st.spinner("En cours… (quelques minutes pour la recherche du jour)"):
            report = orch().run(chain, params=params, title=title or f"{chain} (Streamlit)")
        save_online()
        st.session_state.flash = ("success" if report["status"] == "succeeded" else "warning", tr(report["summary"]))
        return report
    finally:
        if s:
            s.release(owner)


def rows(sql: str, *args) -> list[dict]:
    with db_connect(orch().db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def one(sql: str, *args):
    with db_connect(orch().db_path) as c:
        r = c.execute(sql, args).fetchone()
    return dict(r) if r else None


def flash() -> None:
    if st.session_state.get("flash"):
        kind, msg = st.session_state.pop("flash")
        getattr(st, kind)(msg)


def go(page: str, **state) -> None:
    st.session_state.update(state, page=page)
    st.rerun()


# =========================================================================== pages
def page_home() -> None:
    o = orch()
    digest = build_digest(o.db_path, o.config["digest"], o.config["scoring"]["weights_version"])
    hero("Job Pilot", "Ta recherche d'emploi, triée et préparée chaque jour. Rien n'est envoyé sans toi.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Offres vues", one("SELECT COUNT(*) n FROM offers")["n"])
    c2.metric("Retenues", len(digest["top"]))
    c3.metric("À revoir", len(digest["review"]))
    c4.metric("Candidatures", one("SELECT COUNT(*) n FROM applications")["n"])
    b1, b2, b3 = st.columns(3)
    if b1.button("▶ Lancer la recherche du jour", disabled=bool(busy_by()), type="primary", width="stretch"):
        run_chain("daily")
        st.rerun()
    if b2.button("↻ Re-classer", disabled=bool(busy_by()), width="stretch"):
        run_chain("rescore")
        st.rerun()
    if b3.button("✉ Lire la boîte mail", disabled=bool(busy_by()), width="stretch"):
        run_chain("inbox")
        st.rerun()
    alerts = rows("SELECT * FROM alerts WHERE seen_at IS NULL ORDER BY alert_id DESC LIMIT 30")
    with st.expander(f"🔔 Alertes ({len(alerts)})", expanded=bool(alerts) and len(alerts) <= 6):
        for a in alerts:
            with st.container(border=True):
                col1, col2 = st.columns([9, 1])
                with col1:
                    st.badge(tr(a["category"]), color="blue" if a["kind"] == "immediate" else "gray")
                    st.markdown(f"{md(a['message'])}  \n:grey[{a['created_at'][:16].replace('T', ' ')}]")
                if col2.button("✓ Vu", key=f"al{a['alert_id']}"):
                    act("agent000", "mark_alerts_seen", alert_id=a["alert_id"])
                    st.rerun()
    for title, items, review in (("Meilleures offres du jour", digest["top"], False), ("À revoir", digest["review"], True)):
        st.subheader(f"{title} ({len(items)})")
        for r in items:
            with st.container(border=True):
                col0, col1, col2 = st.columns([1, 8, 1.2], vertical_alignment="center")
                col0.markdown(f"### {r['score']:.0f}")
                with col1:
                    st.markdown(f"**{md(r['title'])}**  \n{md(r['company'])} · {md(r.get('location') or '?')}"
                                f"{' · ' + md(r['contract']) if r.get('contract') else ''}  \n:grey[{md(' · '.join(r['why']))}]")
                    if review:
                        st.badge(md(r["reason"])[:120], color="orange")
                if col2.button("Ouvrir", key=f"{title[:3]}{r['offer_id']}", width="stretch"):
                    go("Offre", offer=r["offer_id"])


def page_offer() -> None:
    o = orch()
    choices = rows("SELECT o.offer_id, o.title, o.company, COALESCE(MAX(s.score), 0) score FROM offers o LEFT JOIN scores s "
                   "USING(offer_id) GROUP BY o.offer_id ORDER BY score DESC LIMIT 300")
    ids = [c["offer_id"] for c in choices]
    label = {c["offer_id"]: f"{c['score']:.0f} · {c['title']} · {c['company']}" for c in choices}
    cur = st.session_state.get("offer")
    oid = st.selectbox("Offre", ids, index=ids.index(cur) if cur in ids else 0, format_func=label.get) if ids else None
    if not oid:
        st.info("Aucune offre pour l'instant : lance la recherche du jour.")
        return
    st.session_state.offer = oid
    off = one("SELECT * FROM offers WHERE offer_id=?", oid)
    dec = one("SELECT * FROM experience_decisions WHERE offer_id=? ORDER BY decided_at DESC LIMIT 1", oid)
    sc = one("SELECT * FROM scores WHERE offer_id=? ORDER BY scored_at DESC LIMIT 1", oid)
    hero(off["title"], " · ".join(x for x in (off["company"], off.get("location"), off.get("contract"),
                                               off.get("salary_text"), tr(off["source_id"])) if x))
    if (off.get("url") or "").startswith(("http://", "https://")):
        st.link_button("↗ Voir l'annonce", off["url"])
    if dec:
        d = dec.get("user_override") or dec["decision"]
        st.badge(f"Expérience : {tr(d)}" + (" (ta décision)" if dec.get("user_override") else ""),
                 color=STATE_COLOR.get(d, "gray"))
        st.markdown(md(dec["reason"]) + (f"  \n> {md(dec['source_sentence'])}" if dec.get("source_sentence") else ""))
        k1, k2, k3 = st.columns(3)
        if k1.button("Garder"):
            act("agent003", "set_user_decision", offer_id=oid, choice="keep", ok="Décision enregistrée.")
            st.rerun()
        if k2.button("Rejeter"):
            act("agent003", "set_user_decision", offer_id=oid, choice="reject", ok="Décision enregistrée.")
            st.rerun()
        if k3.button("Écarter l'offre"):
            act("agent002", "set_offer_status", offer_id=oid, status="dismissed", ok="Offre écartée.")
            st.rerun()
    if sc:
        comp = json.loads(sc["components"])
        st.subheader(f"Score : {sc['score']:.0f} / 100")
        st.dataframe([{"composante": tr(k), "valeur": round(comp["values"][k], 2), "poids": w, "note": tr(comp["notes"].get(k, ""))}
                      for k, w in comp["weights"].items()], hide_index=True, width="stretch")
        st.markdown("**Satisfait :** " + (", ".join(f"{m['skill']} ({tr(m['level'])})" for m in json.loads(sc["matched"])) or "aucune")
                    + "  \n**Manques :** " + (", ".join(json.loads(sc["missing"])) or "aucun"))
    for b in rows("SELECT brief_id, skill, title, json_extract(brief, '$.alternatives') alts FROM project_briefs WHERE offer_id=?", oid):
        g = project_gain_for(oid, b["skill"])
        st.info(f"**Projet proposé** : {md(b['title'])} ({', '.join(skill_label(x.strip(), json.loads(b['alts'] or '{}')) for x in b['skill'].split(','))})"
                + (f"  \nCouverture du CV optimal : {g['coverage_now']} % → {g['coverage_with']} % ({g['coverage_gain']:+d} points) · "
                   f"score {g['score_now']:.0f} → {g['score_with']:.0f}" if g else ""))
    st.subheader("Candidature")
    apps = rows("SELECT * FROM applications WHERE offer_id=? ORDER BY created_at DESC", oid)
    for a in apps:
        folder = o.runs_dir / a["package_ref"]
        cols = st.columns(4)
        cols[0].badge(tr(a["status"]), color=STATE_COLOR.get(a["status"], "gray"))
        cols[0].caption(a["created_at"][:16].replace("T", " "))
        for i, f in enumerate(("cv.pdf", "letter.pdf")):
            if (folder / f).is_file():
                cols[i + 1].download_button(f"⬇ {f}", (folder / f).read_bytes(), file_name=f, key=f"{a['application_id']}{f}")
        if a["status"] == "ready" and cols[3].button("✓ Marquer envoyée", key=f"sent{a['application_id']}"):
            done = act("agent006", "mark_sent", application_id=a["application_id"])
            if done:
                act("agent002", "set_offer_status", offer_id=done["offer_id"], status="applied", ok="Candidature marquée envoyée.")
            st.rerun()
        if (folder / "package.md").is_file():
            with st.expander("Dossier de candidature (contrôle qualité inclus)"):
                st.markdown((folder / "package.md").read_text(encoding="utf-8"))
    letter = st.checkbox("Avec lettre de motivation")
    if st.button("✎ Préparer la candidature (CV adapté)", disabled=bool(busy_by())):
        try:
            cloud.ensure_tectonic()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Tectonic indisponible : {exc}")
            return
        run_chain("application", params=application_params(oid, letter), title=f"candidature {oid} (Streamlit)")
        st.rerun()
    if off.get("description_complete") == 0:
        with st.form("full_text"):
            st.markdown("**Seul un extrait est connu.** Copie toute l'offre depuis son site et colle-la ici.")
            text = st.text_area("Texte complet", height=220)
            if st.form_submit_button("Coller le texte complet"):
                if act("agent002", "set_full_text", offer_id=oid, text=text, ok="Texte enregistré : l'offre est rejugée."):
                    act("agent000", "mark_alerts_seen", category="full_text", ref=oid)
                    run_chain("rescore")
                st.rerun()
    with st.expander("Description de l'annonce"):
        st.text(off["description"])


def project_gain_for(oid: str, skill_csv: str):
    o = orch()
    with db_connect(o.db_path) as c:
        return project_gain(c, oid, [x.strip() for x in skill_csv.split(",")], criteria=o.instance["criteria"],
                            words=o.schemas["words"], scoring=o.config["scoring"])


def page_projects() -> None:
    o = orch()
    hero("Projets", "Pour combler un manque, renforcer une compétence ou en élargir une, avec le gain pour tes offres.")
    briefs = rows("SELECT b.*, json_extract(b.brief, '$.kind') kind, json_extract(b.brief, '$.coverage.offers') covers "
                  "FROM project_briefs b WHERE status!='superseded' ORDER BY created_at DESC LIMIT 100")
    if not briefs:
        st.info("Aucune idée de projet pour l'instant.")
        return
    kinds = {"reinforce": "renforcement", "broaden": "élargissement"}
    label = {b["brief_id"]: f"{b['title']} · {kinds.get(b['kind'], 'combler un manque')}"
                            + (f" · améliore {b['covers']} offre(s)" if b["covers"] else "") for b in briefs}
    ids = list(label)
    cur = st.session_state.get("brief")
    bid = st.selectbox("Projet", ids, index=ids.index(cur) if cur in ids else 0, format_func=label.get)
    st.session_state.brief = bid
    b = next(x for x in briefs if x["brief_id"] == bid)
    ans = json.loads(b["brief"])
    tab1, tab2, tab3 = st.tabs(["Le projet", "Modifier", "Projet réalisé"])
    with tab1:
        if b.get("offer_id"):
            off = one("SELECT title, company, url FROM offers WHERE offer_id=?", b["offer_id"])
            if off:
                c1, c2 = st.columns([6, 1])
                if c1.button(f"Offre visée : {md(off['title'])} · {md(off['company'])}"):
                    go("Offre", offer=b["offer_id"])
                if (off.get("url") or "").startswith(("http://", "https://")):
                    c2.link_button("↗ Annonce", off["url"])
        path = runs_path(b["path"], o.runs_dir)
        text = path.read_text(encoding="utf-8") if path and path.is_file() else ""
        hw_section = re.compile(r"## Matériel\n.*?(?=\n## )", re.S)
        body = re.sub(r"\A# .*\n", "", hw_section.sub("", text))       # the title is already in the selector
        st.subheader(ans.get("title", ""))
        st.markdown(body.replace("$", "\\$") if text else "_(fichier du brief absent)_")
        hw = [hw_item(h) for h in ans.get("hardware") or []]
        if hw:
            st.subheader("Matériel (survole « ? » pour le détail)")
            for h in hw:
                detail = "  \n".join(x for x in (f"**Rôle** : {h['role']}" if h["role"] else "",
                                                  f"**À vérifier** : {h['specs']}" if h["specs"] else "",
                                                  f"**Exemple** : {h['example']}" if h["example"] else "",
                                                  f"**Alternative** : {h['alternative']}" if h["alternative"] else "") if x)
                st.markdown(f"- **{h['name']}**" + (" _(facultatif)_" if h["optional"] else ""), help=detail or None)
    with tab2:
        with st.form("edit"):
            f = {"title": st.text_input("Titre", ans.get("title", "")), "objective": st.text_area("Objectif", ans.get("objective", "")),
                 "builds_on": st.text_input("S'appuie sur (séparées par des virgules)", ", ".join(ans.get("builds_on") or [])),
                 "architecture": st.text_area("Architecture", ans.get("architecture", "")),
                 "hardware": st.text_area("Matériel (un par ligne : nom | rôle | à vérifier | exemple | alternative | facultatif)",
                                          "\n".join(" | ".join([d["name"], d["role"], d["specs"], d["example"], d["alternative"],
                                                                "facultatif" if d["optional"] else ""]).rstrip(" |")
                                                    for d in (hw_item(h) for h in ans.get("hardware") or []))),
                 "software": st.text_area("Logiciels (un par ligne)", "\n".join(ans.get("software") or [])),
                 "steps": st.text_area("Étapes (jours | jalon | livrable)", "\n".join(
                     " | ".join(str(s.get(k, "")) for k in ("days", "milestone", "deliverable")) for s in ans.get("steps") or [])),
                 "success_criteria": st.text_area("Critères de réussite", "\n".join(ans.get("success_criteria") or [])),
                 "pitfalls": st.text_area("Pièges", "\n".join(ans.get("pitfalls") or [])),
                 "interview_pitch": st.text_area("Le présenter en entretien", ans.get("interview_pitch", "")),
                 "portfolio_summary": st.text_area("Résumé public (carte du portfolio)", ans.get("portfolio_summary", "")),
                 "done_on": st.text_input("Réalisé en (date réelle : AAAA, AAAA-MM ou AAAA-MM-JJ)", ans.get("done_on") or "")}
            if st.form_submit_button("Enregistrer"):
                act("agent007", "edit_brief", brief_id=bid, form=f, ok="Projet enregistré.")
                st.rerun()
    with tab3:
        card = ans.get("card") or {}
        cats = o.config["portfolio"].get("categories") or {}
        with st.form("done"):
            form = {"done_on": st.text_input("Réalisé en (date réelle)", ans.get("done_on") or ""),
                    "short_name": st.text_input("Nom court", card.get("short_name", "")),
                    "domain": st.text_input("Domaine", card.get("domain", "")),
                    "context": st.text_input("Contexte (avant l'année)", card.get("context", "Seul")),
                    "title": st.text_input("Titre", card.get("title", ans.get("title", ""))),
                    "summary": st.text_area("Ce que tu as construit", card.get("summary", ans.get("portfolio_summary", ""))),
                    "details_title": st.text_input("Titre du volet dépliable", card.get("details_title", "Architecture")),
                    "details": st.text_area("Texte du volet", card.get("details", ans.get("architecture", ""))),
                    "tags": st.text_input("Mots-clés (virgules)", ", ".join(card.get("tags") or [])),
                    "link_url": st.text_input("Lien (https://…)", card.get("link_url", "")),
                    "link_label": st.text_input("Texte du lien", card.get("link_label", "Code sur GitHub")),
                    "image_alt": st.text_input("Description de l'image", card.get("image_alt", "")),
                    "position": "first"}
            figs = card.get("figures") or []
            figures = [(st.text_input(f"Chiffre {i + 1}", figs[i]["value"] if i < len(figs) else "", key=f"fv{i}"),
                        st.text_input(f"Ce qu'il mesure {i + 1}", figs[i]["label"] if i < len(figs) else "", key=f"fl{i}"))
                       for i in range(3)]
            chosen = st.multiselect("Catégories de ton portfolio", list(cats), default=[c for c in card.get("categories") or []
                                                                                       if c in cats], format_func=cats.get) if cats else []
            img = st.file_uploader("Image (png, jpg, webp, svg ; 2 Mo au plus)", type=["png", "jpg", "jpeg", "webp", "svg"])
            if st.form_submit_button("Enregistrer la fiche"):
                act("agent007", "save_project_sheet", brief_id=bid, form=form, categories=chosen, figures=figures,
                    image=(img.name, img.getvalue()) if img else None, ok="Fiche enregistrée.")
                st.rerun()
        if o.config["portfolio"].get("repo"):
            if st.button("Démo de la carte", disabled=bool(busy_by())):
                run_chain("portfolio", params={"agent008": {"brief_id": bid}}, title=f"démo portfolio {bid} (Streamlit)")
                st.rerun()
            entry = one("SELECT * FROM portfolio_entries WHERE brief_id=?", bid)
            demo = runs_path(entry.get("demo_path"), o.runs_dir) if entry else None
            if demo and demo.is_file():
                st.download_button("⬇ Page de démo (index.html)", demo.read_bytes(), file_name="index.html")
                with st.form("publish"):
                    pub = st.text_input("Date de publication (AAAA-MM-JJTHH:MM)")
                    exp = st.text_input("Date de fin (facultative)")
                    if st.form_submit_button("Demander la publication"):
                        if act("agent007", "set_done_on", brief_id=bid, done_on=form["done_on"]):
                            act("agent008", "request_publication", entry_id=entry["entry_id"], publish_at=pub,
                                expire_at=exp or None, done_on=form["done_on"],
                                ok="Publication demandée : valide-la dans Supervision.")
                        st.rerun()


def page_supervision() -> None:
    o = orch()
    hero("Supervision", "Santé des agents, validations, incidents et derniers runs.")
    data = o.store.dashboard(o.agent_ids)
    st.subheader("Santé des agents")
    st.dataframe([{"agent": a["id"], "rôle": tr(a["id"]), "état": tr(a["state"]), "détail": tr(a["detail"] or "")}
                  for a in data["agents"]], hide_index=True, width="stretch")
    apps = o.store.approvals()
    st.subheader(f"Validations en attente ({len(apps)})")
    n_skills = sum(1 for a in apps if a["action"] == "activate_skill")
    if n_skills and st.button(f"✓ Tout approuver ({n_skills} skill(s))", type="primary",
                              help="Seulement les activations de skills ; les publications, relances, rendez-vous et "
                                   "nouveaux poids restent à valider un par un."):
        if busy_by():
            st.warning("Lecture seule : une tâche automatique travaille. Réessaie dans quelques minutes.")
        else:
            done = o.approve_all_skills()
            save_online()
            st.session_state.flash = ("success", f"{done} skill(s) approuvée(s).")
            st.rerun()
    for a in apps:
        c1, c2, c3 = st.columns([6, 1, 1])
        c1.markdown(f"**{md(tr(a['action']))}** · `{a['target']}`")
        for col, label, ok in ((c2, "Approuver", True), (c3, "Rejeter", False)):
            if col.button(label, key=f"ap{a['approval_id']}{ok}"):
                try:
                    o.decide(a["approval_id"], ok)
                    save_online()
                except (ValueError, RuntimeError) as exc:
                    st.error(str(exc))
                st.rerun()
    st.subheader(f"Incidents ouverts ({len(data['incidents'])})")
    for i in data["incidents"]:
        # INCIDENT_AS_TEXT (real case 2026-11-25: a LaTeX log with a line of '====' turned into a huge Markdown title):
        # one translated, escaped summary line; the raw technical log in a folded code block
        msg = str(i["message"] or "")
        first = next((ln.strip() for ln in msg.splitlines() if ln.strip()), "")
        with st.container(border=True):
            c1, c2 = st.columns([7, 1], vertical_alignment="center")
            with c1:
                st.badge(tr(i["error_category"]), color="red")
                st.markdown(f"**{md(i['agent'])}** · {md(tr(i['agent']))} · {md(tr(first))[:240]}")
                if msg.strip() != first:
                    with st.expander("Détail technique"):
                        st.code(msg[:4000], language=None)
            if c2.button("Résolu", key=f"inc{i['id']}"):
                o.store.close_incident(i["id"], "resolved")
                save_online()
                st.rerun()
    st.subheader("Runs récents")
    st.dataframe([{"run": tr(r.get("title") or r["run_id"]), "statut": tr(r["status"]),
                   "résumé": tr(json.loads(r["totals"]).get("summary", "")) if r.get("totals") else ""}
                  for r in (data.get("runs_recent") or [])[:20]], hide_index=True, width="stretch")


def page_setup() -> None:
    o = orch()
    hero("Premiers pas", "Vérifie l'installation, importe ton CV et règle ta recherche.")
    st.subheader("Vérification")
    st.dataframe([{"élément": tr(c["item"]), "état": tr(c["state"]), "détail": c["detail"], "à faire": tr(c["fix"])}
                  for c in setup_checks(o)], hide_index=True, width="stretch")
    s = store()
    st.caption("En ligne : tes données sont sauvegardées chiffrées (version "
               f"{cloud.local_version(INSTANCE_DIR)})." if s else "Local : dossier d'instance de ta machine.")
    st.subheader("1. Ton profil à partir de ton CV")
    st.caption("Ton e-mail et ton téléphone sont masqués avant que le modèle lise ton CV ; chaque phrase du brouillon est "
               "écrite mot pour mot dans ton CV. Rien n'est utilisé avant ta validation.")
    up = st.file_uploader("Ton CV (PDF)", type=["pdf"])
    if up and st.button("Lire mon CV"):
        with st.spinner("Lecture du CV…"):
            r = act("agent001", "import_cv", pdf=up.getvalue())
        if r:
            st.session_state.flash = ("success" if not r["problems"] else "warning",
                                      "Brouillon prêt : vérifie-le ci-dessous, puis valide." if not r["problems"]
                                      else "Brouillon prêt, avec des points à corriger : " + "; ".join(r["problems"][:3]))
        st.rerun()
    draft = o.instance_dir / "profile.draft.yaml"
    prof = Path(o.instance["profile_path"])
    text = draft.read_text(encoding="utf-8") if draft.is_file() else (prof.read_text(encoding="utf-8") if prof.is_file() else "")
    with st.form("profile"):
        y = st.text_area("Ton profil (YAML) : vérifie chaque ligne, puis valide", text, height=420)
        if st.form_submit_button("✓ Valider mon profil"):
            if act("agent001", "save_profile", yaml_text=y, ok="Profil enregistré : il est utilisé dès le prochain run."):
                draft.unlink(missing_ok=True)
                save_online()
            st.rerun()
    st.subheader("2. Ta recherche")
    crit, pf = o.instance.get("criteria") or {}, o.config["portfolio"]
    with st.form("criteria"):
        form = {"roles": st.text_area("Postes visés (un par ligne)", "\n".join(crit.get("roles") or [])),
                "title_families": st.multiselect("Familles de métiers", sorted(WORDS.get("title_families", {})),
                                                 default=[f for f in crit.get("title_families") or [] if f in WORDS.get("title_families", {})]),
                "contracts": st.multiselect("Contrats", ["CDI", "CDD", "INTERIM", "STAGE", "ALTERNANCE", "FREELANCE"],
                                            default=crit.get("contracts") or []),
                "cities": st.text_area("Villes préférées (une par ligne)", "\n".join(z["label"] for z in crit.get("zones") or [])),
                "mobility_france": st.checkbox("Mobile partout en France", bool(crit.get("mobility_france"))),
                "remote_ok": st.checkbox("Télétravail complet accepté", bool(crit.get("remote_ok"))),
                "salary_min_eur": st.text_input("Salaire minimum (€/an, facultatif)", str(crit.get("salary_min_eur") or "")),
                "max_years_required": st.text_input("Années d'expérience demandées au maximum",
                                                    str((crit.get("experience_rule") or {}).get("max_years_required", 2))),
                "excluded_keywords": st.text_area("Mots exclus (un par ligne)", "\n".join(crit.get("excluded_keywords") or [])),
                "target_companies": st.text_area("Entreprises suivies (Nom | greenhouse:tableau ou lever:id)", "\n".join(
                    f"{t['name']} | " + (f"greenhouse:{t['greenhouse']}" if t.get("greenhouse") else f"lever:{t.get('lever', '')}")
                    for t in crit.get("target_companies") or [])),
                "portfolio_repo": st.text_input("Dépôt de ton portfolio (facultatif) : https://github.com/toi/toi.github.io",
                                                pf.get("repo") or ""),
                "portfolio_categories": st.text_area("Catégories de ton portfolio (clé | libellé)",
                                                     "\n".join(f"{k} | {v}" for k, v in (pf.get("categories") or {}).items()))}
        if st.form_submit_button("✓ Enregistrer ma recherche"):
            act("agent000", "save_criteria", form=form, ok="Recherche enregistrée.")
            _orch.clear()
            st.rerun()


PAGES = {"Accueil": page_home, "Offre": page_offer, "Projets": page_projects, "Supervision": page_supervision,
         "Premiers pas": page_setup}


def main() -> None:
    skin()
    gate()
    st.sidebar.title("🧭 Job Pilot")
    page = st.sidebar.radio("Pages", list(PAGES), index=list(PAGES).index(st.session_state.get("page", "Accueil")),
                            label_visibility="collapsed")
    st.session_state.page = page
    holder = busy_by()
    if holder:
        st.sidebar.warning(f"Tâche automatique en cours ({holder}) : lecture seule.")
    if store() and st.sidebar.button("↻ Recharger les données"):
        st.session_state.pulled = False
        st.rerun()
    flash()
    PAGES[page]()


main()
