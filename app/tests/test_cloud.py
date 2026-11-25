"""SNAPSHOT_SYNC (deployment on Streamlit, 2026-11-23): the instance folder saved as one encrypted snapshot,
restored before use; a save on top of an older version is refused; a lease makes a running chain exclusive."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import run_main  # noqa: E402

from app.agent import common_cloud as cloud  # noqa: E402

KEY = cloud.new_key()


def instance(tmp: Path) -> Path:
    folder = tmp / "instance"
    (folder / "runs" / "r1").mkdir(parents=True)
    with sqlite3.connect(folder / "job_pilot.db") as c:
        c.execute("CREATE TABLE t (x TEXT)")
        c.execute("INSERT INTO t VALUES ('mon offre')")
    (folder / "profile.yaml").write_text("identity: {full_name: Prénom Nom}\n")
    (folder / "runs" / "r1" / "cv.pdf").write_bytes(b"%PDF fake")
    (folder / "backups").mkdir()
    (folder / "backups" / "old.db").write_bytes(b"x" * 100)
    return folder


def test_snapshot_is_encrypted_restored_and_skips_local_backups():
    tmp = Path(tempfile.mkdtemp())
    store = cloud.FolderStore(tmp / "store")
    src = instance(tmp)
    assert cloud.push(store, src, KEY) == 1
    raw = (tmp / "store" / "main.bin").read_bytes()
    assert b"mon offre" not in raw and b"Pr\xc3\xa9nom" not in raw          # Neon only sees unreadable bytes
    dst = tmp / "elsewhere"
    assert cloud.pull(store, dst, KEY) is True and cloud.pull(store, dst, KEY) is False   # second time: up to date
    assert sqlite3.connect(dst / "job_pilot.db").execute("SELECT x FROM t").fetchone()[0] == "mon offre"
    assert (dst / "runs" / "r1" / "cv.pdf").read_bytes() == b"%PDF fake" and not (dst / "backups").exists()
    try:
        cloud.pull(store, tmp / "x", cloud.new_key())
        raise AssertionError("decrypted with another key")
    except Exception as exc:  # noqa: BLE001 - cryptography.fernet.InvalidToken
        assert "InvalidToken" in type(exc).__name__


def test_a_save_on_top_of_an_older_version_is_refused():
    """Streamlit and GitHub Actions both saved: the second one must reload instead of erasing the first."""
    tmp = Path(tempfile.mkdtemp())
    store = cloud.FolderStore(tmp / "store")
    a = instance(tmp)
    cloud.push(store, a, KEY)
    b = tmp / "b"
    cloud.pull(store, b, KEY)
    cloud.push(store, b, KEY)                   # b saves version 2
    try:
        cloud.push(store, a, KEY)               # a still on version 1
        raise AssertionError("an older copy overwrote a newer one")
    except cloud.SnapshotConflict:
        pass
    assert cloud.pull(store, a, KEY) and cloud.push(store, a, KEY) == 3


def test_lease_makes_a_chain_exclusive_and_run_synced_saves_its_work():
    tmp = Path(tempfile.mkdtemp())
    store = cloud.FolderStore(tmp / "store")
    folder = instance(tmp)
    cloud.push(store, folder, KEY)
    assert store.lease("streamlit", 60) and not store.lease("actions-1", 60)
    assert store.lease_holder() == "streamlit"
    import os
    os.environ["JOB_PILOT_SNAPSHOT_KEY"] = KEY
    try:
        ran = []
        assert cloud.run_synced(lambda: ran.append(1) or {"status": "succeeded", "run_id": "r"}, owner="actions-1",
                                store=store, folder=folder) == 0
        assert ran == []                        # busy: skipped, not failed
        store.release("streamlit")

        def run():
            with sqlite3.connect(folder / "job_pilot.db") as c:
                c.execute("INSERT INTO t VALUES ('collectée par Actions')")
            return {"status": "succeeded", "run_id": "r2"}
        assert cloud.run_synced(run, owner="actions-1", store=store, folder=folder) == 0
        assert store.lease_holder() is None and store.version() == 2
        other = tmp / "other"
        cloud.pull(store, other)
        assert sqlite3.connect(other / "job_pilot.db").execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    finally:
        os.environ.pop("JOB_PILOT_SNAPSHOT_KEY", None)


def test_unsafe_archive_members_are_refused():
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    try:
        cloud.unpack(buf.getvalue(), Path(tempfile.mkdtemp()))
        raise AssertionError("path traversal accepted")
    except RuntimeError:
        pass


def test_postgres_store_same_contract():
    """The real Postgres store (Neon online): only when JOB_PILOT_TEST_PG_URL points to a throwaway database."""
    import os
    url = os.environ.get("JOB_PILOT_TEST_PG_URL")
    if not url:
        return
    tmp = Path(tempfile.mkdtemp())
    store = cloud.PostgresStore(url)
    with store._conn() as c:
        c.execute("DELETE FROM job_pilot_snapshot")
    assert store.lease("actions-1", 60) and not store.lease("streamlit", 60) and store.lease_holder() == "actions-1"
    folder = instance(tmp)
    assert cloud.push(store, folder, KEY) == 1          # first save even though a lease row already existed
    store.release("actions-1")
    assert store.lease_holder() is None and store.lease("streamlit", 60)
    b = tmp / "b"
    assert cloud.pull(store, b, KEY) and cloud.push(store, b, KEY) == 2
    try:
        cloud.push(store, folder, KEY)
        raise AssertionError("an older copy overwrote a newer one")
    except cloud.SnapshotConflict:
        pass
    assert sqlite3.connect(b / "job_pilot.db").execute("SELECT x FROM t").fetchone()[0] == "mon offre"


def test_stored_files_follow_the_instance_and_stay_confined():
    """Real case 2026-11-25 (FILES_FOLLOW_THE_INSTANCE): online, every project said "fichier du brief absent" because the
    stored path was written by GitHub Actions (/home/runner/...) or on the PC. It is found in THIS runs folder; a path
    that would leave the folder is refused."""
    from app.agent import runs_path
    tmp = Path(tempfile.mkdtemp())
    runs = tmp / "instance" / "runs"
    (runs / "20261125T145206_x").mkdir(parents=True)
    (runs / "20261125T145206_x" / "brief_o1.md").write_text("# Projet")
    stored = "/home/runner/work/_temp/instance/runs/20261125T145206_x/brief_o1.md"
    assert runs_path(stored, runs).read_text() == "# Projet"
    assert runs_path(str(runs / "20261125T145206_x" / "brief_o1.md"), runs).is_file()
    assert runs_path("/home/runner/work/_temp/instance/runs/../../../etc/passwd", runs) is None
    assert runs_path("/etc/passwd", runs) is None and runs_path("", runs) is None and runs_path("/x/runs", runs) is None


if __name__ == "__main__":
    run_main(globals())
