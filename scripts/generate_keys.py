"""Create the secrets a production setup needs. Run it once, from the project root:

    python scripts/generate_keys.py

It writes an RS256 key pair for signing login tokens:

    .keys/auth/jwt_private.pem      the PRIVATE key: only the auth service may see this
    .keys/gateway/jwt_public.pem    the PUBLIC key: the gateway uses it to check tokens

and prints the lines to add to your .env. Keep the private key out of git (.keys/ is ignored),
back it up somewhere safe, and never put it in the same place as the gateway.
Existing keys are never overwritten unless you pass --force (that would log everybody out).
"""

import argparse
import os
import secrets
import sys
from pathlib import Path

from ecom_common.keys import generate_rsa_keypair

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--force", action="store_true", help="replace existing keys (logs everyone out)"
    )
    args = parser.parse_args()

    private_path = ROOT / ".keys" / "auth" / "jwt_private.pem"
    public_path = ROOT / ".keys" / "gateway" / "jwt_public.pem"
    if (private_path.exists() or public_path.exists()) and not args.force:
        print(
            "Keys already exist. Nothing changed. Use --force to replace them (logs everyone out)."
        )
        return 1

    private_pem, public_pem = generate_rsa_keypair(bits=3072)
    for path, content in ((private_path, private_pem), (public_path, public_pem)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    try:
        os.chmod(private_path, 0o600)  # owner only (has no effect on Windows)
    except OSError:
        pass

    print(f"Wrote {private_path.relative_to(ROOT)}  (private: auth service only)")
    print(f"Wrote {public_path.relative_to(ROOT)}  (public: gateway)\n")
    print("Add to your .env (the internal secret below is freshly generated, keep it private):\n")
    print("JWT_ALGORITHM=RS256")
    print("JWT_PRIVATE_KEY_FILE=/run/keys/jwt_private.pem")
    print("JWT_PUBLIC_KEY_FILE=/run/keys/jwt_public.pem")
    print(f"INTERNAL_AUTH_SECRET={secrets.token_urlsafe(48)}")
    print("ENVIRONMENT=production   # only when really deploying: it enforces all of the above")
    return 0


if __name__ == "__main__":
    sys.exit(main())
