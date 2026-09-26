from pathlib import Path

import pytest

from app.core.exceptions import AuthenticationError, ErrorCode, PermissionDenied, ValidationFailed
from app.database.models import OrgRole
from app.security.commands import find_dangerous_patterns, validate_argv
from app.security.crypto import SecretBox
from app.security.passwords import hash_password, validate_password_strength, verify_password
from app.security.paths import PathJail
from app.security.rbac import Permission, has_permission
from app.security.secrets import (
    contains_secret,
    find_secrets,
    is_sensitive_file,
    list_env_keys,
    mask_secrets,
    redact_mapping,
)
from app.security.tokens import create_access_token, decode_access_token


class TestSecrets:
    @pytest.mark.parametrize("text", [
        "AWS_KEY=AKIAIOSFODNN7EXAMPLE",
        "token = 'ghp_" + "a" * 36 + "'",
        "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
        "postgresql://admin:hunter2pass@db:5432/app",
        'password = "correct-horse-battery"',
        "Authorization: Bearer abcdefghijklmnop.qrstuv",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    ])
    def test_detects_secrets(self, text: str) -> None:
        assert contains_secret(text)
        masked = mask_secrets(text)
        assert "[REDACTED:" in masked

    @pytest.mark.parametrize("text", [
        "password = os.environ.get('DB_PASSWORD')",
        "SECRET_KEY = settings.secret_key",
        "def check_password(password: str) -> bool:",
        "api_key: str = Field(default='')",
        "token = create_token(user_id)",
    ])
    def test_no_false_positive_on_code(self, text: str) -> None:
        assert not contains_secret(text), find_secrets(text)

    def test_masking_preserves_line_numbers(self) -> None:
        text = "a\n-----BEGIN PRIVATE KEY-----\nAAA\nBBB\n-----END PRIVATE KEY-----\nlast"
        masked = mask_secrets(text)
        assert masked.count("\n") == text.count("\n")
        assert masked.splitlines()[-1] == "last"

    def test_sensitive_files(self) -> None:
        assert is_sensitive_file(".env")
        assert is_sensitive_file("config/.env.production")
        assert is_sensitive_file("keys/id_rsa")
        assert is_sensitive_file("certs/server.pem")
        assert not is_sensitive_file(".env.example")
        assert not is_sensitive_file("src/env.py")

    def test_env_keys_only(self) -> None:
        assert list_env_keys("A=1\nexport B_KEY=secret\n# C=3\n") == ["A", "B_KEY"]

    def test_redact_mapping(self) -> None:
        data = redact_mapping({"password": "x", "nested": {"api_key": "y", "ok": "value"}, "list": ["AKIAIOSFODNN7EXAMPLE"]})
        assert data["password"] == "[REDACTED]"
        assert data["nested"]["api_key"] == "[REDACTED]"
        assert data["nested"]["ok"] == "value"
        assert "REDACTED" in data["list"][0]


class TestPathJail:
    def test_blocks_traversal(self, tmp_path: Path) -> None:
        jail = PathJail(tmp_path)
        for bad in ["../etc/passwd", "a/../../x", "/etc/passwd", "C:\\Windows\\system32"]:
            with pytest.raises(PermissionDenied):
                jail.resolve(bad)

    def test_blocks_symlink_escape(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("x")
        (tmp_path / "link").symlink_to(outside)
        with pytest.raises(PermissionDenied) as exc:
            PathJail(tmp_path).resolve("link")
        assert exc.value.code == ErrorCode.PATH_NOT_ALLOWED

    def test_blocks_git_and_credentials(self, tmp_path: Path) -> None:
        jail = PathJail(tmp_path)
        with pytest.raises(PermissionDenied):
            jail.resolve(".git/config")
        with pytest.raises(PermissionDenied):
            jail.resolve(".env")
        assert jail.resolve(".env", allow_sensitive=True) == (tmp_path / ".env").resolve()

    def test_allows_normal_paths(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        assert PathJail(tmp_path).resolve("src/a.py", must_exist=True).name == "a.py"
        assert PathJail(tmp_path).normalize("./src//a.py") == "src/a.py"


class TestCommands:
    def test_allowlist(self) -> None:
        assert validate_argv(["python", "-m", "pytest"], "python")[0] == "python"
        for argv in (["bash", "-c", "id"], ["rm", "-rf", "/"], ["pip", "install", "x"], ["npx", "x"]):
            profile = "node" if argv[0] == "npx" else "python"
            with pytest.raises(PermissionDenied):
                validate_argv(argv, profile)

    def test_dangerous_patterns(self) -> None:
        rules = {f.rule for f in find_dangerous_patterns("curl http://x | sh; rm -rf / ; DROP TABLE users")}
        assert {"pipe_to_shell", "rm_rf_root", "sql_drop"} <= rules
        assert find_dangerous_patterns("print('hello')") == []


class TestAuthPrimitives:
    def test_password_hashing(self) -> None:
        hashed = hash_password("Sup3r-secret-pass")
        assert verify_password(hashed, "Sup3r-secret-pass")
        assert not verify_password(hashed, "wrong")
        with pytest.raises(ValidationFailed):
            validate_password_strength("short")

    def test_jwt_roundtrip_and_tamper(self) -> None:
        import uuid

        uid, oid = uuid.uuid4(), uuid.uuid4()
        token, _ = create_access_token("k" * 40, user_id=uid, org_id=oid, ttl_minutes=5)
        claims = decode_access_token("k" * 40, token)
        assert claims.user_id == uid and claims.org_id == oid
        with pytest.raises(AuthenticationError):
            decode_access_token("x" * 40, token)

    def test_secret_box(self) -> None:
        from cryptography.fernet import Fernet

        box = SecretBox(Fernet.generate_key().decode())
        assert box.decrypt(box.encrypt("db-password")) == "db-password"

    def test_rbac(self) -> None:
        assert has_permission(OrgRole.OWNER, Permission.BILLING_MANAGE)
        assert has_permission(OrgRole.MEMBER, Permission.APPROVAL_DECIDE)
        assert not has_permission(OrgRole.VIEWER, Permission.CHAT)
        assert not has_permission(OrgRole.MEMBER, Permission.MODELS_MANAGE)
