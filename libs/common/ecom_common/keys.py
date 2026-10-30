from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def read_key(inline: str = "", path: str = "") -> str:
    """Return key text from a file (preferred: mount it as a secret) or from an env variable."""
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    # Environment variables often hold a PEM on one line with literal "\n" instead of newlines
    return inline.replace("\\n", "\n").strip()


def generate_rsa_keypair(bits: int = 2048) -> tuple[str, str]:
    """Create a new (private_pem, public_pem) pair."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return private_pem, public_key_from_private(private_pem)


def public_key_from_private(private_pem: str) -> str:
    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        private.public_key()  # type: ignore[union-attr]
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
