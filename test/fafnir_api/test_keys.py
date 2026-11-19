"""API keys (ADR 0021): what one looks like, and what makes one refused."""

from __future__ import annotations

import datetime as dt

import pytest

from fafnir_api import keys

NOW = dt.datetime(2026, 11, 17, tzinfo=dt.timezone.utc)


def _store(*records):
    by_id = {r.key_id: r for r in records}
    return by_id.get


def _issued(**overrides):
    key_id, token = keys.generate()
    record = keys.KeyRecord(
        key_id=key_id, name="rob-laptop", key_sha256=keys.digest(token), **overrides
    )
    return token, record


def test_a_generated_key_names_its_own_row():
    key_id, token = keys.generate()
    assert token.startswith(f"ffn_{key_id}_")
    assert keys.key_id_of(token) == key_id


def test_two_keys_are_never_the_same():
    assert keys.generate()[1] != keys.generate()[1]


def test_the_stored_digest_is_not_the_key():
    _, token = keys.generate()
    assert token not in keys.digest(token)
    assert len(keys.digest(token)) == 64


def test_a_live_key_authenticates():
    token, record = _issued()
    assert keys.authenticate(token, _store(record), NOW) is record


@pytest.mark.parametrize("token", ["", "hunter2", "ffn_zz_short", "Bearer x"])
def test_something_that_is_not_a_key_is_refused(token):
    with pytest.raises(keys.KeyRefused, match="not a fafnir API key"):
        keys.authenticate(token, _store(), NOW)


def test_an_unknown_key_and_a_wrong_secret_get_the_same_answer():
    token, record = _issued()
    _, other = keys.generate()
    forged = f"ffn_{record.key_id}_{other.rsplit('_', 1)[0][-43:].ljust(43, 'A')}"
    with pytest.raises(keys.KeyRefused) as unknown:
        keys.authenticate(token, _store(), NOW)
    with pytest.raises(keys.KeyRefused) as wrong:
        keys.authenticate(forged, _store(record), NOW)
    assert str(unknown.value) == str(wrong.value) == "unknown key"


def test_a_revoked_key_is_refused():
    token, record = _issued(revoked_at=NOW - dt.timedelta(days=1))
    with pytest.raises(keys.KeyRefused, match="revoked"):
        keys.authenticate(token, _store(record), NOW)


def test_a_key_stops_working_at_its_expiry():
    token, record = _issued(expires_at=NOW)
    with pytest.raises(keys.KeyRefused, match="expired"):
        keys.authenticate(token, _store(record), NOW)
    assert keys.authenticate(token, _store(record), NOW - dt.timedelta(seconds=1))


@pytest.mark.parametrize("name", ["rob-laptop", "ci.portable_2", "a"])
def test_names_that_need_no_quoting_are_accepted(name):
    assert keys.valid_name(name)


@pytest.mark.parametrize("name", ["", "Rob", "has space", "-lead", "x" * 41, "a;b"])
def test_other_names_are_not(name):
    assert not keys.valid_name(name)


def test_the_store_remembers_a_row_and_a_miss(monkeypatch):
    store = keys.DatabaseKeyStore("dsn", ttl=60)
    reads = []
    record = keys.KeyRecord("a" * 12, "n", "0" * 64)

    def read(key_id):
        reads.append(key_id)
        return record if key_id == record.key_id else None

    monkeypatch.setattr(store, "_read", read)
    assert store.lookup(record.key_id) is record
    assert store.lookup(record.key_id) is record
    assert store.lookup("b" * 12) is None
    assert store.lookup("b" * 12) is None
    assert reads == [record.key_id, "b" * 12]


def test_the_store_asks_again_once_the_ttl_has_passed(monkeypatch):
    store = keys.DatabaseKeyStore("dsn", ttl=0)
    reads = []
    monkeypatch.setattr(store, "_read", lambda key_id: reads.append(key_id))
    store.lookup("a" * 12)
    store.lookup("a" * 12)
    assert len(reads) == 2


def test_the_store_cannot_be_grown_without_limit(monkeypatch):
    store = keys.DatabaseKeyStore("dsn", ttl=60, max_entries=4)
    monkeypatch.setattr(store, "_read", lambda key_id: None)
    for i in range(50):
        store.lookup(f"{i:012x}")
    assert len(store._cache) <= 4
