import pytest

from rt2nb.db import ReadOnlyConnection, WriteAttemptError


def _guard(sql):
    ReadOnlyConnection._assert_read_only(sql)


class TestReadOnlyGuard:
    def test_select_allowed(self):
        _guard("SELECT * FROM Object")
        _guard("  select id from Port where name = 'gi0/1'")

    def test_show_and_describe_allowed(self):
        _guard("SHOW TABLES")
        _guard("DESCRIBE Object")

    def test_insert_rejected(self):
        with pytest.raises(WriteAttemptError):
            _guard("INSERT INTO Object (name) VALUES ('x')")

    def test_update_rejected(self):
        with pytest.raises(WriteAttemptError):
            _guard("UPDATE Object SET name='x'")

    def test_delete_rejected(self):
        with pytest.raises(WriteAttemptError):
            _guard("DELETE FROM Object")

    def test_stacked_write_rejected(self):
        with pytest.raises(WriteAttemptError):
            _guard("SELECT 1; DROP TABLE Object")

    def test_write_verb_inside_string_literal_allowed(self):
        # 'update' only appears as data, not as a statement -> allowed.
        _guard("SELECT * FROM Attribute WHERE name = 'last update'")

    def test_column_named_like_verb_allowed(self):
        _guard("SELECT `update_ts` FROM Object")
