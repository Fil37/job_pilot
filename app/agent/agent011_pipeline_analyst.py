"""agent011 — pipeline analyst (advisory).

Objective        : compute the monitoring figures of your job search and, when there are enough outcomes, propose
                   new scoring weights.
Scope            : applications, mails (email_events), scores; figures per source and per job family.
Out of scope     : NEVER changes the weights itself (an 'apply_scoring_weights' approval does, after you approve),
                   never contacts anyone, never asks a model to compute a figure.
Inputs           : applications, email_events, scores, offers.
Outputs          : metrics_snapshots row; an approval request when a weight change is supported by the data.
Tools            : arithmetic only.
Data read        : applications, email_events, scores, offers.
Data written     : metrics_snapshots (owner); approvals (request only).
Success criteria : every figure recomputable from the tables (check METRICS_RECOMPUTABLE); weights proposal sums to 1,
                   each weight moves by at most settings.analyst.max_weight_step.
Cost limits      : 0 model call.
Time limits      : < 2 s.
Error policy     : not enough outcomes → no proposal (said in the snapshot); advisory: its failure stops nothing.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from statistics import mean

from app.agent import BaseAgent, RunContext, envelope, fold, sha, upsert, utc_now

SKILLS = [
    {"skill_id": "SK_METRICS", "version": "1", "status": "proposed",
     "objective": "Calculer les chiffres de suivi de la recherche.",
     "summary": "Candidatures, taux de réponse, délai moyen avant la première réponse, entretiens contre refus, par "
                "source et par famille de postes ; calculés par le programme (contrôle METRICS_RECOMPUTABLE). Panneau "
                "Statistiques de la page Supervision."},
    {"skill_id": "SK_WEIGHT_TUNING", "version": "1", "status": "proposed",
     "objective": "Proposer de nouveaux poids de score à partir des résultats.",
     "summary": "Avec au moins min_outcomes candidatures ayant reçu une réponse : les composantes plus élevées dans "
                "les réponses positives gagnent du poids, de max_weight_step au plus chacune, la somme restant à 1. "
                "Appliqué seulement après ta validation (nouvelle version des poids)."},
]

AGENT_ID = "agent011"
POSITIVE = {"interview", "technical_test", "offer", "info_request"}


def family_of(title: str, families: dict) -> str:
    t = fold(title)
    for fam, titles in families.items():
        if any(fold(x) in t for x in titles):
            return fam
    return "other"


def propose_weights(weights: dict, pos: list[dict], neg: list[dict], step: float) -> dict | None:
    """Move each weight by +/- step toward the components that separate positive from negative answers; sum = 1."""
    if not pos or not neg:
        return None
    new = {}
    for k, w in weights.items():
        diff = mean(p.get(k, 0) for p in pos) - mean(n.get(k, 0) for n in neg)
        new[k] = max(0.01, w + (step if diff > 0.05 else -step if diff < -0.05 else 0))
    total = sum(new.values())
    new = {k: round(v / total, 3) for k, v in new.items()}
    drift = round(1 - sum(new.values()), 3)
    k_max = max(new, key=new.get)
    new[k_max] = round(new[k_max] + drift, 3)
    return None if all(abs(new[k] - weights[k]) < 0.005 for k in weights) else new


def approve_weights(orch, target: str, params: dict) -> dict:
    """Handler of an APPROVED 'apply_scoring_weights': writes the new version in job_pilot.json (overrides.scoring)."""
    from app.agent import INSTANCE_JSON, merge_config
    weights = params["weights"]
    if abs(sum(weights.values()) - 1) > 1e-6 or set(weights) != set(orch.config["scoring"]["weights"]):
        raise ValueError("invalid weights")
    path = getattr(orch, "instance_path", INSTANCE_JSON)
    inst = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else orch.instance
    inst.setdefault("overrides", {}).setdefault("scoring", {}).update(weights_version=target, weights=weights)
    if path.is_file():
        path.write_text(json.dumps(inst, ensure_ascii=False, indent=1), encoding="utf-8")
    orch.instance = inst
    orch.config = merge_config(orch.schemas["settings"], inst)
    return {"applied": True, "weights_version": target}


class Agent(BaseAgent):
    AGENT_ID = AGENT_ID
    SKILLS = SKILLS

    def work(self, ctx: RunContext, subtasks):
        ctx.store.register_check("METRICS_RECOMPUTABLE", AGENT_ID, "chiffres recalculés à partir des tables, jamais générés",
                                 "rule of the method: every displayed figure is computed by the program")
        with ctx.connect() as c:
            apps = [dict(r) for r in c.execute(
                "SELECT a.application_id, a.status, a.sent_at, a.source_id, o.title, o.offer_id FROM applications a "
                "JOIN offers o USING(offer_id) WHERE a.status != 'ready'")]
            events = [dict(r) for r in c.execute("SELECT application_id, category, received_at FROM email_events "
                                                 "WHERE application_id IS NOT NULL AND needs_check=0 ORDER BY received_at")]
            comps = {r[0]: json.loads(r[1]) for r in c.execute("SELECT offer_id, components FROM scores")}
        first, outcome = {}, {}
        for e in events:
            first.setdefault(e["application_id"], e)
            if e["category"] in POSITIVE | {"rejection"}:
                outcome[e["application_id"]] = "positive" if e["category"] in POSITIVE else "negative"
        sent = [a for a in apps if a["sent_at"]]
        answered = [a for a in sent if a["application_id"] in first]
        delays = [(datetime.fromisoformat(first[a["application_id"]]["received_at"]) - datetime.fromisoformat(a["sent_at"])).days
                  for a in answered]
        families = ctx.words["title_families"]

        def group(key):
            out = {}
            for a in sent:
                g = out.setdefault(key(a), {"sent": 0, "answered": 0, "positive": 0, "negative": 0})
                g["sent"] += 1
                g["answered"] += a["application_id"] in first
                g["positive"] += outcome.get(a["application_id"]) == "positive"
                g["negative"] += outcome.get(a["application_id"]) == "negative"
            return out

        metrics = {"applications_sent": len(sent), "answered": len(answered),
                   "answer_rate": round(len(answered) / len(sent), 3) if sent else None,
                   "avg_days_to_first_answer": round(mean(delays), 1) if delays else None,
                   "interviews_or_tests": sum(1 for v in outcome.values() if v == "positive"),
                   "rejections": sum(1 for v in outcome.values() if v == "negative"),
                   "by_source": group(lambda a: a["source_id"]),
                   "by_family": group(lambda a: family_of(a["title"], families)),
                   "computed_at": utc_now()}
        # METRICS_RECOMPUTABLE: the totals of the groups equal the global figures
        ok = sum(g["sent"] for g in metrics["by_source"].values()) == metrics["applications_sent"]
        proposal, note = None, None
        cfg, scoring = ctx.config["analyst"], ctx.config["scoring"]
        n_out = len(outcome)
        if n_out >= cfg["min_outcomes"]:
            vals = lambda o: comps.get(o, {}).get("values", {})  # noqa: E731
            by_offer = {a["application_id"]: a["offer_id"] for a in sent}
            pos = [vals(by_offer[k]) for k, v in outcome.items() if v == "positive" and k in by_offer]
            neg = [vals(by_offer[k]) for k, v in outcome.items() if v == "negative" and k in by_offer]
            proposal = propose_weights(scoring["weights"], pos, neg, cfg["max_weight_step"])
        else:
            note = f"{n_out} candidature(s) avec réponse, {cfg['min_outcomes']} nécessaires avant de proposer de nouveaux poids"
        metrics["weights_note"] = note
        with ctx.connect() as c:
            upsert(c, ctx.rows, "metrics_snapshots", {"snapshot_id": "m_" + sha(ctx.run_id, 12), "metrics": metrics,
                   "run_id": ctx.run_id, "created_at": utc_now()})
        if proposal:
            version = "w" + datetime.now().strftime("%Y%m%d")
            ctx.store.request_approval("apply_scoring_weights", version, {"weights": proposal, "current": scoring["weights"],
                                       "basis": f"{n_out} answered applications"})
        ctx.store.record_outcomes(ctx.run_id, [{"check": "METRICS_RECOMPUTABLE", "outcome": "pass" if ok else "kept_despite"}])
        return envelope(ctx, AGENT_ID, "succeeded", data={"metrics": metrics, "weights_proposal": proposal},
                        metrics={"sent": len(sent), "answered": len(answered)})


if __name__ == "__main__":
    from app.agent.agent000_job_pilot import run_standalone
    sys.exit(run_standalone(AGENT_ID))
