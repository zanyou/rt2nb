from rt2nb.netbox.client import NetBoxClient


def hdr(token, scheme="auto"):
    return NetBoxClient._auth_header({"token": token, "auth_scheme": scheme})


class TestAuthHeader:
    def test_classic_token_uses_token_scheme(self):
        assert hdr("0123456789abcdef0123456789abcdef01234567") == \
            "Token 0123456789abcdef0123456789abcdef01234567"

    def test_new_key_secret_uses_bearer(self):
        assert hdr("nbt_d2E2rmxxX8op.EaydVhFJ4b8bgDDUFNLL5bbpz") == \
            "Bearer nbt_d2E2rmxxX8op.EaydVhFJ4b8bgDDUFNLL5bbpz"

    def test_strips_pasted_bearer_prefix(self):
        assert hdr("Bearer nbt_key.secret") == "Bearer nbt_key.secret"

    def test_strips_pasted_token_prefix(self):
        assert hdr("Token abc.def") == "Bearer abc.def"

    def test_force_scheme_override(self):
        assert hdr("nbt_key.secret", scheme="token") == "Token nbt_key.secret"
        assert hdr("40hextoken", scheme="bearer") == "Bearer 40hextoken"

    def test_whitespace_trimmed(self):
        assert hdr("  nbt_a.b  ") == "Bearer nbt_a.b"
