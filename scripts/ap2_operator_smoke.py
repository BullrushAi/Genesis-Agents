"""Operator-run AP2 acceptance smoke check using Cato's existing vault identity.

The command intentionally prints only a small structured result.  It never
prints the envelope, headers, response body, vault values, signature, or key
material.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_GENESIS_URL = "https://swarmsync-agents.onrender.com"
DEFAULT_AGENT = "genesis-support"
SMOKE_TASK = "AP2 authentication smoke check. Reply only with: accepted."
_SAFE_AP2_DETAILS = frozenset(
    {
        "ap2_agent_mismatch",
        "ap2_client_untrusted",
        "ap2_envelope_expired",
        "ap2_envelope_invalid",
        "ap2_nonce_invalid",
        "ap2_nonce_store_unavailable",
        "ap2_params_invalid",
        "ap2_params_mismatch",
        "ap2_pubkey_header_mismatch",
        "ap2_pubkey_mismatch",
        "ap2_replay_detected",
        "ap2_scope_denied",
        "ap2_signature_invalid",
        "ap2_task_mismatch",
        "ap2_timestamp_invalid",
        "ap2_version_invalid",
        "ap2_version_unsupported",
    }
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SmokeFailure(Exception):
    """A redaction-safe failure whose fields may be printed."""

    def __init__(self, category: str, action: str, *, http_status: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.action = action
        self.http_status = http_status


def _emit(payload: dict[str, Any], stream: Any) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=stream)


def _validate_endpoint(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SmokeFailure(
            "invalid_config",
            "Set --url to an HTTPS Genesis origin without embedded credentials.",
        )
    if parsed.query or parsed.fragment or (parsed.path not in ("", "/")):
        raise SmokeFailure("invalid_config", "Set --url to the Genesis origin only, without a path.")
    return value.rstrip("/")


def _load_cato(cato_repo: Path, vault_path: Path | None = None) -> tuple[Callable[[], Any], Any]:
    repo_dir = cato_repo.resolve()
    package_dir = repo_dir / "cato"
    if not package_dir.is_dir():
        raise SmokeFailure(
            "cato_dependency_missing",
            "Pass --cato-repo pointing to the Cato repository, then install its dependencies.",
        )
    importlib.invalidate_caches()
    for name in ("cato.vault_crypto", "cato.vault", "cato"):
        existing = sys.modules.get(name)
        existing_file = getattr(existing, "__file__", None)
        if existing_file:
            try:
                Path(existing_file).resolve().relative_to(package_dir)
            except ValueError:
                sys.modules.pop(name, None)
    sys.path.insert(0, str(repo_dir))
    try:
        vault_module = importlib.import_module("cato.vault")
        crypto_module = importlib.import_module("cato.vault_crypto")
    except Exception as exc:
        raise SmokeFailure(
            "cato_dependency_missing",
            "Install the Cato project dependencies in this Python environment.",
        ) from exc
    finally:
        try:
            sys.path.remove(str(repo_dir))
        except ValueError:
            pass
    for module in (vault_module, crypto_module):
        module_file = getattr(module, "__file__", None)
        try:
            if not module_file:
                raise ValueError("module path unavailable")
            Path(module_file).resolve().relative_to(package_dir)
        except ValueError as exc:
            raise SmokeFailure(
                "cato_dependency_untrusted",
                "Ensure --cato-repo points to the intended Cato checkout and retry.",
            ) from exc
    if vault_path is None:
        get_vault = vault_module.get_vault
    else:
        selected = vault_path.expanduser().resolve()
        if not selected.is_file():
            raise SmokeFailure(
                "vault_path_invalid",
                "Pass --vault-path pointing to an existing encrypted Cato vault file.",
            )
        get_vault = lambda: vault_module.Vault(vault_path=selected)
    return get_vault, crypto_module


def _build_request(get_vault: Callable[[], Any], vault_crypto: Any, agent: str) -> tuple[dict, dict]:
    try:
        vault = get_vault()
        key_names = set(vault.list_keys())
        required = {"CATO_AP2_PRIVKEY", "CATO_AP2_PUBKEY"}
        if not required.issubset(key_names):
            raise SmokeFailure(
                "vault_identity_missing",
                "Restore CATO_AP2_PRIVKEY and CATO_AP2_PUBKEY in the Cato vault; this command will not create them.",
            )
        private_b64 = vault.get("CATO_AP2_PRIVKEY")
        public_b64 = vault.get("CATO_AP2_PUBKEY")
    except SmokeFailure:
        raise
    except Exception as exc:
        raise SmokeFailure(
            "vault_unavailable",
            "Unlock the existing Cato vault (interactive prompt or CATO_VAULT_PASSWORD) and retry.",
        ) from exc

    # Refuse to call get_or_create_keypair when either value is absent.  A smoke
    # check must never create or rotate identity material as a side effect.
    if not isinstance(private_b64, str) or not private_b64 or not isinstance(public_b64, str) or not public_b64:
        raise SmokeFailure(
            "vault_identity_missing",
            "Restore CATO_AP2_PRIVKEY and CATO_AP2_PUBKEY in the Cato vault; this command will not create them.",
        )
    try:
        private_raw = base64.b64decode(private_b64, validate=True)
        public_raw = base64.b64decode(public_b64, validate=True)
        if len(private_raw) != 32 or len(public_raw) != 32:
            raise ValueError("unexpected key size")
        # Verify the stored halves agree without displaying either one.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        derived_public = Ed25519PrivateKey.from_private_bytes(private_raw).public_key().public_bytes_raw()
        if derived_public != public_raw:
            raise ValueError("keypair mismatch")
    except Exception as exc:
        raise SmokeFailure(
            "vault_identity_invalid",
            "Repair the restored Cato AP2 keypair in the vault before retrying.",
        ) from exc

    params: dict[str, Any] = {}
    payload = {"agent": agent, "task": SMOKE_TASK, "params": params}
    nonce = uuid.uuid4().hex
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    canonical = json.dumps(
        {"payload": payload, "nonce": nonce, "timestamp": timestamp},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        signature = vault_crypto.sign(vault, canonical)
    except Exception as exc:
        raise SmokeFailure("signing_failed", "Verify the restored Cato vault identity and retry.") from exc

    body = {
        "version": 1,
        "payload": payload,
        "nonce": nonce,
        "timestamp": timestamp,
        "pubkey": public_b64,
        "signature": base64.b64encode(signature).decode("ascii"),
        # These top-level fields are the execution inputs Genesis binds back to
        # the signed payload. live_test keeps this harmless check out of the
        # browser/tool runtime and skip_internal_escrow prevents payment state.
        "prompt": SMOKE_TASK,
        "task": params,
        "mode": "live_test",
        "skip_internal_escrow": True,
    }
    headers = {
        "Content-Type": "application/json",
        "X-AP2-Version": "1",
        "X-AP2-Pubkey": public_b64,
    }
    return body, headers


def _refusal_detail(body: bytes) -> str:
    try:
        detail = json.loads(body.decode("utf-8"))["detail"]
    except Exception:
        return "upstream_refused"
    return detail if isinstance(detail, str) and detail in _SAFE_AP2_DETAILS else "upstream_refused"


def run(
    *,
    url: str,
    agent: str,
    timeout: float,
    attempts: int = 3,
    cato_repo: Path,
    vault_path: Path | None = None,
    opener: Callable[..., Any] | None = None,
    dependency_loader: Callable[[Path], tuple[Callable[[], Any], Any]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    endpoint = _validate_endpoint(url)
    if not _SLUG_RE.fullmatch(agent):
        raise SmokeFailure("invalid_config", "Set --agent to a lowercase Genesis slug.")
    if timeout <= 0 or timeout > 120:
        raise SmokeFailure("invalid_config", "Set --timeout to a value greater than 0 and at most 120 seconds.")
    if attempts < 1 or attempts > 3:
        raise SmokeFailure("invalid_config", "Set --attempts to a value from 1 through 3.")

    opener = opener or urllib.request.urlopen
    if dependency_loader is None:
        # Preserve the original one-argument loader contract when no explicit
        # recovery vault is selected. Besides keeping injected/test loaders
        # stable, this avoids surprising wrappers around the normal operator
        # path. The two-argument form is used only for the opt-in vault override.
        dependency_loader = (
            _load_cato
            if vault_path is None
            else lambda path: _load_cato(path, vault_path)
        )
    get_vault, vault_crypto = dependency_loader(cato_repo)
    fingerprint = ""
    for attempt in range(1, attempts + 1):
        # Every attempt gets a fresh nonce, timestamp, and signature. Reusing an
        # envelope after a lost response would turn a transient retry into an AP2 replay.
        body, headers = _build_request(get_vault, vault_crypto, agent)
        public_raw = base64.b64decode(headers["X-AP2-Pubkey"], validate=True)
        fingerprint = hashlib.sha256(public_raw).hexdigest()
        request = urllib.request.Request(
            f"{endpoint}/agents/{agent}/run",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with opener(request, timeout=timeout) as response:
                status = int(response.status)
                response.read(1024 * 1024)  # drain, but never display or log it
        except urllib.error.HTTPError as exc:
            detail = _refusal_detail(exc.read(8192))
            if exc.code in {408, 425, 429, 500, 502, 503, 504} and attempt < attempts:
                sleeper(float(2 ** (attempt - 1)))
                continue
            raise SmokeFailure(
                detail,
                "Check the Cato trust entry, AP2 scope, restored identity, and Genesis auth health.",
                http_status=exc.code,
            ) from exc
        except TimeoutError as exc:
            if attempt < attempts:
                sleeper(float(2 ** (attempt - 1)))
                continue
            raise SmokeFailure("timeout", "Retry after checking Genesis service health.") from exc
        except Exception as exc:
            if attempt < attempts:
                sleeper(float(2 ** (attempt - 1)))
                continue
            raise SmokeFailure("network_error", "Check DNS, TLS, and Genesis service health, then retry.") from exc

        if status == 200:
            return {
                "accepted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "agent": agent,
                "attempt": attempt,
                "http_status": status,
                "public_key_sha256": fingerprint,
                "status": "accepted",
                "target": endpoint,
            }
        if attempt < attempts and status in {408, 425, 429, 500, 502, 503, 504}:
            sleeper(float(2 ** (attempt - 1)))
            continue
        raise SmokeFailure("upstream_refused", "Check Genesis service health.", http_status=status)
    raise SmokeFailure("upstream_refused", "Check Genesis service health.")


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Verify Genesis accepts Cato's restored AP2 identity.")
    parser.add_argument("--url", default=os.getenv("GENESIS_SMOKE_URL", DEFAULT_GENESIS_URL))
    parser.add_argument("--agent", default=DEFAULT_AGENT)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--cato-repo", type=Path, default=repo_root.parent / "Cato")
    parser.add_argument(
        "--vault-path",
        type=Path,
        default=None,
        help="Optional explicit preserved Cato vault for read-only recovery proof.",
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            url=args.url,
            agent=args.agent,
            timeout=args.timeout,
            attempts=args.attempts,
            cato_repo=args.cato_repo,
            vault_path=args.vault_path,
        )
    except SmokeFailure as exc:
        result: dict[str, Any] = {"action": exc.action, "category": exc.category, "status": "refused"}
        if exc.http_status is not None:
            result["http_status"] = exc.http_status
        _emit(result, sys.stderr)
        return 1
    _emit(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
