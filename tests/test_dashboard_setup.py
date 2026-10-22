"""凭据初始化必须保留其他配置、仅存哈希、轮换签名密钥，并原子提交。"""

import importlib.util
import os
from pathlib import Path

import bcrypt
import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard_setup_test", ROOT / "scripts/setup_dashboard.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_initializes_default_account_with_independent_random_passwords(tmp_path):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    username, password = setup.initialize(first)
    _, other_password = setup.initialize(second)
    config = dotenv_values(first)
    assert username == config["DASHBOARD_USER"] == "admin"
    assert len(password) >= 24 and password != other_password
    assert bcrypt.checkpw(password.encode(), config["DASHBOARD_PASSWORD_HASH"].encode())
    assert password not in first.read_text(encoding="utf-8")
    assert len(config["DASHBOARD_SECRET_KEY"]) >= 32
    assert config["DASHBOARD_SECRET_KEY"] != dotenv_values(second)["DASHBOARD_SECRET_KEY"]
    if os.name != "nt":
        assert first.stat().st_mode & 0o777 == 0o600


def test_preserves_unrelated_settings_and_updates_duplicate_dashboard_keys(tmp_path):
    env = tmp_path / ".env"
    original = '# keep\nHOST=127.0.0.1\nGROUP_WHITELIST=["123"]\nOTHER="multi\nline"\nDASHBOARD_USER=old\nDASHBOARD_PASSWORD_HASH=old\nDASHBOARD_USER=duplicate'
    env.write_text(original, encoding="utf-8")
    username, password = setup.initialize(env, reset=True)
    config = dotenv_values(env)
    assert config["HOST"] == "127.0.0.1" and config["OTHER"] == "multi\nline"
    assert config["GROUP_WHITELIST"] == '["123"]'
    assert env.read_text(encoding="utf-8").count("DASHBOARD_USER=") == 1
    assert config["DASHBOARD_USER"] == username == "admin"
    assert bcrypt.checkpw(password.encode(), config["DASHBOARD_PASSWORD_HASH"].encode())


def test_existing_password_requires_explicit_reset_and_old_key_changes(tmp_path):
    env = tmp_path / ".env"
    setup.initialize(env)
    original = env.read_text(encoding="utf-8")
    old = dotenv_values(env)
    with pytest.raises(ValueError, match="--reset"):
        setup.initialize(env)
    assert env.read_text(encoding="utf-8") == original
    setup.initialize(env, reset=True)
    assert dotenv_values(env)["DASHBOARD_SECRET_KEY"] != old["DASHBOARD_SECRET_KEY"]
    assert dotenv_values(env)["DASHBOARD_PASSWORD_HASH"] != old["DASHBOARD_PASSWORD_HASH"]


def test_failed_replace_keeps_original_and_removes_temporary(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("PORT=8082\n", encoding="utf-8")
    def fail(*args):
        raise OSError("simulated write failure")
    monkeypatch.setattr(setup.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        setup.initialize(env)
    assert env.read_text(encoding="utf-8") == "PORT=8082\n"
    assert not list(tmp_path.glob(".dashboard-env-*"))


def test_rejects_malformed_env_without_changing_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('SECRET="unterminated', encoding="utf-8")
    with pytest.raises(ValueError, match="无法解析"):
        setup.initialize(env)
    assert env.read_text(encoding="utf-8") == 'SECRET="unterminated'
