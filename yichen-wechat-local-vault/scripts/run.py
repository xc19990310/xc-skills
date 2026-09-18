#!/usr/bin/env python3
"""Installed entrypoint. Doctor never enumerates or reads WeChat account data."""
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


def doctor():
    from encrypted_snapshot import sqlcipher_binary
    app = Path("/Applications/WeChat.app/Contents/Info.plist")
    version = None
    if app.is_file():
        version = plistlib.loads(app.read_bytes()).get("CFBundleShortVersionString")
    deps = {name: importlib.util.find_spec(name) is not None for name in ("Crypto", "zstandard", "frida")}
    try:
        cipher = sqlcipher_binary()
        result = subprocess.run([cipher, "-batch", "-init", "/dev/null", ":memory:"],
                                input="PRAGMA cipher_version;\n", capture_output=True, text=True, timeout=10)
        cipher_version = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        cipher_version = None
    base = Path.home() / "Library/Application Support/wechat-local-vault"
    data = {
        "python": sys.version.split()[0], "dependencies": deps,
        "sqlcipher_version": cipher_version, "wechat_version": version,
        "config_present": (Path.home() / ".config/wechat-local-vault.json").is_file(),
        "keys_present": (Path.home() / ".config/wechat-keys.json").is_file(),
        "snapshot_present": (base / "decrypted/current/refresh_status.json").is_file(),
        "account_data_read": False,
    }
    data["installation_ready"] = all(deps.values()) and bool(cipher_version)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data["installation_ready"] else 2


def main():
    os.umask(0o077)
    arguments = sys.argv[1:]
    if arguments == ["doctor"]:
        return doctor()
    routes = {"extract": "extract_keys.py", "refresh": "decrypt_all_dbs.py", "export-chat": "export_chat.py"}
    if arguments and arguments[0] in routes:
        script = routes[arguments.pop(0)]
    else:
        script = "vault_cli.py"
    target = Path(__file__).resolve().parent / script
    os.execv(sys.executable, [sys.executable, str(target), *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
