#!/usr/bin/env python3
"""
AetherDesk OTA Release Signing Tool
==================================
Takes a compiled Windows executable (AetherDesk.exe) and a locally provided
Ed25519 private key (stored offline outside repo), validates executable integrity,
and generates a cryptographically signed update manifest (manifest.json).

SECURITY:
- The private key MUST NEVER be committed to git, passed via CLI flags, or embedded in client binaries.
- Fail-closed: if the private key does not match the public trust anchor fixed in the app
  (OTA_VENDOR_PUBLIC_KEY_HEX), the process halts with an error WITHOUT writing or altering manifest.json.
- Final manifest verification strictly uses the public trust anchor fixed in the app.
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Optional, Tuple

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from src.ota import (
    CURRENT_VERSION,
    OTA_VENDOR_PUBLIC_KEY_HEX,
    is_valid_hex,
    parse_version,
    compute_file_sha256,
    validate_executable_file,
    build_manifest_canonical_bytes,
    sign_update_manifest,
    verify_update_manifest,
)


def load_private_key(
    key_file: str | Path,
    password: Optional[str] = None
) -> ed25519.Ed25519PrivateKey:
    """
    Load an Ed25519 private key exclusively from a local file outside the repository.
    Never accepts keys via command-line arguments to prevent exposure in process lists.
    Supports:
    1. Raw 32-byte binary file
    2. 64-character hex text file
    3. PEM encoded PKCS#8 private key (interactive hidden prompt for passphrase if encrypted)
    """
    if not key_file:
        raise ValueError("Percorso del file della chiave privata mancante. Specificare --key-file <percorso>.")

    kp = Path(key_file).resolve()
    if not kp.is_file():
        raise FileNotFoundError(f"File chiave privata non trovato: '{kp}'")

    data = kp.read_bytes()

    # 1. Raw 32-byte binary private key
    if len(data) == 32:
        return ed25519.Ed25519PrivateKey.from_private_bytes(data)

    # 2. 64-character hex text file
    text_candidate = data.decode("utf-8", errors="ignore").strip()
    if len(text_candidate) == 64 and all(c in "0123456789abcdefABCDEF" for c in text_candidate):
        try:
            return ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(text_candidate))
        except ValueError:
            pass

    # 3. PEM format (PKCS#8)
    try:
        try:
            loaded_key = serialization.load_pem_private_key(
                data,
                password=password.encode("utf-8") if password else None
            )
        except TypeError:
            # Encrypted PEM requires password
            import getpass
            pwd = getpass.getpass("Inserisci passphrase per la chiave privata PEM: ")
            loaded_key = serialization.load_pem_private_key(data, password=pwd.encode("utf-8"))

        if isinstance(loaded_key, ed25519.Ed25519PrivateKey):
            return loaded_key
        raise ValueError(f"Tipo chiave non supportato nel PEM: atteso Ed25519, trovato {type(loaded_key).__name__}.")
    except Exception as pem_ex:
        if isinstance(pem_ex, ValueError) and "Tipo chiave" in str(pem_ex):
            raise
        pass

    raise ValueError(
        f"Formato file chiave privata non riconosciuto in '{key_file}'. "
        "Attesi: file binario raw a 32 byte, file di testo con stringa esadecimale da 64 caratteri o PEM PKCS#8."
    )


def create_signed_manifest(
    binary_path: str | Path,
    version: str,
    private_key: ed25519.Ed25519PrivateKey
) -> Tuple[dict, str]:
    """
    Generate and cryptographically sign a release manifest for a compiled executable.
    Fail-closed guarantees:
    - Verifies that the derived public key matches the fixed trust anchor (OTA_VENDOR_PUBLIC_KEY_HEX).
      If there is a mismatch, raises ValueError immediately without creating or altering any manifest file.
    - Final verification strictly verifies the signature against the app's fixed public trust anchor.
    - No parameter or CLI option exists to override this anchor.
    Returns (signed_manifest_dict, verified_public_key_hex).
    """
    target_pub_hex = OTA_VENDOR_PUBLIC_KEY_HEX.strip().lower()
    if not is_valid_hex(target_pub_hex, 64):
        raise ValueError(
            f"Chiave pubblica di trust non valida: attesi esattamente 64 caratteri esadecimali, ricevuti '{target_pub_hex}'."
        )

    # 1. Derive public key from the private key and enforce fail-closed check
    derived_pub_bytes = private_key.public_key().public_bytes_raw()
    derived_pub_hex = derived_pub_bytes.hex().lower()

    if derived_pub_hex != target_pub_hex:
        raise ValueError(
            f"Rifiuto firma fail-closed: la chiave privata fornita genera la chiave pubblica '{derived_pub_hex}', "
            f"che NON corrisponde alla chiave pubblica di trust incorporata nell'applicazione ('{target_pub_hex}'). "
            f"I client installati rifiuteranno questo manifest. Operazione interrotta senza generare o modificare file."
        )

    bp = Path(binary_path).resolve()
    if not bp.is_file():
        raise FileNotFoundError(f"Binario eseguibile non trovato: '{bp}'")

    # 2. Strict SemVer validation (ASCII digits 0-9 in major.minor.patch)
    parse_version(version)

    # 3. Executable structural validation (PE MZ header, size >= 1MB)
    valid_exe, exe_msg = validate_executable_file(bp)
    if not valid_exe:
        raise ValueError(f"Binario eseguibile non valido: {exe_msg}")

    # 4. Compute SHA-256 digest and file size
    file_sha256 = compute_file_sha256(bp)
    file_size = bp.stat().st_size
    filename = bp.name

    if not filename.lower().endswith(".exe"):
        raise ValueError(f"L'asset deve essere un eseguibile Windows (.exe), trovato: '{filename}'")

    # 5. Build and sign manifest
    manifest_data = {
        "version": version,
        "filename": filename,
        "sha256": file_sha256,
        "size": file_size
    }
    signed_manifest = sign_update_manifest(manifest_data, private_key)

    # 6. Final verification against the app's fixed public trust anchor
    ok_verify, msg_verify = verify_update_manifest(
        signed_manifest,
        local_version="0.0.0",
        allow_downgrade=True,
        public_key_hex=target_pub_hex
    )
    if not ok_verify:
        raise RuntimeError(
            f"Verifica crittografica del manifest con la chiave pubblica di trust ('{target_pub_hex}') fallita: {msg_verify}"
        )

    return signed_manifest, target_pub_hex


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strumento di firma crittografica per le release OTA di AetherDesk (Fail-Closed)."
    )
    parser.add_argument(
        "--binary", "-b",
        default=str(PROJECT_ROOT / "dist" / "AetherDesk.exe"),
        help="Percorso dell'eseguibile compilato (default: dist/AetherDesk.exe)"
    )
    parser.add_argument(
        "--version", "-v",
        default=CURRENT_VERSION,
        help=f"Versione semantica del rilascio (default: {CURRENT_VERSION})"
    )
    parser.add_argument(
        "--key-file", "-k",
        required=True,
        help="Percorso del file contenente la chiave privata Ed25519 (consigliato: PEM PKCS#8 cifrato con passphrase). Custodire FUORI dal repo."
    )
    parser.add_argument(
        "--output", "-o",
        help="Percorso di salvataggio del manifest.json (default: stessa directory del binario)"
    )

    args = parser.parse_args()

    # Step 1: Caricamento sicuro della chiave privata da file
    try:
        priv_key = load_private_key(key_file=args.key_file)
    except Exception as e:
        print(f"\n[ERRORE CHIAVE PRIVATA] {e}", file=sys.stderr)
        print("\nCome preparare la chiave privata Ed25519 offline (sul tuo PC sicuro):", file=sys.stderr)
        print("  1. In un terminale Python offline:", file=sys.stderr)
        print("     from cryptography.hazmat.primitives.asymmetric import ed25519", file=sys.stderr)
        print("     from cryptography.hazmat.primitives import serialization", file=sys.stderr)
        print("     import getpass", file=sys.stderr)
        print("     k = ed25519.Ed25519PrivateKey.generate()", file=sys.stderr)
        print("     pwd = getpass.getpass('Passphrase: ').encode('utf-8')", file=sys.stderr)
        print("     pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(pwd))", file=sys.stderr)
        print("     open(r'C:\\percorso_sicuro\\aether_ota_private.pem', 'wb').write(pem)", file=sys.stderr)
        print("     print('CHIAVE PUBBLICA DA FISSARE IN src/ota.py:', k.public_key().public_bytes_raw().hex())", file=sys.stderr)
        print("  2. Custodisci la chiave privata fuori dal repository ed eseguila con '--key-file <percorso>'.\n", file=sys.stderr)
        return 1

    # Step 2: Generazione e verifica fail-closed del manifest
    try:
        signed_manifest, pub_hex = create_signed_manifest(
            binary_path=args.binary,
            version=args.version,
            private_key=priv_key
        )
    except Exception as e:
        print(f"\n[ERRORE FIRMA RELEASE FAIL-CLOSED] {e}", file=sys.stderr)
        print("Nessun manifest e' stato generato o modificato.\n", file=sys.stderr)
        return 1

    # Step 3: Solo dopo il superamento di tutti i controlli, scrittura del manifest
    out_path = Path(args.output) if args.output else Path(args.binary).resolve().parent / "manifest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_json_str = json.dumps(signed_manifest, indent=2)
    out_path.write_text(manifest_json_str, encoding="utf-8")

    print("\n========================================================")
    print("  AetherDesk OTA - Manifest Verificato e Firmato")
    print("========================================================")
    print(f"Versione Asset:          {signed_manifest['version']}")
    print(f"Nome File Asset:         {signed_manifest['filename']}")
    print(f"Digest SHA-256:          {signed_manifest['sha256']}")
    print(f"Dimensione:              {signed_manifest['size']:,} byte")
    print(f"Firma Ed25519:           {signed_manifest['signature'][:32]}...{signed_manifest['signature'][-32:]}")
    print(f"Chiave Pubblica Trust:   {pub_hex}")
    print("\n[OK] La firma e' stata verificata con successo contro la chiave pubblica di trust fissata nell'app.")
    print(f"[OK] File salvato: {out_path}")

    print("\n--------------------------------------------------------")
    print("Blocco da incollare nella descrizione della Release GitHub:")
    print("--------------------------------------------------------")
    print(f"AETHER_MANIFEST:\n{manifest_json_str}\n---")
    print("--------------------------------------------------------")
    print("\nPassi successivi:")
    print(f"  1. Crea la release su GitHub con tag 'v{signed_manifest['version']}'.")
    print(f"  2. Carica '{signed_manifest['filename']}' e 'manifest.json' come asset.")
    print("  3. Incolla il blocco AETHER_MANIFEST sopra riportato nelle note di rilascio.")
    print("  4. I client installati rileveranno l'aggiornamento e chiederanno conferma all'utente.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
