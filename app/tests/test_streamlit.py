"""The Streamlit interface (online, for one person, 2026-11-23): every page renders without error on a fresh instance,
the optional password blocks, an action goes through the owning agent and the encrypted snapshot is saved. Runs in a
separate process with a temporary instance folder and snapshot store (your real data is never touched)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import run_main  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r'''
import json, sys
from streamlit.testing.v1 import AppTest
out = {}
for page in ["Accueil", "Offre", "Projets", "Supervision", "Premiers pas"]:
    at = AppTest.from_file("app/web/streamlit_app.py", default_timeout=120)
    at.session_state["page"] = page
    at.session_state["auth"] = True
    at.run()
    out[page] = [str(e.value)[:200] for e in at.exception]
at = AppTest.from_file("app/web/streamlit_app.py", default_timeout=120)
at.run()
out["gate"] = bool(at.text_input) and not at.sidebar.radio
at = AppTest.from_file("app/web/streamlit_app.py", default_timeout=120)
at.session_state["page"] = "Premiers pas"
at.session_state["auth"] = True
at.run()
roles = next(t for t in at.text_area if t.label.startswith("Postes"))
roles.set_value("Data engineer")
next(m for m in at.multiselect if m.label == "Contrats").set_value(["CDI"])
next(b for b in at.button if "Enregistrer ma recherche" in b.label).click()
at.run()
out["after_save"] = [str(e.value)[:200] for e in at.exception]
print("RESULT" + json.dumps(out))
'''


def test_streamlit_pages_password_and_snapshot():
    try:
        import streamlit  # noqa: F401
        from cryptography.fernet import Fernet
    except ImportError:
        return
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ, "JOB_PILOT_INSTANCE_DIR": str(tmp / "instance"), "JOB_PILOT_SNAPSHOT_DIR": str(tmp / "store"),
           "JOB_PILOT_SNAPSHOT_KEY": Fernet.generate_key().decode(), "JOB_PILOT_APP_PASSWORD": "secret-password-1",
           "DEEPSEEK_API_KEY": ""}
    r = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, cwd=ROOT, env=env, timeout=600)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line, (r.stdout[-1500:], r.stderr[-1500:])
    out = json.loads(line[6:])
    for page in ["Accueil", "Offre", "Projets", "Supervision", "Premiers pas"]:
        assert out[page] == [], (page, out[page])
    assert out["gate"] is True and out["after_save"] == []
    inst = json.loads((tmp / "instance" / "job_pilot.json").read_text())
    assert inst["criteria"]["roles"] == ["Data engineer"]
    meta = json.loads((tmp / "store" / "main.json").read_text())
    assert meta["version"] >= 1 and b"Data engineer" not in (tmp / "store" / "main.bin").read_bytes()   # saved, encrypted


if __name__ == "__main__":
    run_main(globals())
