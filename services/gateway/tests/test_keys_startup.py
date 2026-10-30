import pytest
from app.core.config import Settings
from app.main import create_app
from ecom_common.keys import generate_rsa_keypair


def test_a_missing_public_key_file_stops_the_gateway_with_a_clear_message():
    settings = Settings(jwt_algorithm="RS256", jwt_public_key_file="/run/keys/nope.pem")
    with pytest.raises(RuntimeError, match="JWT_PUBLIC_KEY_FILE.*INSIDE the container"):
        create_app(settings)


def test_no_key_and_a_garbage_key_both_stop_the_gateway():
    with pytest.raises(RuntimeError, match="no key was provided"):
        create_app(Settings(jwt_algorithm="RS256"))
    with pytest.raises(RuntimeError, match="cannot be used"):
        create_app(Settings(jwt_algorithm="RS256", jwt_public_key="not a pem"))


def test_a_private_key_is_not_accepted_where_a_public_key_belongs():
    # a mix-up that would be dangerous: the gateway must never be handed the private key
    private, _ = generate_rsa_keypair()
    with pytest.raises(RuntimeError, match="cannot be used"):
        create_app(Settings(jwt_algorithm="RS256", jwt_public_key=private))


def test_a_valid_public_key_starts_the_gateway():
    _, public = generate_rsa_keypair()
    create_app(Settings(jwt_algorithm="RS256", jwt_public_key=public))
