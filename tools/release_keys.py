"""Make the PlaStim release-signing key pair (ed25519).

    py -3.13 tools/release_keys.py [--dir DIR]              make the key pair
    py -3.13 tools/release_keys.py --encrypt [--dir DIR]    protect the private key with a passphrase (you type it;
                                                            it is never shown or stored), then offer to delete the
                                                            unencrypted copy

Writes, OUTSIDE any repository (default %USERPROFILE%\\.plastim\\release-signing\\):
    plastim-release.key   the PRIVATE key (hex, with a warning header). Whoever has it can sign firmware that every
                          copy of the app will flash. Keep it offline (a USB stick in a drawer, a password manager),
                          back it up, never commit it, never put it on a server or in CI.
    plastim-release.pub   the public key (hex). The app carries it in stimengine/app/updates.py TRUSTED_KEYS.
    plastim-release.key.enc   after --encrypt: the private key as passphrase-encrypted PKCS#8 PEM. Safe to keep
                          copies of (a USB stick, a backup drive); signing asks for the passphrase.

Refuses to overwrite an existing key: rotating keys means adding the new public key to TRUSTED_KEYS in an app
release first, and only then signing with the new private key.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_DIR = Path(os.environ.get("USERPROFILE") or Path.home()) / ".plastim" / "release-signing"
PRIVATE_NAME = "plastim-release.key"
PUBLIC_NAME = "plastim-release.pub"
ENC_NAME = "plastim-release.key.enc"
MIN_PASSPHRASE = 12

HEADER = """# PlaStim release-signing PRIVATE key (ed25519, raw 32 bytes as hex on the last line).
# Whoever has this file can sign firmware that every copy of the PlaStim app will accept and flash.
# Keep it OFFLINE and backed up. Never commit it, never copy it to a server, CI or a shared folder.
"""


def generate(folder: Path) -> tuple[Path, str]:
    folder.mkdir(parents=True, exist_ok=True)
    priv_path, pub_path = folder / PRIVATE_NAME, folder / PUBLIC_NAME
    if priv_path.exists() or pub_path.exists():
        raise FileExistsError(f"a key already exists in {folder}: not overwriting it")
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    priv_path.write_text(HEADER + raw.hex() + "\n", encoding="ascii")
    pub_path.write_text(pub + "\n", encoding="ascii")
    return priv_path, pub


def default_key(folder: Path = DEFAULT_DIR) -> Path:
    """The encrypted key when there is one, else the plain one."""
    return folder / ENC_NAME if (folder / ENC_NAME).exists() else folder / PRIVATE_NAME


def load_private(path: Path, passphrase: bytes | None = None) -> Ed25519PrivateKey:
    data = path.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN ENCRYPTED PRIVATE KEY"):
        if passphrase is None:
            passphrase = getpass.getpass(f"passphrase for {path.name}: ").encode("utf-8")
        key = serialization.load_pem_private_key(data, password=passphrase)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path}: not an ed25519 key")
        return key
    lines = [ln.strip() for ln in data.decode("ascii").splitlines() if ln.strip() and not ln.startswith("#")]
    if len(lines) != 1 or len(lines[0]) != 64:
        raise ValueError(f"{path}: not a PlaStim release key")
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(lines[0]))


def _pub_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def encrypt(folder: Path, passphrase: bytes) -> Path:
    """Write the passphrase-encrypted copy of the private key and prove it opens to the same key."""
    plain, enc = folder / PRIVATE_NAME, folder / ENC_NAME
    if enc.exists():
        raise FileExistsError(f"{enc} already exists: not overwriting it")
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"use a passphrase of at least {MIN_PASSPHRASE} characters")
    key = load_private(plain)
    enc.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.BestAvailableEncryption(passphrase)))
    if _pub_hex(load_private(enc, passphrase)) != _pub_hex(key):
        enc.unlink()
        raise RuntimeError("the encrypted copy did not open to the same key; nothing changed")
    return enc


def _encrypt_interactive(folder: Path) -> int:
    first = getpass.getpass(f"new passphrase (at least {MIN_PASSPHRASE} characters, not shown): ")
    if getpass.getpass("same passphrase again: ") != first:
        print("the two did not match; nothing changed", file=sys.stderr)
        return 1
    try:
        enc = encrypt(folder, first.encode("utf-8"))
    except (FileExistsError, ValueError, RuntimeError, OSError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"encrypted key written and checked: {enc}")
    print("Keep copies of it (a USB stick, a backup drive) and remember the passphrase: without it the key is lost.")
    if input(f"delete the UNENCRYPTED key {folder / PRIVATE_NAME} now? [y/N] ").strip().lower() == "y":
        (folder / PRIVATE_NAME).unlink()
        print("deleted the unencrypted key")
    else:
        print("kept the unencrypted key; delete it once the encrypted copy is backed up")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--encrypt", action="store_true", help="protect the existing private key with a passphrase")
    args = ap.parse_args(argv)
    if args.encrypt:
        return _encrypt_interactive(args.dir)
    try:
        priv, pub = generate(args.dir)
    except FileExistsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"private key: {priv}  (move it offline and back it up)")
    print(f"public key:  {pub}")
    print("add the public key to TRUSTED_KEYS in stimengine/app/updates.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
