from __future__ import annotations

import base64
import io
import json
import logging
import sys
import urllib.error
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import ap2_operator_smoke as smoke


PRIVATE_SENTINEL = "PRIVATE_KEY_MUST_NEVER_LEAK"
PASSWORD_SENTINEL = "VAULT_PASSWORD_MUST_NEVER_LEAK"
AUTH_SENTINEL = "Bearer AUTHORIZATION_MUST_NEVER_LEAK"
SIGNATURE_SENTINEL = "SIGNATURE_MUST_NEVER_LEAK"
CATO_REPO = Path(__file__).resolve().parents[2] / "Cato"


class FakeVault:
    def __init__(self) -> None:
        private = Ed25519PrivateKey.generate()
        self.private_raw = private.private_bytes_raw()
        self.public_raw = private.public_key().public_bytes_raw()

    def get(self, name: str):
        values = {
            "CATO_AP2_PRIVKEY": base64.b64encode(self.private_raw).decode(),
            "CATO_AP2_PUBKEY": base64.b64encode(self.public_raw).decode(),
        }
        return values.get(name)

    def list_keys(self):
        return ["CATO_AP2_PRIVKEY", "CATO_AP2_PUBKEY"]


class FakeCrypto:
    @staticmethod
    def sign(vault, message: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(vault.private_raw).sign(message)


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, _limit=-1):
        return b'{"response":"accepted"}'


def dependencies(_path: Path):
    vault = FakeVault()
    return lambda: vault, FakeCrypto


def test_success_prints_only_acceptance(monkeypatch, capsys, caplog):
    captured_request = None
    test_vault = FakeVault()

    def opener(request, timeout):
        nonlocal captured_request
        captured_request = request
        assert timeout == 5
        return Response()

    monkeypatch.setattr(smoke, "_load_cato", lambda _path: (lambda: test_vault, FakeCrypto))
    monkeypatch.setattr(smoke.urllib.request, "urlopen", opener)
    caplog.set_level(logging.DEBUG)

    assert smoke.main(["--url", "https://genesis.example", "--timeout", "5"]) == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["agent"] == "genesis-support"
    assert result["attempt"] == 1
    assert result["http_status"] == 200
    assert len(result["public_key_sha256"]) == 64
    assert result["status"] == "accepted"
    assert result["target"] == "https://genesis.example"
    assert result["accepted_at"].endswith("Z")
    assert out.err == ""
    wire = json.loads(captured_request.data)
    assert wire["mode"] == "live_test"
    assert wire["skip_internal_escrow"] is True
    assert wire["payload"]["task"] == wire["prompt"]
    assert wire["payload"]["params"] == wire["task"] == {}
    assert captured_request.headers.get("Authorization") is None
    assert caplog.text == ""
    private_b64 = base64.b64encode(test_vault.private_raw).decode()
    combined = out.out + out.err + caplog.text
    assert wire["signature"] not in combined
    assert private_b64 not in combined


def test_structured_ap2_refusal_is_actionable_and_redacted(capsys, caplog):
    vault = FakeVault()

    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            f"{PRIVATE_SENTINEL} {PASSWORD_SENTINEL} {AUTH_SENTINEL} {SIGNATURE_SENTINEL}",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"ap2_client_untrusted","secret":"do-not-print"}'),
        )

    caplog.set_level(logging.DEBUG)
    try:
        smoke.run(
            url="https://genesis.example",
            agent="genesis-support",
            timeout=5,
            cato_repo=Path("unused"),
            opener=opener,
            dependency_loader=lambda _p: (lambda: vault, FakeCrypto),
        )
    except smoke.SmokeFailure as exc:
        result = {"category": exc.category, "action": exc.action, "http_status": exc.http_status}
        print(json.dumps(result), file=sys.stderr)
    captured = capsys.readouterr()
    assert json.loads(captured.err)["category"] == "ap2_client_untrusted"
    combined = captured.out + captured.err + caplog.text
    for sentinel in (PRIVATE_SENTINEL, PASSWORD_SENTINEL, AUTH_SENTINEL, SIGNATURE_SENTINEL, "do-not-print"):
        assert sentinel not in combined


def test_unknown_failure_body_is_not_echoed(capsys):
    vault = FakeVault()

    def opener(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "failure", hdrs=None, fp=io.BytesIO(PRIVATE_SENTINEL.encode())
        )

    try:
        smoke.run(
            url="https://genesis.example",
            agent="genesis-support",
            timeout=5,
            cato_repo=Path("unused"),
            opener=opener,
            dependency_loader=lambda _p: (lambda: vault, FakeCrypto),
        )
    except smoke.SmokeFailure as exc:
        print(exc.category, file=sys.stderr)
    assert capsys.readouterr().err.strip() == "upstream_refused"


def test_missing_cato_dependency_has_clear_category(tmp_path):
    try:
        smoke.run(
            url="https://genesis.example",
            agent="genesis-support",
            timeout=5,
            cato_repo=tmp_path / "missing",
        )
    except smoke.SmokeFailure as exc:
        assert exc.category == "cato_dependency_missing"
        assert "--cato-repo" in exc.action


def test_missing_vault_identity_never_creates_a_keypair():
    class EmptyVault:
        def get(self, _name):
            return None

        def list_keys(self):
            return []

    class MustNotSign:
        @staticmethod
        def sign(*_args):
            raise AssertionError("sign must not be called")

    try:
        smoke._build_request(lambda: EmptyVault(), MustNotSign, "genesis-support")
    except smoke.SmokeFailure as exc:
        assert exc.category == "vault_identity_missing"


def test_locked_or_unreadable_vault_has_clear_redacted_failure(capsys, caplog):
    class BrokenVault:
        def get(self, _name):
            raise RuntimeError(PASSWORD_SENTINEL)

        def list_keys(self):
            raise RuntimeError(PASSWORD_SENTINEL)

    caplog.set_level(logging.DEBUG)
    try:
        smoke._build_request(lambda: BrokenVault(), FakeCrypto, "genesis-support")
    except smoke.SmokeFailure as exc:
        print(json.dumps({"category": exc.category, "action": exc.action}), file=sys.stderr)
    captured = capsys.readouterr()
    assert json.loads(captured.err)["category"] == "vault_unavailable"
    assert PASSWORD_SENTINEL not in captured.out + captured.err + caplog.text


def test_invalid_config_refuses_non_tls_and_embedded_credentials():
    for url in ("http://genesis.example", "https://user:secret@genesis.example", "https://genesis.example/path"):
        try:
            smoke.run(
                url=url,
                agent="genesis-support",
                timeout=5,
                cato_repo=Path("unused"),
                dependency_loader=dependencies,
            )
        except smoke.SmokeFailure as exc:
            assert exc.category == "invalid_config"


def test_transient_retries_use_fresh_signed_envelopes() -> None:
    vault = FakeVault()
    requests: list[dict] = []

    def opener(request, timeout):
        assert timeout == 5
        requests.append(json.loads(request.data))
        if len(requests) < 3:
            raise TimeoutError("transient")
        return Response()

    result = smoke.run(
        url="https://genesis.example",
        agent="genesis-support",
        timeout=5,
        attempts=3,
        cato_repo=Path("unused"),
        opener=opener,
        dependency_loader=lambda _p: (lambda: vault, FakeCrypto),
        sleeper=lambda _seconds: None,
    )
    assert result["attempt"] == 3
    assert len({request["nonce"] for request in requests}) == 3
    assert len({request["signature"] for request in requests}) == 3


def test_current_ap2_verifier_codes_are_safe_to_report() -> None:
    assert "ap2_version_invalid" in smoke._SAFE_AP2_DETAILS
    assert "ap2_pubkey_header_mismatch" in smoke._SAFE_AP2_DETAILS


@pytest.mark.skipif(not (CATO_REPO / "cato").is_dir(), reason="requires sibling Cato checkout")
def test_real_cato_modules_resolve_from_selected_repository() -> None:
    get_vault, crypto = smoke._load_cato(CATO_REPO)
    assert callable(get_vault)
    assert Path(crypto.__file__).resolve().is_relative_to((CATO_REPO / "cato").resolve())


@pytest.mark.skipif(not (CATO_REPO / "cato").is_dir(), reason="requires sibling Cato checkout")
def test_explicit_missing_vault_path_fails_without_creation(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.enc"
    try:
        smoke._load_cato(CATO_REPO, missing)
    except smoke.SmokeFailure as exc:
        assert exc.category == "vault_path_invalid"
    assert not missing.exists()
