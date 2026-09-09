"""Tests for api/credentials_bootstrap.py and the service-account resolver.

The bug these pin down: `GOOGLE_SERVICE_ACCOUNT_FILE` was set to the service
account key pasted twice. `json.load` then raised
"Extra data: line 14 column 1 (char 2364)", and because `json.JSONDecodeError`
subclasses `ValueError`, `api/main.py`'s ValueError handler returned it as a
bare 400. The Export button showed that sentence with nothing to say it was
about credentials at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import api.credentials_bootstrap as bootstrap
from api.credentials_bootstrap import (
    CredentialProblem,
    credential_problem,
    materialize_json_credentials,
)
from api.dependencies import SERVICE_ACCOUNT_VAR, service_account_file

KEY = {
    "type": "service_account",
    "project_id": "iff-example",
    "private_key_id": "0" * 40,
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
    "client_email": "robot@iff-example.iam.gserviceaccount.com",
    "client_id": "1" * 21,
    "token_uri": "https://oauth2.googleapis.com/token",
}

# A key file downloaded from the Google console ends with a newline, which
# is what puts the duplicate at column 1 of the following line.
KEY_JSON = json.dumps(KEY, indent=2) + "\n"


@pytest.fixture(autouse=True)
def _clean_problems() -> None:
    """The problem table is module state, materialised once per cold start."""
    bootstrap._PROBLEMS.clear()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    monkeypatch.setattr(bootstrap, "_TMP_DIR", str(tmp_path / "creds"))
    return monkeypatch


def _materialise(env: pytest.MonkeyPatch, value: str) -> CredentialProblem | None:
    env.setenv(SERVICE_ACCOUNT_VAR, value)
    materialize_json_credentials()
    return credential_problem(SERVICE_ACCOUNT_VAR)


# ---- the reported bug ----


def test_the_exact_reported_error_comes_from_a_doubled_paste() -> None:
    """Guard on the diagnosis itself: this is the only shape that produces
    "Extra data" at the first column of the line after the document."""
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads(KEY_JSON + KEY_JSON)
    assert caught.value.msg == "Extra data"
    assert caught.value.colno == 1
    assert caught.value.lineno == KEY_JSON.count("\n") + 1
    assert isinstance(caught.value, ValueError), "why main.py's handler swallowed it"


def test_a_key_pasted_twice_is_recovered_not_rejected(env: pytest.MonkeyPatch) -> None:
    """Two byte-identical copies are unambiguous, so the deploy keeps working
    and only warns. This is what unbreaks the live Export button."""
    assert _materialise(env, KEY_JSON + KEY_JSON) is None
    assert json.loads(Path(service_account_file()).read_text()) == KEY


def test_a_key_pasted_three_times_is_still_recovered(env: pytest.MonkeyPatch) -> None:
    assert _materialise(env, "\n".join([KEY_JSON] * 3)) is None
    assert json.loads(Path(service_account_file()).read_text()) == KEY


def test_two_different_keys_are_refused_rather_than_guessed(
    env: pytest.MonkeyPatch,
) -> None:
    """Which of two real keys is current cannot be inferred (CLAUDE.md
    invariant 3)."""
    other = json.dumps(KEY | {"client_email": "someone-else@example.iam.gserviceaccount.com"})
    problem = _materialise(env, KEY_JSON + other)
    assert problem is not None
    assert "2 different JSON documents" in problem.message
    assert SERVICE_ACCOUNT_VAR in problem.message


def test_trailing_junk_is_refused_and_points_at_the_real_line(
    env: pytest.MonkeyPatch,
) -> None:
    problem = _materialise(env, KEY_JSON + "oops")
    assert problem is not None
    # The junk sits on the line after the document, and the message must say
    # so rather than reporting an offset into a trimmed copy.
    expected_line = KEY_JSON.count("\n") + 1
    assert f"line {expected_line}" in problem.message


def test_a_bad_value_never_raises_out_of_import(env: pytest.MonkeyPatch) -> None:
    """This runs at import in api/main.py. If it raised, /api/health would
    stop answering and the platform would restart the container in a loop."""
    for value in ("{", "{}garbage", '{"a": 1} {"b": ', "{ not json at all }"):
        bootstrap._PROBLEMS.clear()
        env.setenv(SERVICE_ACCOUNT_VAR, value)
        materialize_json_credentials()  # must not raise


# ---- the resolver turns all of that into one actionable answer ----


def test_resolver_reports_a_recorded_problem_as_a_409(env: pytest.MonkeyPatch) -> None:
    _materialise(env, KEY_JSON + json.dumps(KEY | {"client_email": "b@example.com"}))
    with pytest.raises(HTTPException) as caught:
        service_account_file()
    assert caught.value.status_code == 409
    assert SERVICE_ACCOUNT_VAR in str(caught.value.detail)


def test_resolver_rejects_a_path_that_is_not_there(env: pytest.MonkeyPatch) -> None:
    env.setenv(SERVICE_ACCOUNT_VAR, "/nope/service_account.json")
    with pytest.raises(HTTPException) as caught:
        service_account_file()
    assert caught.value.status_code == 409
    assert "not a readable file" in str(caught.value.detail)


def test_resolver_rejects_an_unset_variable(env: pytest.MonkeyPatch) -> None:
    env.delenv(SERVICE_ACCOUNT_VAR, raising=False)
    with pytest.raises(HTTPException) as caught:
        service_account_file()
    assert caught.value.status_code == 409
    assert "is not set" in str(caught.value.detail)


def test_resolver_rejects_the_oauth_client_file(env: pytest.MonkeyPatch) -> None:
    """The Gmail OAuth client JSON is a different file and pasting it here is
    the other easy slip; it is valid JSON, so only a shape check catches it."""
    _materialise(env, json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    with pytest.raises(HTTPException) as caught:
        service_account_file()
    assert caught.value.status_code == 409
    assert "not a Google service account key" in str(caught.value.detail)


def test_resolver_rejects_a_key_file_that_is_malformed_on_disk(
    env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The path-shaped case: bootstrap never sees it, so the resolver is the
    only thing between a corrupt file and a cryptic 400."""
    broken = tmp_path / "service_account.json"
    broken.write_text(KEY_JSON + KEY_JSON, encoding="utf-8")
    env.setenv(SERVICE_ACCOUNT_VAR, str(broken))
    with pytest.raises(HTTPException) as caught:
        service_account_file()
    assert caught.value.status_code == 409
    assert "not valid JSON" in str(caught.value.detail)


def test_resolver_accepts_a_good_key_file(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    good = tmp_path / "service_account.json"
    good.write_text(KEY_JSON, encoding="utf-8")
    env.setenv(SERVICE_ACCOUNT_VAR, str(good))
    assert service_account_file() == str(good)


def test_a_path_shaped_value_is_left_completely_alone(
    env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local dev and the CLI pass a path; bootstrap must not touch it."""
    good = tmp_path / "service_account.json"
    good.write_text(KEY_JSON, encoding="utf-8")
    env.setenv(SERVICE_ACCOUNT_VAR, str(good))
    materialize_json_credentials()
    assert credential_problem(SERVICE_ACCOUNT_VAR) is None
