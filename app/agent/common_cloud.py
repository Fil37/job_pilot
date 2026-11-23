"""Your data online without changing the code: SNAPSHOT_SYNC (deployment on Streamlit, 2026-11-23).

job_pilot keeps SQLite and files in its instance folder. Online (Streamlit Community Cloud, whose disk is wiped at
each restart, and GitHub Actions for the daily collection and the mails), that folder is saved as ONE encrypted
snapshot in a Postgres database (Neon, free) and restored before use:

  pull()   restores the latest snapshot if it is newer than the local copy
  push()   saves the local folder; refused (SnapshotConflict) if someone else saved in between (compare-and-swap)
  lease()  a short lock while a chain runs (GitHub Actions or a CV preparation): the interface is read-only meanwhile

The snapshot is encrypted (Fernet, key JOB_PILOT_SNAPSHOT_KEY known only by your secrets) BEFORE it leaves the
machine: Neon only stores unreadable bytes. Two stores: PostgresStore (JOB_PILOT_DATABASE_URL) and FolderStore (tests,
or your own server with a mounted disk).

GitHub Actions: python -m app.agent.agent000_job_pilot --chain daily --sync
First upload of your local data: python -m app.agent.common_cloud push
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SNAPSHOT_ID = "main"
VERSION_FILE = ".snapshot_version"
SKIP = {"backups", ".snapshot_version", "job_pilot.db-wal", "job_pilot.db-shm", "nightly.lock"}


class SnapshotConflict(RuntimeError):
    """Someone else (GitHub Actions, another tab) saved a newer snapshot: reload, then redo the action."""


class LeaseBusy(RuntimeError):
    """A chain is running elsewhere (lease held): read-only until it ends."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# =========================================================================== encryption and archive
def fernet(key: str | None = None):
    from cryptography.fernet import Fernet
    key = key or os.environ.get("JOB_PILOT_SNAPSHOT_KEY")
    if not key:
        raise RuntimeError("JOB_PILOT_SNAPSHOT_KEY missing: your data is never stored online unencrypted")
    return Fernet(key.encode() if isinstance(key, str) else key)


def new_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def pack(folder: Path) -> bytes:
    """tar.gz of the instance folder; the SQLite base is copied with the backup API (consistent even if open)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        for p in sorted(folder.iterdir()):
            if p.name in SKIP:
                continue
            if p.suffix == ".db":
                copy = Path(tmp) / p.name
                with sqlite3.connect(p) as src, sqlite3.connect(copy) as dst:
                    src.backup(dst)
                tar.add(copy, arcname=p.name)
            else:
                tar.add(p, arcname=p.name)
    return buf.getvalue()


def unpack(data: bytes, folder: Path) -> None:
    """Restore into the instance folder; members are checked (no absolute path, no '..', no link)."""
    folder.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = []
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in Path(m.name).parts or m.issym() or m.islnk():
                raise RuntimeError(f"unsafe member in the snapshot: {m.name}")
            members.append(m)
        for name in ("job_pilot.db-wal", "job_pilot.db-shm"):
            (folder / name).unlink(missing_ok=True)
        tar.extractall(folder, members=members, filter="data")


def local_version(folder: Path) -> int:
    f = folder / VERSION_FILE
    return int(f.read_text()) if f.is_file() else 0


def _set_local_version(folder: Path, v: int) -> None:
    (folder / VERSION_FILE).write_text(str(v))


# =========================================================================== stores
class PostgresStore:
    """One row per snapshot in Postgres (Neon). CAS on 'version', lease with an expiry time."""

    def __init__(self, url: str | None = None):
        import psycopg
        self.psycopg = psycopg
        self.url = url or os.environ["JOB_PILOT_DATABASE_URL"]
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS job_pilot_snapshot (id TEXT PRIMARY KEY, version BIGINT NOT NULL, "
                      "data BYTEA NOT NULL, updated_at TIMESTAMPTZ NOT NULL, lease_owner TEXT, lease_until TIMESTAMPTZ)")

    def _conn(self):
        return self.psycopg.connect(self.url, autocommit=True)

    def get(self, sid=SNAPSHOT_ID):
        with self._conn() as c:
            row = c.execute("SELECT version, data FROM job_pilot_snapshot WHERE id=%s", (sid,)).fetchone()
        return (row[0], bytes(row[1])) if row else (0, None)

    def version(self, sid=SNAPSHOT_ID) -> int:
        with self._conn() as c:
            row = c.execute("SELECT version FROM job_pilot_snapshot WHERE id=%s", (sid,)).fetchone()
        return row[0] if row else 0

    def cas(self, base: int, data: bytes, sid=SNAPSHOT_ID) -> int:
        with self._conn() as c:
            if base == 0:
                # first save; the row may already exist with version 0 if a lease was taken before any save
                cur = c.execute("INSERT INTO job_pilot_snapshot (id, version, data, updated_at) VALUES (%s, 1, %s, now()) "
                                "ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, version=1, updated_at=now() "
                                "WHERE job_pilot_snapshot.version=0", (sid, data))
            else:
                cur = c.execute("UPDATE job_pilot_snapshot SET data=%s, version=version+1, updated_at=now() "
                                "WHERE id=%s AND version=%s", (data, sid, base))
            if cur.rowcount != 1:
                raise SnapshotConflict("a newer snapshot was saved in between")
        return base + 1

    def lease(self, owner: str, seconds: int, sid=SNAPSHOT_ID) -> bool:
        with self._conn() as c:
            c.execute("INSERT INTO job_pilot_snapshot (id, version, data, updated_at) VALUES (%s, 0, ''::bytea, now()) "
                      "ON CONFLICT (id) DO NOTHING", (sid,))
            cur = c.execute("UPDATE job_pilot_snapshot SET lease_owner=%s, lease_until=now() + %s * interval '1 second' "
                            "WHERE id=%s AND (lease_until IS NULL OR lease_until < now() OR lease_owner=%s)",
                            (owner, seconds, sid, owner))
            return cur.rowcount == 1

    def release(self, owner: str, sid=SNAPSHOT_ID) -> None:
        with self._conn() as c:
            c.execute("UPDATE job_pilot_snapshot SET lease_owner=NULL, lease_until=NULL WHERE id=%s AND lease_owner=%s",
                      (sid, owner))

    def lease_holder(self, sid=SNAPSHOT_ID) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT lease_owner FROM job_pilot_snapshot WHERE id=%s AND lease_until > now()",
                            (sid,)).fetchone()
        return row[0] if row else None


class FolderStore:
    """The same contract on a folder (tests, or your own server with a persistent disk). A lock file makes the
    compare-and-swap and the lease atomic between processes."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _locked(self):
        import fcntl
        f = open(self.folder / ".lock", "w")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def _meta(self, sid) -> dict:
        p = self.folder / f"{sid}.json"
        return json.loads(p.read_text()) if p.is_file() else {"version": 0, "lease_owner": None, "lease_until": None}

    def _save_meta(self, sid, meta) -> None:
        (self.folder / f"{sid}.json").write_text(json.dumps(meta))

    def get(self, sid=SNAPSHOT_ID):
        meta = self._meta(sid)
        p = self.folder / f"{sid}.bin"
        return (meta["version"], p.read_bytes()) if meta["version"] and p.is_file() else (0, None)

    def version(self, sid=SNAPSHOT_ID) -> int:
        return self._meta(sid)["version"]

    def cas(self, base: int, data: bytes, sid=SNAPSHOT_ID) -> int:
        with self._locked():
            meta = self._meta(sid)
            if meta["version"] != base:
                raise SnapshotConflict("a newer snapshot was saved in between")
            (self.folder / f"{sid}.bin").write_bytes(data)
            meta["version"] = base + 1
            self._save_meta(sid, meta)
        return base + 1

    def lease(self, owner: str, seconds: int, sid=SNAPSHOT_ID) -> bool:
        with self._locked():
            meta = self._meta(sid)
            until = meta.get("lease_until")
            free = not until or datetime.fromisoformat(until) < _now() or meta.get("lease_owner") == owner
            if free:
                meta.update(lease_owner=owner, lease_until=(_now() + timedelta(seconds=seconds)).isoformat())
                self._save_meta(sid, meta)
            return free

    def release(self, owner: str, sid=SNAPSHOT_ID) -> None:
        with self._locked():
            meta = self._meta(sid)
            if meta.get("lease_owner") == owner:
                meta.update(lease_owner=None, lease_until=None)
                self._save_meta(sid, meta)

    def lease_holder(self, sid=SNAPSHOT_ID) -> str | None:
        meta = self._meta(sid)
        until = meta.get("lease_until")
        return meta.get("lease_owner") if until and datetime.fromisoformat(until) > _now() else None


def store_from_env():
    """PostgresStore when JOB_PILOT_DATABASE_URL is set, FolderStore when JOB_PILOT_SNAPSHOT_DIR is set, else None
    (local use: nothing to synchronise)."""
    if os.environ.get("JOB_PILOT_DATABASE_URL"):
        return PostgresStore()
    if os.environ.get("JOB_PILOT_SNAPSHOT_DIR"):
        return FolderStore(Path(os.environ["JOB_PILOT_SNAPSHOT_DIR"]))
    return None


# =========================================================================== sync
def pull(store, folder: Path, key: str | None = None) -> bool:
    """Restore the latest snapshot if it is newer than the local copy. True when something was restored."""
    remote, data = store.get()
    if not data or remote <= local_version(folder):
        return False
    unpack(fernet(key).decrypt(data), folder)
    _set_local_version(folder, remote)
    return True


def push(store, folder: Path, key: str | None = None) -> int:
    """Save the local folder on top of the version it was restored from (SnapshotConflict otherwise)."""
    v = store.cas(local_version(folder), fernet(key).encrypt(pack(folder)))
    _set_local_version(folder, v)
    return v


def run_synced(run, *, owner: str, store, folder: Path, lease_s: int = 3600) -> int:
    """GitHub Actions: lease, restore, run (a callable returning a run report), save, release. Exit code."""
    if not store.lease(owner, lease_s):
        print(f"busy: {store.lease_holder()} holds the lease; this run is skipped")
        return 0
    try:
        pull(store, folder)
        report = run()
        print(report["status"], report["run_id"])
        for attempt in range(3):
            try:
                push(store, folder)
                break
            except SnapshotConflict:            # never expected while we hold the lease; retry after a short wait
                time.sleep(2 + attempt)
        return 0 if report["status"] in ("succeeded", "partial") else 1
    finally:
        store.release(owner)


TECTONIC = {"version": "0.17.0",
            "url": "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.17.0/"
                   "tectonic-0.17.0-x86_64-unknown-linux-musl.tar.gz",
            "sha256": "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7"}


def ensure_tectonic(dest: Path | None = None) -> Path:
    """Online (Streamlit): Tectonic is downloaded once from its GitHub release and used only if its SHA-256 is the
    expected one (TECTONIC_CHECKSUM). Returns the binary path."""
    import hashlib
    import shutil
    import stat
    import urllib.request
    dest = dest or Path.home() / ".local" / "bin" / "tectonic"
    if dest.is_file() or shutil.which("tectonic"):
        return dest if dest.is_file() else Path(shutil.which("tectonic"))
    with urllib.request.urlopen(TECTONIC["url"], timeout=120) as r:  # noqa: S310 - fixed https URL
        data = r.read()
    if hashlib.sha256(data).hexdigest() != TECTONIC["sha256"]:
        raise RuntimeError("Tectonic download refused: unexpected checksum")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        binary = tar.extractfile("tectonic").read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(binary)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="job_pilot snapshot of the instance folder (online deployment)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("push", help="save the local instance folder as the first snapshot (or on top of the latest)")
    sub.add_parser("pull", help="restore the latest snapshot into the instance folder")
    sub.add_parser("new-key", help="print a new encryption key for JOB_PILOT_SNAPSHOT_KEY")
    args = ap.parse_args(argv)
    if args.cmd == "new-key":
        print(new_key())
        return 0
    from app.agent import INSTANCE_DIR
    store = store_from_env()
    if store is None:
        print("JOB_PILOT_DATABASE_URL (or JOB_PILOT_SNAPSHOT_DIR) missing")
        return 2
    if args.cmd == "push":
        if local_version(INSTANCE_DIR) == 0 and store.version():
            print("a snapshot already exists online: pull first")
            return 1
        print("saved version", push(store, INSTANCE_DIR))
        return 0
    print("restored" if pull(store, INSTANCE_DIR) else "already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
