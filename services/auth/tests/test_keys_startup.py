import pytest
from app.core.config import Settings
from app.main import create_app
from ecom_common.keys import generate_rsa_keypair


def test_a_missing_key_file_stops_the_service_with_a_clear_message():
    settings = Settings(jwt_algorithm="RS256", jwt_private_key_file="/run/keys/nope.pem")
    with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY_FILE.*INSIDE the container"):
        create_app(settings)


def test_no_key_at_all_stops_the_service():
    with pytest.raises(RuntimeError, match="no key was provided"):
        create_app(Settings(jwt_algorithm="RS256"))


def test_a_garbage_key_stops_the_service():
    with pytest.raises(RuntimeError, match="cannot be used"):
        create_app(Settings(jwt_algorithm="RS256", jwt_private_key="not a pem"))


def test_a_valid_key_starts_and_hs256_needs_no_key(tmp_path):
    private, _ = generate_rsa_keypair()
    path = tmp_path / "private.pem"
    path.write_text(private)
    create_app(Settings(jwt_algorithm="RS256", jwt_private_key_file=str(path)))
    create_app(Settings(jwt_algorithm="HS256"))
