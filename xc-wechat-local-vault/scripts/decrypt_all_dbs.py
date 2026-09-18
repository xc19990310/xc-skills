#!/usr/bin/env python3
"""
Decrypt all WeChat Mac 4.x SQLCipher databases that have captured keys.

Input:
  ~/.config/wechat-local-vault.json
  ~/.config/wechat-keys.json

Output:
  ~/Library/Application Support/wechat-local-vault/decrypted/current/<relative db path>
  ~/Library/Application Support/wechat-local-vault/manifests/decrypt-*.json
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
import fcntl
from private_io import atomic_json, private_dir
from encrypted_snapshot import decrypt_db, source_fingerprint, validate_plaintext, SnapshotError
from pathlib import Path



CONFIG_FILE = Path("~/.config/wechat-local-vault.json").expanduser()
KEYS_FILE = Path("~/.config/wechat-keys.json").expanduser()
DEFAULT_VAULT_DIR = Path("~/Library/Application Support/wechat-local-vault").expanduser()
DEFAULT_OUTPUT_DIR = DEFAULT_VAULT_DIR / "decrypted/current"
DECRYPT_STATE_FILE = DEFAULT_VAULT_DIR / "state/decrypt_state.json"

ALIAS_TO_REL = {
    "message_0": "message/message_0.db",
    "message_1": "message/message_1.db",
    "message_2": "message/message_2.db",
    "message_3": "message/message_3.db",
    "message_fts": "message/message_fts.db",
    "message_resource": "message/message_resource.db",
    "contact": "contact/contact.db",
    "session": "session/session.db",
    "sns": "sns/sns.db",
    "favorite": "favorite/favorite.db",
}


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)


def load_config() -> dict:
    return load_json(CONFIG_FILE)


def save_json(path: Path, data: dict) -> None:
    atomic_json(path, data)


def ensure_private_dir(path: Path) -> None:
    private_dir(path)


def resolve_db_base() -> Path:
    config = load_config()
    if config.get("db_base_path"):
        return Path(config["db_base_path"]).expanduser()
    wxid = config.get("wxid")
    if wxid:
        return Path(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        ).expanduser() / wxid / "db_storage"
    raise SystemExit(f"Missing db_base_path/wxid in {CONFIG_FILE}")


def key_name_to_rel(name: str) -> str | None:
    if name.startswith("__"):
        return None
    if name in ALIAS_TO_REL:
        return ALIAS_TO_REL[name]
    if name.endswith(".db") and "/" in name and not Path(name).is_absolute() and ".." not in Path(name).parts:
        return name
    return None


def sqlite_table_count(path: Path) -> int:
    return validate_plaintext(path)


def write_manifest(out_base: Path, records: list[dict]) -> Path:
    manifest_dir = DEFAULT_VAULT_DIR / "manifests"
    ensure_private_dir(manifest_dir)
    manifest_path = manifest_dir / f"decrypt-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "decrypted_dir": str(out_base),
        "database_count": sum(1 for item in records if item.get("status") in ("ok", "unchanged")),
        "complete": bool(records) and all(item.get("status") in ("ok", "unchanged") for item in records),
        "scope": "databases with captured keys; not a guarantee of all account data",
        "records": records,
        "privacy": {
            "contains_plaintext_wechat_data": True,
            "do_not_sync_or_share": True,
            "keys_file": str(KEYS_FILE),
        },
    }
    save_json(manifest_path, manifest)
    save_json(out_base / "refresh_status.json", manifest)
    return manifest_path


def update_config_paths(out_base: Path) -> None:
    config = load_config()
    config["vault_dir"] = str(DEFAULT_VAULT_DIR)
    config["decrypted_dir"] = str(out_base)
    config.setdefault("exports_dir", str(DEFAULT_VAULT_DIR / "exports"))
    save_json(CONFIG_FILE, config)


def unchanged(src: Path, dst: Path, key_hex: str, state: dict, rel: str) -> bool:
    if not dst.exists():
        return False
    previous = state.get(rel)
    if not previous:
        return False
    return (previous.get("source") == source_fingerprint(src, key_hex)
            and previous.get("output_sha256") == hashlib.sha256(dst.read_bytes()).hexdigest())


def main() -> int:
    parser = argparse.ArgumentParser(description="Decrypt all known WeChat DB keys.")
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help="output directory; default is the private local wechat-local-vault vault",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="disabled: preserve previous snapshots",
    )
    parser.add_argument(
        "--mode",
        choices=["full", "incremental"],
        default="full",
        help="full decrypts every keyed DB; incremental skips unchanged sources",
    )
    parser.add_argument(
        "--no-manifest",
        action="store_true",
        help="disabled: freshness manifest is mandatory",
    )
    args = parser.parse_args()

    db_base = resolve_db_base()
    keys = load_json(KEYS_FILE)
    out_base = Path(args.output).expanduser().absolute()
    if (out_base.resolve() == db_base.resolve() or db_base.resolve() in out_base.resolve().parents
            or out_base.resolve() in db_base.resolve().parents):
        raise SystemExit("Output must be separate from the live source database directory")
    if args.clean:
        raise SystemExit("--clean is disabled: previous good snapshots must be retained")
    if args.no_manifest:
        raise SystemExit("--no-manifest is disabled: freshness status is required")
    ensure_private_dir(out_base)
    # Hold one private lock for this refresh, including status/state publication.
    ensure_private_dir(DECRYPT_STATE_FILE.parent)
    lock_fd = os.open(DECRYPT_STATE_FILE.parent / "refresh.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise SystemExit("Another refresh is running")
    state = load_json(DECRYPT_STATE_FILE)
    save_json(out_base / "refresh_status.json", {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "complete": False, "status": "refreshing", "records": [],
    })

    passed = 0
    failed = 0
    skipped = 0
    records: list[dict] = []

    print("DB base: [private account directory]")
    print(f"Output:  {out_base}")

    for name, key_hex in sorted(keys.items()):
        rel = key_name_to_rel(name)
        if not rel:
            skipped += 1
            continue
        src = db_base / rel
        dst = out_base / rel
        if not src.exists():
            print(f"SKIP {name}: source not found ({rel})")
            skipped += 1
            records.append({"name": name, "rel": rel, "status": "skip", "reason": "source not found"})
            continue
        try:
            if args.mode == "incremental" and unchanged(src, dst, key_hex, state, rel):
                print(f"SKIP {name:24s}: unchanged (DB + WAL verified)")
                skipped += 1
                records.append({"name": name, "rel": rel, "status": "unchanged",
                                "snapshot_at": state[rel].get("snapshot_at")})
                continue
            fingerprint = decrypt_db(src, dst, key_hex)
            size = dst.stat().st_size
            snapshot_at = datetime.now().isoformat(timespec="seconds")
            state[rel] = {"source": fingerprint, "output_sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
                          "snapshot_at": snapshot_at}
            print(f"OK   {name:24s}: verified snapshot")
            passed += 1
            records.append({
                "name": name,
                "rel": rel,
                "status": "ok",
                "snapshot_at": snapshot_at,
                "wal_included": bool(fingerprint.get("-wal") and fingerprint["-wal"]["size"]),
                "bytes": size,
            })
        except Exception as exc:
            reason = str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__
            print(f"MISS {name:24s}: {reason}; previous snapshot retained")
            failed += 1
            records.append({"name": name, "rel": rel, "status": "miss", "reason": reason, "previous_retained": dst.exists(),
                            "snapshot_at": state.get(rel, {}).get("snapshot_at")})

    known = {r["rel"] for r in records}
    for source in sorted(db_base.rglob("*.db")):
        rel = str(source.relative_to(db_base))
        if rel not in known:
            records.append({"rel": rel, "status": "missing_key", "previous_retained": (out_base / rel).exists()})
            failed += 1
    known = {r["rel"] for r in records}
    for retained in sorted(out_base.rglob("*.db")):
        rel = str(retained.relative_to(out_base))
        if rel not in known:
            records.append({"rel": rel, "status": "retained_orphan", "previous_retained": True,
                            "snapshot_at": state.get(rel, {}).get("snapshot_at")})
            failed += 1
    update_config_paths(out_base)
    save_json(DECRYPT_STATE_FILE, state)
    if not args.no_manifest:
        manifest_path = write_manifest(out_base, records)
        print(f"Manifest: {manifest_path}")
    print(f"Done: {passed} decrypted, {failed} failed, {skipped} skipped")
    os.close(lock_fd)
    return 2 if failed or not records or any(r["status"] == "skip" for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
