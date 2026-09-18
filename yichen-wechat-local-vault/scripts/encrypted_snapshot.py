"""Recover encrypted DB + WAL only in a private copy, then export atomically.

SQLCipher 4 owns WAL recovery and HMAC validation. Never open the live WeChat
database with SQLite/SQLCipher or checkpoint it. Busy or incompatible inputs
fail without replacing the previous readable database.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

from private_io import private_dir


class SnapshotError(RuntimeError):
    pass


def source_fingerprint(src, key_hex):
    result = {"engine": "sqlcipher4-physical-wal-v3", "key_sha256": hashlib.sha256(key_hex.encode()).hexdigest()}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(str(src) + suffix)
        try:
            before = path.stat()
            if path.is_symlink():
                raise SnapshotError("Symlink database/sidecar refused")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            after = path.stat()
        except FileNotFoundError:
            if suffix == "":
                raise SnapshotError("Source database missing") from None
            result[suffix] = None
            continue
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
            raise SnapshotError("Source changed while reading; retry refresh")
        result[suffix] = {"size": after.st_size, "sha256": digest.hexdigest()}
    if result["-journal"] and result["-journal"]["size"]:
        raise SnapshotError("Rollback journal present; retry after a clean WeChat exit")
    return result


def sqlcipher_binary():
    configured = os.environ.get("WECHAT_VAULT_SQLCIPHER")
    binary = configured or shutil.which("sqlcipher")
    if not binary:
        candidate = Path("/opt/homebrew/bin/sqlcipher")
        if candidate.is_file():
            binary = str(candidate)
    if not binary:
        raise SnapshotError("SQLCipher is required; no main-file-only fallback is allowed")
    return binary


def empty_wal_index(shm, wal_header):
    """Corroborate an empty recycled WAL with both native wal-index headers.

    Only the narrow mxFrame=nBackfill=0 case is accepted; nonempty/missing or
    inconsistent indexes never authorize discarding mismatched active frames.
    https://www.sqlite.org/walformat.html#the_wal_index_header
    """
    if not shm.exists():
        return False
    data = shm.read_bytes()
    if len(data) < 136 or data[:48] != data[48:96]:
        return False
    order = "<" if sys.byteorder == "little" else ">"
    version = struct.unpack(order + "I", data[:4])[0]
    page_size = struct.unpack(order + "H", data[14:16])[0]
    frames = struct.unpack(order + "I", data[16:20])[0]
    backfill = struct.unpack(order + "I", data[96:100])[0]
    attempted = struct.unpack(order + "I", data[128:132])[0]
    magic = struct.unpack(">I", wal_header[:4])[0]
    if (version != 3007000 or data[12] != 1 or page_size != 4096 or frames or backfill or attempted
            or data[13] != (magic & 1) or data[32:40] != wal_header[16:24]):
        return False
    words = struct.unpack(order + "10I", data[:40])
    a = b = 0
    for index in range(0, 10, 2):
        a = (a + words[index] + b) & 0xFFFFFFFF
        b = (b + words[index + 1] + a) & 0xFFFFFFFF
    return (a, b) == struct.unpack(order + "2I", data[40:48])


def validate_wal(path):
    """Reject damaged active WAL frames rather than silently losing a commit.

    Complete uncommitted frames are left for SQLite to ignore. A torn tail or
    salt mismatch is rejected except for a corroborated empty wal-index reset.
    The copied index must pass duplicate header, checksum and zero-frame checks.
    Nonempty recycled tails remain conservatively rejected.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb") as handle:
        header = handle.read(32)
        if len(header) != 32:
            raise SnapshotError("Incomplete WAL header; retry refresh")
        magic, version, page_size, _, salt1, salt2, check1, check2 = struct.unpack(">8I", header)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != 4096:
            raise SnapshotError("Unsupported WAL format")
        endian = "<" if magic == 0x377F0682 else ">"

        def checksum(data, pair):
            a, b = pair
            words = struct.unpack(endian + str(len(data) // 4) + "I", data)
            for i in range(0, len(words), 2):
                a = (a + words[i] + b) & 0xFFFFFFFF
                b = (b + words[i + 1] + a) & 0xFFFFFFFF
            return a, b

        running = checksum(header[:24], (0, 0))
        if running != (check1, check2):
            raise SnapshotError("WAL header checksum failed")
        frame_number = 0
        while frame := handle.read(24 + page_size):
            frame_number += 1
            if len(frame) < 24:
                raise SnapshotError("Incomplete WAL frame; retry refresh")
            page, commit, s1, s2, c1, c2 = struct.unpack(">6I", frame[:24])
            if (s1, s2) != (salt1, salt2):
                if frame_number == 1 and empty_wal_index(Path(str(path)[:-4] + "-shm"), header):
                    return
                raise SnapshotError("WAL salt mismatch (damaged or recycled tail); retry after a clean WeChat exit")
            if len(frame) != 24 + page_size or page == 0:
                raise SnapshotError("Incomplete/invalid active WAL frame")
            running = checksum(frame[:8] + frame[24:], running)
            if running != (c1, c2):
                raise SnapshotError("Active WAL frame checksum failed; previous output retained")


class ReadOnlyConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def read_only(path):
    path = Path(path).resolve()
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise SnapshotError("Plaintext sidecar present; use a verified standalone snapshot")
    con = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, factory=ReadOnlyConnection)
    con.execute("PRAGMA query_only=ON")
    return con


def validate_plaintext(path):
    with read_only(path) as con:
        if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SnapshotError("Plaintext integrity check failed")
        return con.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]


def decrypt_verified_copy(source, output, key_hex):
    """Physical page copy preserves hidden rowids (Name2Id sender identities).

    Only called after SQLCipher recovered/checkpointed this disposable copy and
    verified its page HMACs. No logical export, which renumbers implicit rowids.
    """
    from Crypto.Cipher import AES
    page_size, reserve = 4096, 80
    key = bytes.fromhex(key_hex)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
        number = 0
        while page := inp.read(page_size):
            if len(page) != page_size:
                raise SnapshotError("Encrypted database ends in a partial page")
            start = 16 if number == 0 else 0
            iv = page[page_size - reserve:page_size - reserve + 16]
            clear = AES.new(key, AES.MODE_CBC, iv).decrypt(page[start:page_size - reserve])
            result = bytearray(page_size)
            if number == 0:
                result[:16] = b"SQLite format 3\x00"
            result[start:page_size - reserve] = clear
            if number == 0:
                if result[16:18] != b"\x10\x00" or result[20] != reserve:
                    raise SnapshotError("Unsupported decrypted page layout")
                # Standalone validated plaintext has no sidecars.
                result[18:20] = b"\x01\x01"
            out.write(result)
            number += 1
        if not number:
            raise SnapshotError("Encrypted database is empty")


def decrypt_db(src, dst, key_hex):
    src, dst = Path(src).resolve(), Path(dst).absolute()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
        raise SnapshotError("Expected a 32-byte hexadecimal raw key")
    if dst.is_symlink() or src == dst.resolve():
        raise SnapshotError("Unsafe output destination")
    binary = sqlcipher_binary()
    private_dir(dst.parent)
    before = source_fingerprint(src, key_hex)
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=dst.parent) as folder:
        copied = Path(folder) / "source.db"
        output = Path(folder) / "plaintext.db"
        for suffix in ("", "-wal", "-shm"):
            if before[suffix] is not None:
                # Explicit mode before copying any encrypted bytes.
                target = Path(str(copied) + suffix)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out, Path(str(src) + suffix).open("rb") as inp:
                    shutil.copyfileobj(inp, out)
        if source_fingerprint(copied, key_hex) != before or source_fingerprint(src, key_hex) != before:
            raise SnapshotError("Source changed during snapshot; previous output retained")
        validate_wal(Path(str(copied) + "-wal"))
        # This is a disposable copied index. SQLCipher must rebuild its own.
        Path(str(copied) + "-shm").unlink(missing_ok=True)
        # Keys travel over stdin, never argv, environment or a SQL script file.
        sql = (
            f"PRAGMA key = \"x'{key_hex}'\";\n"
            "PRAGMA cipher_compatibility=4;\n"
            "PRAGMA cipher_memory_security=ON;\n"
            "PRAGMA wal_checkpoint(TRUNCATE);\n"
            "SELECT '__CHECK_BEGIN__';\nPRAGMA integrity_check;\nSELECT '__CHECK_END__';\n"
            "SELECT '__HMAC_BEGIN__';\nPRAGMA cipher_integrity_check;\nSELECT '__HMAC_END__';\n"
        )
        try:
            proc = subprocess.run([binary, "-batch", "-bail", "-init", "/dev/null", str(copied)],
                                  input=sql, capture_output=True, text=True, timeout=300, umask=0o077)
        except subprocess.TimeoutExpired:
            raise SnapshotError("SQLCipher timed out; previous output retained") from None
        # SQL errors can contain secret SQL text. Never propagate stdout/stderr.
        checked = proc.stdout.partition("__CHECK_BEGIN__\n")[2].partition("__CHECK_END__")[0].strip()
        hmac_check = proc.stdout.partition("__HMAC_BEGIN__\n")[2].partition("__HMAC_END__")[0].strip()
        if proc.returncode or checked != "ok" or "__HMAC_END__" not in proc.stdout or hmac_check:
            raise SnapshotError("SQLCipher recovery/validation failed (wrong key, damaged data or unsupported schema); previous output retained")
        remaining_wal = Path(str(copied) + "-wal")
        if remaining_wal.exists() and remaining_wal.stat().st_size:
            raise SnapshotError("Private copy WAL was not checkpointed")
        decrypt_verified_copy(copied, output, key_hex)
        validate_plaintext(output)
        if source_fingerprint(src, key_hex) != before:
            raise SnapshotError("Source changed during export; retry refresh")
        with output.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(output, dst)
    return before
