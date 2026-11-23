"""First steps of another user (2026-11-23): start without any configuration, import a CV PDF into a DRAFT profile
(e-mail and phone never sent to the model, every sentence word for word in the CV), validate it, set the criteria."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import env_vars, make_orch, run_main  # noqa: E402


def tiny_pdf(lines: list[str]) -> bytes:
    """A minimal valid PDF with one text line per entry (enough for pdftotext)."""
    def esc(t):
        return t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({esc(t)}) Tj T*" for t in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            f"<< /Length {len(body.encode('latin-1'))} >>\nstream\n{body}\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{x:010d} 00000 n \n" for x in offsets).encode()
    out += f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


CV = ["Camille Martin", "Ingenieure data junior", "camille.martin@example.org - 06 12 34 56 78",
      "Experience", "Stagiaire data engineer, Acme, Paris, 2025-03 a 2025-08",
      "Pipeline d'ingestion Python et Airflow traitant 2 millions de lignes par jour",
      "Competences : Python, SQL, Airflow"]


def test_first_run_without_configuration_starts_from_the_examples():
    tmp = Path(tempfile.mkdtemp())
    code = (f"import os; os.environ['JOB_PILOT_INSTANCE_DIR']={str(tmp / 'inst')!r}\n"
            "from app.agent.agent000_job_pilot import Orchestrator, setup_checks\n"
            "o = Orchestrator(backup=False); o.startup()\n"
            "import json; print(json.dumps({c['item']: c['state'] for c in setup_checks(o)}))")
    import subprocess
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2],
                         env={**os.environ, "DEEPSEEK_API_KEY": ""})
    assert out.returncode == 0, out.stderr[-800:]
    states = json.loads(out.stdout.strip().splitlines()[-1])
    assert states["profile"] == "missing" and states["criteria"] == "missing" and states["portfolio"] == "optional"
    assert (tmp / "inst" / "job_pilot.json").is_file() and (tmp / "inst" / "profile.yaml").is_file()


def test_cv_import_masks_contact_checks_verbatim_and_validation():
    if not shutil.which("pdftotext"):
        return
    orch = make_orch()
    sent = []
    good = {"identity": {"full_name": "Camille Martin", "headline_variants": ["Ingenieure data junior"]},
            "experiences": [{"kind": "internship", "role": "Stagiaire data engineer", "company": "Acme", "city": "Paris",
                             "start": "2025-03", "end": "2025-08", "bullets": [
                                 {"text": "Pipeline d'ingestion Python et Airflow traitant 2 millions de lignes par jour",
                                  "skills": ["python", "airflow"]}]}],
            "skills": [{"name": "python", "level": "core"}, {"name": "sql", "level": "working"}]}
    invented = json.loads(json.dumps(good))
    invented["experiences"][0]["bullets"].append({"text": "Réduction de 40 % des coûts cloud", "skills": []})

    from app.agent import agent000_job_pilot as a0
    original = a0.Orchestrator._ctx

    def ctx_with_model(self, *a, **k):
        ctx = original(self, *a, **k)

        def transport(url, headers, body, timeout):
            sent.append(body["messages"])
            ans = invented if len(sent) == 1 else good
            return 200, {"choices": [{"message": {"content": json.dumps(ans)}}], "usage": {"prompt_tokens": 9, "completion_tokens": 9}}
        ctx.llm_transport = transport
        return ctx
    a0.Orchestrator._ctx = ctx_with_model
    try:
        with env_vars(DEEPSEEK_API_KEY="test-key-long"):
            r = orch.act("agent001", "import_cv", pdf=tiny_pdf(CV))
    finally:
        a0.Orchestrator._ctx = original
    first = sent[0][1]["content"]
    assert "camille.martin@example.org" not in first and "06 12 34 56 78" not in first and "[email]" in first
    assert len(sent) == 2 and "word for word" in sent[1][-1]["content"]          # the invented line was sent back
    y = r["yaml"]
    assert "40 %" not in y and "camille.martin@example.org" in y and r["problems"] == []
    assert "figures:\n" in y and "'2'" in y or "- '2'" in y                    # PROFILE_FIGURES_DECLARED by the program
    try:
        orch.act("agent001", "save_profile", yaml_text="example_profile: true\n" + y)
        raise AssertionError("example accepted")
    except ValueError:
        pass
    orch.act("agent001", "save_profile", yaml_text=y)
    from app.agent.common_profile import load_profile
    assert load_profile(Path(orch.instance["profile_path"]))["identity"]["full_name"] == "Camille Martin"


def test_criteria_from_the_interface():
    tmp = Path(tempfile.mkdtemp())
    import subprocess
    code = (f"import os; os.environ['JOB_PILOT_INSTANCE_DIR']={str(tmp / 'inst')!r}\n"
            "from app.agent.agent000_job_pilot import Orchestrator\n"
            "o = Orchestrator(backup=False); o.startup()\n"
            "try:\n o.act('agent000', 'save_criteria', form={'roles': '', 'contracts': ['CDI']}); print('NO')\n"
            "except ValueError: pass\n"
            "o.act('agent000', 'save_criteria', form={'roles': 'Data engineer\\nAnalytics engineer', 'contracts': ['CDI', 'X'],"
            " 'title_families': ['data_engineer'], 'cities': 'Lyon', 'mobility_france': '1', 'max_years_required': '3',"
            " 'portfolio_repo': 'https://github.com/camille/camille.github.io', 'portfolio_categories': 'data | Data'})\n"
            "import json; print(json.dumps([o.instance['criteria']['roles'], o.instance['criteria']['contracts'],"
            " o.config['portfolio']['repo'], o.config['portfolio']['categories']]))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2])
    assert out.returncode == 0 and "NO" not in out.stdout, out.stderr[-800:]
    roles, contracts, repo, cats = json.loads(out.stdout.strip().splitlines()[-1])
    assert roles == ["Data engineer", "Analytics engineer"] and contracts == ["CDI"]
    assert repo.endswith("camille.github.io") and cats == {"data": "Data"}


if __name__ == "__main__":
    run_main(globals())
