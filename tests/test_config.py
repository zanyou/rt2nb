"""Config loading: built-in defaults, deep merge of the YAML file, and
environment-variable overrides (RT2NB_SECTION__KEY) with type coercion."""

import os

from rt2nb.config import Config, _apply_env_overrides, _coerce, _deep_merge


class TestDeepMerge:
    def test_nested_override_keeps_sibling_defaults(self):
        base = {"netbox": {"url": "http://x", "timeout": 30, "retries": 5}}
        merged = _deep_merge(base, {"netbox": {"timeout": 99}})
        assert merged["netbox"]["timeout"] == 99
        assert merged["netbox"]["url"] == "http://x"   # sibling preserved
        assert merged["netbox"]["retries"] == 5

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        _deep_merge(base, {"a": {"b": 2}})
        assert base["a"]["b"] == 1

    def test_non_dict_value_replaces(self):
        merged = _deep_merge({"mapping": {"x": {"k": 1}}}, {"mapping": {"x": 5}})
        assert merged["mapping"]["x"] == 5


class TestCoerce:
    def test_bool_like(self):
        assert _coerce("true", False) is True
        assert _coerce("no", True) is False
        assert _coerce("1", False) is True

    def test_int_like(self):
        assert _coerce("42", 0) == 42
        assert _coerce("notint", 0) == "notint"   # falls back to raw string

    def test_float_like(self):
        assert _coerce("0.25", 0.5) == 0.25

    def test_str_default_passthrough(self):
        assert _coerce("hello", "world") == "hello"


class TestEnvOverrides:
    def test_override_leaf_with_type_preserved(self, monkeypatch):
        cfg = {"netbox": {"timeout": 30, "ssl_verify": True}}
        monkeypatch.setenv("RT2NB_NETBOX__TIMEOUT", "90")
        monkeypatch.setenv("RT2NB_NETBOX__SSL_VERIFY", "false")
        _apply_env_overrides(cfg)
        assert cfg["netbox"]["timeout"] == 90          # coerced to int
        assert cfg["netbox"]["ssl_verify"] is False     # coerced to bool

    def test_creates_missing_path(self, monkeypatch):
        cfg = {}
        monkeypatch.setenv("RT2NB_RACKTABLES__PASSWORD", "secret")
        _apply_env_overrides(cfg)
        assert cfg["racktables"]["password"] == "secret"

    def test_unrelated_env_ignored(self, monkeypatch):
        cfg = {"netbox": {"timeout": 30}}
        monkeypatch.setenv("SOMETHING_ELSE", "x")
        _apply_env_overrides(cfg)
        assert cfg == {"netbox": {"timeout": 30}}


class TestConfigLoad:
    def test_defaults_when_no_file(self):
        cfg = Config.load(None)
        assert cfg.netbox["auth_scheme"] == "auto"
        assert cfg.netbox["page_size"] == 200
        assert cfg.mapping["row_as"] == "location"
        assert cfg.paths["export_dir"] == "./export_data"

    def test_file_merges_over_defaults(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("netbox:\n  token: abc\n  timeout: 5\n")
        cfg = Config.load(str(p))
        assert cfg.netbox["token"] == "abc"
        assert cfg.netbox["timeout"] == 5
        assert cfg.netbox["retries"] == 5        # default preserved

    def test_env_beats_file_and_defaults(self, tmp_path, monkeypatch):
        p = tmp_path / "config.yml"
        p.write_text("netbox:\n  token: fromfile\n")
        monkeypatch.setenv("RT2NB_NETBOX__TOKEN", "fromenv")
        cfg = Config.load(str(p))
        assert cfg.netbox["token"] == "fromenv"
