"""Endpoint tests for the FastAPI wrapper (beta, SPEC.md §14).

The API is a thin shell over the alpha core, so these tests check the shell:
routing, status codes, the {detail: ...} error shape, and that a run
produced through HTTP is the same shape the CLI writes. The scheduling logic
itself is covered by the `iff_scheduler` test suite.

Isolation: every test points IFFSCHED_DATA_DIR at a tmp dir, so every path
in `iff_scheduler.workspace` lands under tmp and never touches the real repo
data. (It also chdirs, which keeps any remaining relative path in the same
place.)

The suite also runs against a real Supabase project whenever `SUPABASE_*`
is set (a developer `.env` is enough), and that project is *shared* — other
runs, and other machines, write to the same tables. So it must not depend on
the table starting empty and must not leave rows behind:

* every test names its workspaces with a unique ``pt-`` prefix
  (:func:`wsname`), so a create never collides with a leftover row; and
* :func:`_sweep_test_workspaces` (autouse) deletes every ``pt-`` workspace
  the test created once it finishes, in whichever store is live.

Together that makes the suite idempotent and order-independent regardless of
what the store held when it started.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "applicants_raw.csv"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("IFFSCHED_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    # Name disguise off unless a test turns it on: set to empty rather than
    # deleted so a developer's own `.env` cannot switch it back on mid-suite.
    monkeypatch.setenv("REVEAL_PASSWORD", "")
    return TestClient(app)


# --------------------------------------------------------------- store hygiene

_WS_PREFIX = "pt-"


@pytest.fixture
def wsname() -> str:
    """A unique workspace name for one test.

    The ``pt-`` prefix marks it as this suite's, so a run against a shared
    Supabase project neither collides with a workspace left by an earlier run
    nor depends on the table being empty. :func:`_sweep_test_workspaces`
    removes it again afterwards.
    """
    return f"{_WS_PREFIX}{uuid.uuid4().hex[:12]}"


def _hard_delete_workspace(name: str) -> None:
    """Drop a workspace from whichever store is live.

    Goes straight to the backend rather than through ``DELETE
    /api/workspaces/{id}`` so cleanup does not depend on the route's own
    behaviour. Best-effort: a failure here must never fail the test that
    already passed.
    """
    try:
        from iff_scheduler.db import supabase_enabled

        if supabase_enabled():
            from iff_scheduler.db import workspace_repo

            workspace_repo.delete_workspace(name)
        else:
            from iff_scheduler.workspace import load_workspaces, save_workspaces

            save_workspaces([w for w in load_workspaces() if w.name != name])
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _sweep_test_workspaces(client: TestClient):
    """Delete every ``pt-``-prefixed workspace once the test finishes.

    Rows without the prefix are left untouched, so a shared Supabase project
    is returned to exactly the state the test found it in. Runs for every
    test (autouse) and tolerates a test that made no workspace at all.
    """
    yield
    try:
        rows = client.get("/api/workspaces").json()
    except Exception:
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        name = str(row.get("name", ""))
        if name.startswith(_WS_PREFIX):
            _hard_delete_workspace(name)


def _create_ws(client: TestClient, name: str, group: str = "Test Environment"):
    return client.post("/api/workspaces", json={"name": name, "group": group})


def _ingest_fixture(client: TestClient, name: str):
    with FIXTURE_CSV.open("rb") as fh:
        return client.post(
            f"/api/workspaces/{name}/ingest",
            data={"source": "csv"},
            files={"file": ("applicants_raw.csv", fh, "text/csv")},
        )


# --------------------------------------------------------------- health / meta


def test_health(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_debug_reports_backend_flags_as_booleans(client: TestClient) -> None:
    """/api/debug is the deploy-diagnostic endpoint: it must always answer
    200 with the four boolean flags, whatever backend is configured."""
    resp = client.get("/api/debug")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "supabase_enabled",
        "supabase_url_set",
        "supabase_key_set",
        "workspaces_file_exists",
    }
    assert all(isinstance(v, bool) for v in body.values())


# --------------------------------------------------------------- workspaces


def test_workspace_crud_lifecycle(client: TestClient, wsname: str) -> None:
    created = _create_ws(client, wsname)
    assert created.status_code == 201
    assert created.json()["name"] == wsname
    assert created.json()["group"] == "Test Environment"

    listed = client.get("/api/workspaces")
    assert listed.status_code == 200
    assert wsname in [w["name"] for w in listed.json()]

    one = client.get(f"/api/workspaces/{wsname}")
    assert one.status_code == 200
    assert one.json()["name"] == wsname

    deleted = client.delete(f"/api/workspaces/{wsname}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": wsname}

    assert client.get(f"/api/workspaces/{wsname}").status_code == 404


def test_create_duplicate_workspace_conflicts(client: TestClient, wsname: str) -> None:
    assert _create_ws(client, wsname).status_code == 201
    dup = _create_ws(client, wsname)
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]


def test_unknown_workspace_returns_detail_404(client: TestClient) -> None:
    resp = client.get("/api/workspaces/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Workspace 'nope' not found."}


# --------------------------------------------------------------- pipeline


def test_ingest_csv_upload(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    resp = _ingest_fixture(client, wsname)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applicants"] >= 1
    assert "report" in body


def test_rejected_tab_lists_rows_and_recover_moves_one_to_clean(
    client: TestClient, wsname: str
) -> None:
    _create_ws(client, wsname)
    ingested = _ingest_fixture(client, wsname)
    before = ingested.json()["applicants"]

    rejected = client.get(f"/api/workspaces/{wsname}/rejected")
    assert rejected.status_code == 200
    rows = rejected.json()
    # "Program" twice (Citra) no longer lands here — it collapses to a single
    # interview at ingest (E-01b) rather than being rejected.
    assert {r["reason_code"] for r in rows} == {
        "UNKNOWN_SUBDIVISION",
        "MISSING_FULL_NAME",
    }
    recoverable = next(r for r in rows if r["reason_code"] == "MISSING_FULL_NAME")
    assert recoverable["csv_row"] == recoverable["row_number"] + 1
    assert recoverable["recoverable"] is True
    # An unmappable sub-division has no division to schedule against.
    unknown = next(r for r in rows if r["reason_code"] == "UNKNOWN_SUBDIVISION")
    assert unknown["recoverable"] is False

    recovered = client.post(f"/api/workspaces/{wsname}/recover/{recoverable['row_number']}")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["applicants"] == before + 1
    assert "Re-run Schedule" in recovered.json()["message"]

    still_rejected = client.get(f"/api/workspaces/{wsname}/rejected").json()
    assert recoverable["row_number"] not in {r["row_number"] for r in still_rejected}


def test_recover_refuses_a_non_recoverable_row(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    rows = client.get(f"/api/workspaces/{wsname}/rejected").json()
    unknown = next(r for r in rows if r["reason_code"] == "UNKNOWN_SUBDIVISION")
    resp = client.post(f"/api/workspaces/{wsname}/recover/{unknown['row_number']}")
    assert resp.status_code == 409


def test_ingest_status_reflects_whether_applicants_exist(client: TestClient, wsname: str) -> None:
    """The UI gates Schedule! on this so it never fires it just to get "Run
    ingest first" back."""
    _create_ws(client, wsname)
    before = client.get(f"/api/workspaces/{wsname}/ingest-status")
    assert before.status_code == 200
    assert before.json() == {"ingested": False, "applicants": 0}

    _ingest_fixture(client, wsname)
    after = client.get(f"/api/workspaces/{wsname}/ingest-status")
    assert after.status_code == 200
    body = after.json()
    assert body["ingested"] is True
    assert body["applicants"] >= 1


def test_solve_publish_and_assignments_flow(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)

    solved = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True})
    assert solved.status_code == 200, solved.text
    run_id = solved.json()["run_id"]
    assert solved.json()["interviews_placed"] == solved.json()["interviews_required"]

    runs = client.get(f"/api/workspaces/{wsname}/runs")
    assert runs.status_code == 200
    assert [r["run_id"] for r in runs.json()] == [run_id]

    detail = client.get(f"/api/workspaces/{wsname}/runs/{run_id}")
    assert detail.status_code == 200
    # The detail payload is shaped differently by the file store (carries a
    # `files` list) and the DB store (carries `status` + `metrics`); both
    # echo the resolved run id, which is what callers key on.
    assert detail.json()["run_id"] == run_id

    published = client.post(
        f"/api/workspaces/{wsname}/publish", json={"run": "latest", "formats": ["html"]}
    )
    assert published.status_code == 200
    assert published.json()["applicants"] >= 1

    assignments = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments")
    assert assignments.status_code == 200
    rows = assignments.json()
    assert len(rows) == solved.json()["interviews_required"]
    assert all(":" in r["assignment_id"] for r in rows)

    # Every row carries the applicant's declared day/time preference (FR-51);
    # the fixture picks whole event days, so at least one reads as a day label.
    assert all("declared_availability" in r for r in rows)
    assert any(r["declared_availability"] in {"Thu 17 Sep", "Fri 18 Sep"} for r in rows)


def test_room_scenarios_endpoint_lists_default_and_extended(client: TestClient) -> None:
    resp = client.get("/api/workspaces/any-workspace/room-scenarios")
    assert resp.status_code == 200
    scenarios = resp.json()["scenarios"]
    assert scenarios[0] == "default"
    assert "extended_waiting_rooms" in scenarios


def test_solve_accepts_an_alternate_room_scenario(client: TestClient, wsname: str) -> None:
    """A solve-time choice (SPEC.md §3.3 "extended waiting rooms") — no file
    edited by hand, and it never touches the default (see
    `test_solve_with_room_scenario_default_is_unaffected` below)."""
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)

    solved = client.post(
        f"/api/workspaces/{wsname}/solve",
        json={"skip_check": True, "room_scenario": "extended_waiting_rooms"},
    )
    assert solved.status_code == 200, solved.text
    assert solved.json()["room_scenario"] == "extended_waiting_rooms"
    assert solved.json()["interviews_placed"] == solved.json()["interviews_required"]


def test_solve_rejects_an_unknown_room_scenario(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)

    resp = client.post(
        f"/api/workspaces/{wsname}/solve",
        json={"skip_check": True, "room_scenario": "made_up_scenario"},
    )
    assert resp.status_code == 400, resp.text
    assert "Unknown room scenario" in resp.json()["detail"]


def test_solve_with_room_scenario_default_is_unaffected(client: TestClient, wsname: str) -> None:
    """Explicitly asking for the default scenario is identical to not asking
    at all — this feature is additive, not a change to existing behaviour."""
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)

    implicit = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True})
    assert implicit.status_code == 200, implicit.text
    assert implicit.json()["room_scenario"] == "default"


def test_solve_without_applicants_is_404(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    resp = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True})
    assert resp.status_code == 404


def test_download_bundle_zips_all_three_spreadsheets(client: TestClient, wsname: str) -> None:
    """One "Download XLSX" click yields a ZIP holding all three spreadsheets
    (schedule, rooms, and the Applicants tab export)."""
    import io
    import zipfile

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    published = client.post(
        f"/api/workspaces/{wsname}/publish", json={"run": "latest", "formats": ["xlsx"]}
    )
    assert published.status_code == 200, published.text

    resp = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert run_id in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert sorted(zf.namelist()) == ["applicants.xlsx", "rooms.xlsx", "schedule.xlsx"]
        assert all(zf.read(name)[:2] == b"PK" for name in zf.namelist())


def test_download_bundle_before_publish_publishes_on_demand(
    client: TestClient, wsname: str
) -> None:
    """A solved run's `assignments.csv` is already on disk, so a download
    with no prior explicit Publish call still succeeds — it publishes fresh
    rather than 409ing (see the manual-edit regression test below for why the
    download always republishes)."""
    import io
    import zipfile

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    resp = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/xlsx")
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert sorted(zf.namelist()) == ["applicants.xlsx", "rooms.xlsx", "schedule.xlsx"]


def test_download_bundle_reflects_a_manual_edit_made_after_publish(
    client: TestClient, wsname: str
) -> None:
    """Regression test: a manual lock/move writes straight to the run's
    `assignments.csv` (FR-41), not to the already-published xlsx files. The
    downloaded ZIP must reflect that edit even though nothing re-ran
    `/publish` — this used to silently serve the pre-edit files."""
    import io
    import zipfile

    from openpyxl import load_workbook

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    # Publish once, exactly like "Schedule!" does — this is the snapshot that
    # used to go stale.
    published = client.post(
        f"/api/workspaces/{wsname}/publish", json={"run": "latest", "formats": ["xlsx"]}
    )
    assert published.status_code == 200, published.text

    def applicants_sheet_text() -> str:
        resp = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/xlsx")
        assert resp.status_code == 200
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            ws = load_workbook(io.BytesIO(zf.read("applicants.xlsx"))).active
            return "\n".join(
                str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
            )

    assert "\U0001f512" not in applicants_sheet_text()  # nothing locked yet

    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    first = rows[0]
    locked = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{first['assignment_id']}/lock",
        json={"locked": True},
    )
    assert locked.status_code == 200, locked.text

    # No explicit re-publish in between — the download itself must pick up
    # the lock the manual edit just wrote to assignments.csv.
    assert "\U0001f512" in applicants_sheet_text()


def test_download_bundle_survives_local_run_dir_being_wiped(
    client: TestClient, wsname: str
) -> None:
    """Regression: Railway's filesystem is wiped on every redeploy
    (docs/DEPLOY.md), so a run solved before one has no `assignments.csv` on
    this instance any more. In Supabase mode the download must still
    regenerate the current — possibly since manually edited — schedule from
    Postgres rather than demanding a re-solve; only the file-store backend is
    genuinely stuck once its one copy is gone."""
    import io
    import shutil
    import zipfile

    from openpyxl import load_workbook

    from iff_scheduler import workspace as iff_ws
    from iff_scheduler.db import supabase_enabled

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    published = client.post(
        f"/api/workspaces/{wsname}/publish", json={"run": "latest", "formats": ["xlsx"]}
    )
    assert published.status_code == 200, published.text

    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    first = rows[0]
    locked = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{first['assignment_id']}/lock",
        json={"locked": True},
    )
    assert locked.status_code == 200, locked.text

    # Simulate a redeploy: this instance's local copies of the run and its
    # published output are gone.
    shutil.rmtree(iff_ws.runs_dir(wsname) / run_id, ignore_errors=True)
    shutil.rmtree(iff_ws.output_dir(wsname) / run_id, ignore_errors=True)
    assert not (iff_ws.runs_dir(wsname) / run_id).exists()

    resp = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/xlsx")
    if not supabase_enabled():
        # File-store mode has no second copy — the run directory *is* the
        # record, so wiping it makes the run itself unknown (404), same as
        # before this fix.
        assert resp.status_code == 404
        return

    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        wb = load_workbook(io.BytesIO(zf.read("applicants.xlsx")))
        text = "\n".join(
            str(c.value) for row in wb.active.iter_rows() for c in row if c.value is not None
        )
    assert "\U0001f512" in text


# --------------------------------------------------------------- schedule edits


def test_patch_assignment_rejects_unknown_slot(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    target = rows[0]["assignment_id"]

    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{target}",
        json={"panel_id": rows[0]["panel_id"], "slot_id": "NOT-A-SLOT"},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_patch_assignment_locks_and_survives_resolve(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()

    # Move the first interview onto its own current panel + slot: a no-op
    # placement that is always legal, but still records a lock.
    first = rows[0]
    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{first['assignment_id']}",
        json={"panel_id": first["panel_id"], "slot_id": first["slot_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is True
    assert resp.json()["assignment"]["is_locked"] is True

    resolved = client.post(f"/api/workspaces/{wsname}/runs/{run_id}/resolve", json={})
    assert resolved.status_code == 200
    new_run = resolved.json()["run_id"]
    new_rows = client.get(f"/api/workspaces/{wsname}/runs/{new_run}/assignments").json()
    locked = next(r for r in new_rows if r["assignment_id"] == first["assignment_id"])
    assert locked["panel_id"] == first["panel_id"]
    assert locked["slot_id"] == first["slot_id"]
    assert resolved.json()["locked"] >= 1


def test_toggle_assignment_lock_locks_then_unlocks(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    first = rows[0]
    assert first["is_locked"] is False

    locked = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{first['assignment_id']}/lock",
        json={"locked": True},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["locked"] is True
    assert locked.json()["assignment"]["is_locked"] is True
    assert locked.json()["total_locks"] == 1

    after_lock = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    assert next(r for r in after_lock if r["assignment_id"] == first["assignment_id"])["is_locked"]

    unlocked = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{first['assignment_id']}/lock",
        json={"locked": False},
    )
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["locked"] is False
    assert unlocked.json()["assignment"]["is_locked"] is False
    assert unlocked.json()["total_locks"] == 0

    after_unlock = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    assert not next(r for r in after_unlock if r["assignment_id"] == first["assignment_id"])[
        "is_locked"
    ]


def test_toggle_assignment_lock_unknown_assignment_is_404(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/nobody:1/lock",
        json={"locked": True},
    )
    assert resp.status_code == 404


# --------------------------------------------------- Rooms tab panel management


def _solved_run(client: TestClient, wsname: str) -> str:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    return client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]


def _free_division_and_room(client: TestClient, wsname: str, run_id: str) -> tuple[str, str]:
    """A (division, room) pair that has no panel yet — so `POST /panels`
    accepts it — favouring a division that already has interviews somewhere so
    a later move onto the new panel is legal."""
    body = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()
    rooms = sorted({p["room"] for p in body["panels"]})
    used = {(p["division"], p["room"]) for p in body["panels"]}
    placed = {p["division"] for p in body["panels"]}
    for room in rooms:
        for division in sorted(placed) + [d for d in body["divisions"] if d not in placed]:
            if (division, room) not in used:
                return division, room
    raise AssertionError("no free division/room pair in this run")


def test_add_panel_creates_an_empty_deletable_panel(client: TestClient, wsname: str) -> None:
    run_id = _solved_run(client, wsname)
    division, room = _free_division_and_room(client, wsname, run_id)

    resp = client.post(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels",
        json={"division": division, "room": room},
    )
    assert resp.status_code == 200, resp.text
    new_id = resp.json()["panel"]["panel_id"]

    panels = {p["panel_id"]: p for p in resp.json()["panels"]}
    assert new_id in panels
    assert panels[new_id]["interview_count"] == 0
    assert panels[new_id]["manual"] is True
    assert panels[new_id]["deletable"] is True
    assert panels[new_id]["room"] == room
    assert panels[new_id]["division"] == division

    # survives a re-fetch
    again = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()
    assert new_id in {p["panel_id"] for p in again["panels"]}


def test_add_panel_rejects_a_division_already_in_that_room(client: TestClient, wsname: str) -> None:
    run_id = _solved_run(client, wsname)
    existing = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()["panels"][0]

    resp = client.post(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels",
        json={"division": existing["division"], "room": existing["room"]},
    )
    assert resp.status_code == 409
    assert "room-exclusivity" in resp.json()["detail"]


def test_delete_empty_manual_panel_removes_it(client: TestClient, wsname: str) -> None:
    run_id = _solved_run(client, wsname)
    division, room = _free_division_and_room(client, wsname, run_id)
    new_id = client.post(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels",
        json={"division": division, "room": room},
    ).json()["panel"]["panel_id"]

    resp = client.delete(f"/api/workspaces/{wsname}/runs/{run_id}/panels/{new_id}")
    assert resp.status_code == 200, resp.text
    assert new_id not in {p["panel_id"] for p in resp.json()["panels"]}


def test_delete_panel_with_interviews_is_refused(client: TestClient, wsname: str) -> None:
    run_id = _solved_run(client, wsname)
    division, room = _free_division_and_room(client, wsname, run_id)
    new_id = client.post(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels",
        json={"division": division, "room": room},
    ).json()["panel"]["panel_id"]

    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    mover = next(r for r in rows if r["division"] == division)
    moved = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{mover['assignment_id']}",
        json={"panel_id": new_id, "slot_id": mover["slot_id"]},
    )
    assert moved.status_code == 200, moved.text

    panels = {
        p["panel_id"]: p
        for p in client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()["panels"]
    }
    assert panels[new_id]["interview_count"] == 1
    assert panels[new_id]["deletable"] is False

    resp = client.delete(f"/api/workspaces/{wsname}/runs/{run_id}/panels/{new_id}")
    assert resp.status_code == 409
    assert "still has 1 interview" in resp.json()["detail"]


def test_delete_rejects_a_non_manual_panel(client: TestClient, wsname: str) -> None:
    run_id = _solved_run(client, wsname)
    solver_panel = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()["panels"][0][
        "panel_id"
    ]
    resp = client.delete(f"/api/workspaces/{wsname}/runs/{run_id}/panels/{solver_panel}")
    assert resp.status_code == 404


# ------------------------------------------------ Rooms tab: move a panel's room


def _movable_panel_and_room(client: TestClient, wsname: str, run_id: str) -> tuple[dict, str]:
    """A solver panel with interviews plus a room, open on every day that panel
    runs, that carries no panel of the same division — a legal drag target."""
    panels = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()["panels"]
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    rooms_by_date: dict[str, set[str]] = {}
    for r in rows:
        rooms_by_date.setdefault(r["date"], set()).add(r["room"])
    for src in panels:
        if src["interview_count"] == 0:
            continue
        src_dates = {r["date"] for r in rows if r["panel_id"] == src["panel_id"]}
        open_rooms = set.intersection(*(rooms_by_date.get(d, set()) for d in src_dates))
        div_rooms = {p["room"] for p in panels if p["division"] == src["division"]}
        for target in sorted(open_rooms - div_rooms - {src["room"]}):
            return src, target
    raise AssertionError("no movable panel/room pair in this run")


def test_move_panel_relocates_it_with_every_interview(client: TestClient, wsname: str) -> None:
    run_id = _solved_run(client, wsname)
    src, target = _movable_panel_and_room(client, wsname, run_id)
    before = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    src_rows = [r for r in before if r["panel_id"] == src["panel_id"]]

    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels/{src['panel_id']}",
        json={"room": target},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved_interviews"] == len(src_rows)
    moved = {p["panel_id"]: p for p in body["panels"]}[src["panel_id"]]
    assert moved["room"] == target
    assert moved["interview_count"] == src["interview_count"]

    # Every interview follows the panel: new room, same panel id and slot.
    after = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    after_rows = [r for r in after if r["panel_id"] == src["panel_id"]]
    assert len(after_rows) == len(src_rows)
    assert all(r["room"] == target for r in after_rows)
    assert {r["slot_id"] for r in after_rows} == {r["slot_id"] for r in src_rows}

    # Survives a re-fetch of the panel list.
    again = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()["panels"]
    assert {p["panel_id"]: p["room"] for p in again}[src["panel_id"]] == target


def test_move_panel_rejects_a_room_already_running_that_division(
    client: TestClient, wsname: str
) -> None:
    run_id = _solved_run(client, wsname)
    src, target = _movable_panel_and_room(client, wsname, run_id)

    # Give `target` a panel of the same division (an empty manual one), so the
    # move would put two panels of one division in one room.
    added = client.post(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels",
        json={"division": src["division"], "room": target},
    )
    assert added.status_code == 200, added.text

    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels/{src['panel_id']}",
        json={"room": target},
    )
    assert resp.status_code == 409
    assert "room-exclusivity" in resp.json()["detail"]

    # Nothing moved: the panel is still in its original room.
    still = {p["panel_id"]: p["room"] for p in _panels(client, wsname, run_id)}
    assert still[src["panel_id"]] == src["room"]


def test_move_panel_ignores_the_4_panel_room_cap(client: TestClient, wsname: str) -> None:
    """A manual panel move may overload a room past max_concurrent_panels —
    consistent with add-panel and single-interview moves (Session G4)."""
    run_id = _solved_run(client, wsname)
    src, target = _movable_panel_and_room(client, wsname, run_id)

    # Fill `target` with manual panels of other divisions until it is at the cap.
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()
    divisions = rows["divisions"]
    in_target = {p["division"] for p in rows["panels"] if p["room"] == target}
    room_cfg_cap = 4
    fillers = [d for d in divisions if d not in in_target and d != src["division"]]
    while (
        sum(1 for p in _panels(client, wsname, run_id) if p["room"] == target) < room_cfg_cap
        and fillers
    ):
        client.post(
            f"/api/workspaces/{wsname}/runs/{run_id}/panels",
            json={"division": fillers.pop(), "room": target},
        )

    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/panels/{src['panel_id']}",
        json={"room": target},
    )
    assert resp.status_code == 200, resp.text
    assert {p["panel_id"]: p["room"] for p in resp.json()["panels"]}[src["panel_id"]] == target


def _panels(client: TestClient, wsname: str, run_id: str) -> list[dict]:
    return client.get(f"/api/workspaces/{wsname}/runs/{run_id}/panels").json()["panels"]


_THU_SLOTS = [
    "2026-09-17_1830",
    "2026-09-17_1850",
    "2026-09-17_1910",
    "2026-09-17_1930",
    "2026-09-17_1950",
    "2026-09-17_2010",
    "2026-09-17_2030",
    "2026-09-17_2050",
    "2026-09-17_2110",
]


def _write_concentrated_creative_applicants(wsname: str, n: int = 16) -> None:
    """Drop an applicants.clean.csv straight into the workspace: `n` applicants
    all wanting two CREATIVE roles, all free only on the Thursday evening. That
    per-day load is far past the 85% utilisation mark for one panel, so a real
    ``rebalance_panels`` split runs at solve time and CREATIVE-A2..k panels
    are created — no monkeypatch, the actual production path."""
    from datetime import datetime

    import pandas as pd

    from iff_scheduler import workspace as ws
    from iff_scheduler.ingest.validate import CLEAN_COLUMNS

    rows = [
        {
            "applicant_id": f"IFF-{i:04d}",
            "full_name": f"Creative Applicant {i}",
            "email": f"creative{i}@example.com",
            "phone": f"+62-812-{i:04d}",
            "student_id": f"S{i:04d}",
            "sub_division_1": "Design and Decor",
            "sub_division_2": "WebMaster",
            "division_1": "CREATIVE",
            "division_2": "CREATIVE",
            "single_choice": "False",
            "availability_slots": "|".join(_THU_SLOTS),
            "submitted_at": datetime(2026, 8, 15, 10, i).isoformat(),
            "notes": "",
        }
        for i in range(1, n + 1)
    ]
    path = ws.applicants_clean_path(wsname)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=CLEAN_COLUMNS).to_csv(path, index=False)


# CREATIVE's committed baseline is one panel per event day — CREATIVE-A1
# (Thursday) and CREATIVE-B1 (Friday), per config/panels.yaml. Any other
# CREATIVE panel a run carries was split off by `rebalance_panels` /
# `autoscale_panels` (id `CREATIVE-A2`, `CREATIVE-A3`, ...; origin="balanced").
_COMMITTED_CREATIVE_PANELS = {"CREATIVE-A1", "CREATIVE-B1"}


def _balanced_creative_panels(rows: list[dict]) -> list[str]:
    """The load-balanced CREATIVE panel ids present in `rows`, sorted."""
    return sorted(
        {
            r["panel_id"]
            for r in rows
            if r["panel_id"].startswith("CREATIVE-")
            and r["panel_id"] not in _COMMITTED_CREATIVE_PANELS
        }
    )


def _other_slot(rows: list[dict], target: dict) -> str:
    """The slot the target applicant's *other* interview sits in (so a move
    never double-books them, C3)."""
    other = next(
        (
            r
            for r in rows
            if r["applicant_id"] == target["applicant_id"]
            and r["assignment_id"] != target["assignment_id"]
        ),
        None,
    )
    return other["slot_id"] if other else ""


def test_move_onto_a_load_balanced_panel_is_accepted(client: TestClient, wsname: str) -> None:
    """Regression (FR-40..FR-42): every solve grows a hot division with extra
    panels — real ids like ``CREATIVE-A2`` — that never reach committed
    ``panels.yaml``. The run's assignments and the move UIs both carry those
    ids, so a manual move onto one must be validated against the run's own
    panel set. Previously it 422'd "Unknown panel" and no re-solve could clear
    it. Solve → move → assert it lands."""
    _create_ws(client, wsname)
    _write_concentrated_creative_applicants(wsname)

    solved = client.post(f"/api/workspaces/{wsname}/solve", json={})
    assert solved.status_code == 200, solved.text
    run_id = solved.json()["run_id"]

    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    seen = sorted({r["panel_id"] for r in rows})
    bal_ids = _balanced_creative_panels(rows)
    assert len(bal_ids) >= 2, f"expected a real rebalance split; panels seen: {seen}"

    # Move an interview that is sitting on a load-balanced panel to a different
    # load-balanced panel of the same division, at the slot its choice already
    # occupies (so the only thing under test is that the panel is recognised).
    src, dst = bal_ids[0], bal_ids[-1]
    target = next(r for r in rows if r["panel_id"] == src)
    occupied = {(r["panel_id"], r["slot_id"]) for r in rows}
    free_slot = next(
        s for s in _THU_SLOTS if (dst, s) not in occupied and s != _other_slot(rows, target)
    )

    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{target['assignment_id']}",
        json={"panel_id": dst, "slot_id": free_slot},
    )
    assert resp.status_code == 200, resp.text
    assert "Unknown panel" not in resp.text
    assert resp.json()["assignment"]["panel_id"] == dst
    assert resp.json()["locked"] is True


def test_move_onto_a_load_balanced_panel_survives_lost_run_metadata(
    client: TestClient, wsname: str
) -> None:
    """The panel set is also recoverable from the run's own assignments, so a
    move still validates when every record of the augmented panels is gone:
    a run solved before ``solved_panels`` was persisted (its ``autoscale.json``
    and DB metrics carry nothing the validator can use). This is the exact
    shape of the still-open bug report — a fresh solve alone did not fix it."""
    from iff_scheduler import workspace as ws

    _create_ws(client, wsname)
    _write_concentrated_creative_applicants(wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={}).json()["run_id"]

    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    bal_ids = _balanced_creative_panels(rows)
    assert bal_ids
    target = next(r for r in rows if r["panel_id"] == bal_ids[0])

    # Strip every record of the augmented panels, in whichever store is live,
    # while leaving the run itself intact.
    run_dir = ws.runs_dir(wsname) / run_id
    if run_dir.exists():
        (run_dir / "autoscale.json").unlink(missing_ok=True)
        metrics_path = run_dir / "metrics.json"
        m = json.loads(metrics_path.read_text())
        m.pop("solved_panels", None)
        metrics_path.write_text(json.dumps(m))
    from iff_scheduler.db import supabase_enabled

    if supabase_enabled():
        from api.dependencies import workspace_pk
        from iff_scheduler.db import run_repo

        row = run_repo.get_run(workspace_pk(wsname), run_id)
        stripped = {k: v for k, v in row["metrics"].items() if k != "solved_panels"}
        run_repo.update_run(row["id"], metrics=stripped)

    resp = client.patch(
        f"/api/workspaces/{wsname}/runs/{run_id}/assignments/{target['assignment_id']}",
        json={"panel_id": target["panel_id"], "slot_id": target["slot_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert "Unknown panel" not in resp.text
    assert resp.json()["assignment"]["panel_id"] == target["panel_id"]


def test_schedule_xlsx_renders_every_room_a_load_balanced_division_used(
    client: TestClient, wsname: str
) -> None:
    """Regression: publish resolved panels from committed `panels.yaml` alone,
    so a division `rebalance_panels`/`autoscale_panels` grew from one room to
    several (real ids like `CREATIVE-A2`, each in its own room, SPEC.md §5.5)
    rendered only its baseline room in schedule.xlsx — the extra rooms it
    actually used that day never appeared, not even empty. 16 applicants
    packed onto CREATIVE Thursday forces a real multi-panel, multi-room split
    at solve time; the exported sheet must show a room-panel column for every
    distinct room the run's own assignments actually used that day."""
    import io
    import zipfile

    from openpyxl import load_workbook

    _create_ws(client, wsname)
    _write_concentrated_creative_applicants(wsname)

    solved = client.post(f"/api/workspaces/{wsname}/solve", json={})
    assert solved.status_code == 200, solved.text
    run_id = solved.json()["run_id"]

    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    creative_rows = [r for r in rows if r["division"] == "CREATIVE"]
    thu_rooms = sorted({r["room"] for r in creative_rows if r["slot_id"] in _THU_SLOTS})
    assert len(thu_rooms) >= 2, f"expected a real multi-room split; rows: {creative_rows}"

    published = client.post(
        f"/api/workspaces/{wsname}/publish", json={"run": "latest", "formats": ["xlsx"]}
    )
    assert published.status_code == 200, published.text

    resp = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/xlsx")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        wb = load_workbook(io.BytesIO(zf.read("schedule.xlsx")))
    ws_creative = wb["CREATIVE"]
    header = [c.value for c in ws_creative[2]]
    rendered_rooms = {v for v in header if isinstance(v, str) and v.startswith("Room ")}
    assert rendered_rooms == {f"Room {r}" for r in thu_rooms}


# --------------------------------------------------------------- rename / delete


def test_rename_workspace_moves_its_data_with_it(client: TestClient, wsname: str) -> None:
    new_name = f"{wsname}-renamed"
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)

    renamed = client.patch(f"/api/workspaces/{wsname}", json={"name": new_name})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == new_name

    assert client.get(f"/api/workspaces/{wsname}").status_code == 404
    assert client.get(f"/api/workspaces/{new_name}").status_code == 200
    # The applicants moved too, so the pipeline still works under the new name.
    status = client.get(f"/api/workspaces/{new_name}/ingest-status")
    assert status.status_code == 200
    assert status.json()["ingested"] is True


def test_rename_onto_an_existing_name_is_a_409(client: TestClient, wsname: str) -> None:
    one, two = wsname, f"{wsname}-b"
    _create_ws(client, one)
    _create_ws(client, two)
    clash = client.patch(f"/api/workspaces/{one}", json={"name": two})
    assert clash.status_code == 409
    assert "already exists" in clash.json()["detail"]


def test_rename_unknown_workspace_is_a_404(client: TestClient) -> None:
    missing = client.patch("/api/workspaces/nope", json={"name": "whatever"})
    assert missing.status_code == 404


def test_iff_submissions_workspace_can_be_deleted(client: TestClient, wsname: str) -> None:
    """No workspace is special any more: the "IFF Submissions" group carries
    no delete protection now that live Google Sheets ingest is gone."""
    _create_ws(client, wsname, group="IFF Submissions")
    deleted = client.delete(f"/api/workspaces/{wsname}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": wsname}
    assert client.get(f"/api/workspaces/{wsname}").status_code == 404


def test_an_iff_submissions_workspace_can_be_renamed(client: TestClient, wsname: str) -> None:
    new_name = f"{wsname}-renamed"
    _create_ws(client, wsname, group="IFF Submissions")
    renamed = client.patch(f"/api/workspaces/{wsname}", json={"name": new_name})
    assert renamed.status_code == 200
    assert renamed.json()["group"] == "IFF Submissions"


# ------------------------------------------------------- name disguise (reveal)

_TEST_PASSWORD = "open-sesame"


def _solved_run(client: TestClient, name: str) -> str:
    _create_ws(client, name)
    _ingest_fixture(client, name)
    solved = client.post(f"/api/workspaces/{name}/solve", json={"skip_check": True})
    assert solved.status_code == 200, solved.text
    return str(solved.json()["run_id"])


def _reveal_headers(client: TestClient, workspace_id: str) -> dict[str, str]:
    resp = client.post(
        f"/api/workspaces/{workspace_id}/reveal", json={"password": _TEST_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return {"X-Reveal-Token": resp.json()["token"]}


def test_names_are_not_disguised_when_no_password_is_configured(
    client: TestClient, wsname: str
) -> None:
    run_id = _solved_run(client, wsname)
    reveal_url = f"/api/workspaces/{wsname}/reveal"
    assert client.get(reveal_url).json() == {"masking_enabled": False, "revealed": True}
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    assert not any(r["full_name"].startswith("CAND") for r in rows)
    assert client.post(reveal_url, json={"password": "anything"}).status_code == 409


def test_a_workspace_not_on_the_masked_list_is_never_disguised(
    client: TestClient, wsname: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Masking is per-workspace: a password configured for the deployment
    protects only the named workspace(s) — any other, including a brand
    new one, always shows real names."""
    run_id = _solved_run(client, wsname)
    monkeypatch.setenv("REVEAL_PASSWORD", _TEST_PASSWORD)
    monkeypatch.setenv("MASKED_WORKSPACE_NAMES", "some-other-workspace")

    reveal_url = f"/api/workspaces/{wsname}/reveal"
    assert client.get(reveal_url).json() == {"masking_enabled": False, "revealed": True}
    rows = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/assignments").json()
    assert not any(r["full_name"].startswith("CAND") for r in rows)
    assert client.post(reveal_url, json={"password": _TEST_PASSWORD}).status_code == 409


def test_unverified_viewer_only_ever_sees_aliases(
    client: TestClient, wsname: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import re

    run_id = _solved_run(client, wsname)
    base = f"/api/workspaces/{wsname}"
    real = client.get(f"{base}/runs/{run_id}/assignments").json()
    real_rejected = client.get(f"{base}/rejected").json()
    secrets = {r["full_name"] for r in real} | {r["email"] for r in real}
    secrets |= {r["full_name"] for r in real_rejected} | {r["email"] for r in real_rejected}
    secrets.discard("")

    monkeypatch.setenv("REVEAL_PASSWORD", _TEST_PASSWORD)
    monkeypatch.setenv("MASKED_WORKSPACE_NAMES", wsname)
    assert client.get(f"{base}/reveal").json() == {"masking_enabled": True, "revealed": False}

    masked = client.get(f"{base}/runs/{run_id}/assignments")
    assert masked.status_code == 200
    rows = masked.json()
    assert len(rows) == len(real)
    assert all(re.fullmatch(r"CAND\d{4}", r["full_name"]) for r in rows)
    assert {r["email"] for r in rows} == {"hidden"}
    # The alias follows the applicant, so both of their interviews share it
    # and everything that is not identity is untouched.
    assert {(r["applicant_id"], r["full_name"]) for r in rows} == {
        (r["applicant_id"], f"CAND{int(r['applicant_id'][1:]):04d}") for r in real
    }
    assert [(r["assignment_id"], r["panel_id"], r["slot_id"]) for r in rows] == [
        (r["assignment_id"], r["panel_id"], r["slot_id"]) for r in real
    ]

    rejected = client.get(f"{base}/rejected")
    target = rows[0]
    locked = client.patch(
        f"{base}/runs/{run_id}/assignments/{target['assignment_id']}/lock",
        json={"locked": True},
    )
    assert locked.status_code == 200, locked.text
    assert locked.json()["assignment"]["full_name"] == target["full_name"]
    reingested = _ingest_fixture(client, wsname)
    assert reingested.status_code == 200

    for resp in (masked, rejected, locked, reingested):
        leaked = [s for s in secrets if s in resp.text]
        assert not leaked, f"{resp.request.url} leaked {leaked}"


def test_password_reveals_real_names(
    client: TestClient, wsname: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _solved_run(client, wsname)
    url = f"/api/workspaces/{wsname}/runs/{run_id}/assignments"
    reveal_url = f"/api/workspaces/{wsname}/reveal"
    real = client.get(url).json()

    monkeypatch.setenv("REVEAL_PASSWORD", _TEST_PASSWORD)
    monkeypatch.setenv("MASKED_WORKSPACE_NAMES", wsname)
    wrong = client.post(reveal_url, json={"password": "not-it"})
    assert wrong.status_code == 401
    assert "token" not in wrong.json()

    headers = _reveal_headers(client, wsname)
    assert client.get(reveal_url, headers=headers).json() == {
        "masking_enabled": True,
        "revealed": True,
    }
    assert client.get(url, headers=headers).json() == real

    # A made-up token, and one minted under a since-changed password, reveal nothing.
    forged = client.get(url, headers={"X-Reveal-Token": "9999999999.deadbeef"}).json()
    assert all(r["full_name"].startswith("CAND") for r in forged)
    monkeypatch.setenv("REVEAL_PASSWORD", "rotated")
    stale = client.get(url, headers=headers).json()
    assert all(r["full_name"].startswith("CAND") for r in stale)


def test_reveal_token_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import privacy

    monkeypatch.setenv("REVEAL_PASSWORD", _TEST_PASSWORD)
    token, expires_at = privacy.issue_token(now=1_000)
    assert expires_at == 1_000 + privacy.TOKEN_TTL_SECONDS
    assert privacy.token_valid(token, now=expires_at - 1)
    assert not privacy.token_valid(token, now=expires_at)
    assert not privacy.token_valid(None)
    assert not privacy.token_valid("garbage")


def test_disguised_report_row_scrubs_the_message_too() -> None:
    from api.privacy import disguise_report_row

    row = {
        "row_number": 6,
        "csv_row": 7,
        "full_name": "Eka Putri",
        "email": "eka-at-example.com",
        "reason_code": "INVALID_EMAIL",
        "message": "'eka-at-example.com' is not a valid email address.",
    }
    masked = disguise_report_row(row)
    assert masked["full_name"] == "ROW0007"
    assert masked["email"] == "hidden"
    assert masked["message"] == "'hidden' is not a valid email address."
    # A blank stays blank, so "missing name" still reads as missing.
    assert disguise_report_row({**row, "full_name": ""})["full_name"] == ""


def test_unverified_download_carries_aliases_and_leaves_real_exports_alone(
    client: TestClient, wsname: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import zipfile

    from openpyxl import load_workbook

    from iff_scheduler import workspace as ws

    def names_in(content: bytes) -> set[str]:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            assert sorted(zf.namelist()) == ["applicants.xlsx", "rooms.xlsx", "schedule.xlsx"]
            sheet = load_workbook(io.BytesIO(zf.read("applicants.xlsx"))).active
        return {str(row[1]) for row in sheet.iter_rows(min_row=2, values_only=True)}

    run_id = _solved_run(client, wsname)
    url = f"/api/workspaces/{wsname}/runs/{run_id}/xlsx"
    real_names = names_in(client.get(url).content)
    published = ws.output_dir(wsname) / run_id / "applicants.xlsx"
    before = published.read_bytes()

    monkeypatch.setenv("REVEAL_PASSWORD", _TEST_PASSWORD)
    monkeypatch.setenv("MASKED_WORKSPACE_NAMES", wsname)
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    aliases = names_in(resp.content)
    assert len(aliases) == len(real_names)
    assert all(n.startswith("CAND") for n in aliases)
    assert not aliases & real_names
    assert published.read_bytes() == before

    assert (
        names_in(client.get(url, headers=_reveal_headers(client, wsname)).content)
        == real_names
    )


# ------------------------------------------- preferences survive a wiped host


def test_preference_column_survives_losing_the_applicant_file(
    client: TestClient, wsname: str
) -> None:
    """The API host's disk is wiped on every redeploy, taking
    `applicants.clean.csv` with it. The run records what each applicant
    declared at solve time, so the Preference column must not go blank."""
    from iff_scheduler import workspace as ws

    run_id = _solved_run(client, wsname)
    url = f"/api/workspaces/{wsname}/runs/{run_id}/assignments"
    before = client.get(url).json()
    assert all(r["declared_availability"] for r in before)

    ws.applicants_clean_path(wsname).unlink()
    after = client.get(url).json()
    assert [(r["declared_availability"], r["availability_slots"]) for r in after] == [
        (r["declared_availability"], r["availability_slots"]) for r in before
    ]


def _record_availability(name: str, run_id: str, applicant_id: str, slots: list[str]) -> None:
    """Overwrite one applicant's entry in the run's availability record, in
    whichever store is live."""
    from iff_scheduler import workspace as ws
    from iff_scheduler.db import supabase_enabled

    if supabase_enabled():
        from api.dependencies import workspace_pk
        from iff_scheduler.db import run_repo

        row = run_repo.get_run(workspace_pk(name), run_id)
        assert row is not None
        metrics = dict(row["metrics"])
        metrics["applicant_availability"] = {
            **metrics["applicant_availability"],
            applicant_id: slots,
        }
        run_repo.update_run(row["id"], metrics=metrics)
        return
    path = ws.runs_dir(name) / run_id / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    metrics["applicant_availability"][applicant_id] = slots
    path.write_text(json.dumps(metrics), encoding="utf-8")


def test_a_move_is_checked_against_the_runs_own_availability_record(
    client: TestClient, wsname: str
) -> None:
    """Without the applicant file a move used to keep whatever clash flag the
    interview already had. It is now judged against the run's record: outside
    the recorded slots sets the flag, back inside clears it."""
    from iff_scheduler import workspace as ws

    run_id = _solved_run(client, wsname)
    base = f"/api/workspaces/{wsname}/runs/{run_id}"
    rows = client.get(f"{base}/assignments").json()
    ws.applicants_clean_path(wsname).unlink()

    # An interview with a free slot later the same evening on its own panel.
    taken = {(r["panel_id"], r["slot_id"]) for r in rows}
    target, dest = next(
        (r, s)
        for r in rows
        for s in r["availability_slots"]
        if s.startswith(r["date"])
        and (r["panel_id"], s) not in taken
        and s not in {o["slot_id"] for o in rows if o["applicant_id"] == r["applicant_id"]}
    )
    assert target["is_clash"] is False

    # The applicant is now on record as free for nothing but where they sit.
    _record_availability(wsname, run_id, target["applicant_id"], [target["slot_id"]])
    listed = client.get(f"{base}/assignments").json()
    assert {
        tuple(r["availability_slots"])
        for r in listed
        if r["applicant_id"] == target["applicant_id"]
    } == {(target["slot_id"],)}

    moved = client.patch(
        f"{base}/assignments/{target['assignment_id']}",
        json={"panel_id": target["panel_id"], "slot_id": dest},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["assignment"]["is_clash"] is True

    back = client.patch(
        f"{base}/assignments/{target['assignment_id']}",
        json={"panel_id": target["panel_id"], "slot_id": target["slot_id"]},
    )
    assert back.status_code == 200, back.text
    assert back.json()["assignment"]["is_clash"] is False


def test_backfill_availability_fills_gaps_but_never_from_the_wrong_roster(
    client: TestClient, wsname: str
) -> None:
    from api.cli_helpers import load_assignments, load_clean_applicants
    from api.services import backfill_availability
    from iff_scheduler import workspace as ws

    run_id = _solved_run(client, wsname)
    assignments = load_assignments(ws.runs_dir(wsname) / run_id / "assignments.csv")
    roster = {a.applicant_id: a for a in load_clean_applicants(ws.applicants_clean_path(wsname))}
    full = {
        i: list(a.availability_slots)
        for i, a in roster.items()
        if i in {x.applicant_id for x in assignments}
    }

    # Nothing recorded yet: the whole roster is filled in.
    assert backfill_availability({}, assignments, roster) == full
    # Already complete: nothing to do.
    assert backfill_availability(full, assignments, roster) is None
    # An entry the run already carries (a hand correction) wins over the CSV.
    someone = assignments[0].applicant_id
    assert backfill_availability({someone: ["corrected"]}, assignments, roster) == {
        **full,
        someone: ["corrected"],
    }
    # Same ids, different people (another CSV): refuse rather than mis-attach.
    other = assignments[-1].applicant_id
    assert other != someone
    swapped = {**roster, someone: roster[other].model_copy(update={"applicant_id": someone})}
    assert backfill_availability({}, assignments, swapped) is None
    # A roster that lacks someone the run scheduled is not this run's roster.
    assert backfill_availability({}, assignments, {someone: roster[someone]}) is None


def test_reimporting_the_csv_restores_preferences_on_runs_that_lost_them(
    client: TestClient, wsname: str
) -> None:
    """Postgres only: a run solved before availability was recorded with it
    gets the record back the next time the same CSV is imported."""
    from iff_scheduler.db import supabase_enabled

    if not supabase_enabled():
        pytest.skip("the backfill only exists for the Postgres store")
    from api.dependencies import workspace_pk
    from iff_scheduler import workspace as ws
    from iff_scheduler.db import run_repo

    run_id = _solved_run(client, wsname)
    url = f"/api/workspaces/{wsname}/runs/{run_id}/assignments"
    before = client.get(url).json()

    # Put the run in the state production was found in: no record, no file.
    row = run_repo.get_run(workspace_pk(wsname), run_id)
    assert row is not None
    metrics = dict(row["metrics"])
    metrics.pop("applicant_availability")
    someone = before[0]["applicant_id"]
    run_repo.update_run(row["id"], metrics={**metrics, "applicant_availability": {someone: []}})
    ws.applicants_clean_path(wsname).unlink()
    blank = client.get(url).json()
    assert not any(r["declared_availability"] for r in blank)

    assert _ingest_fixture(client, wsname).status_code == 200
    ws.applicants_clean_path(wsname).unlink()
    restored = client.get(url).json()
    for was, now in zip(before, restored, strict=True):
        if now["applicant_id"] == someone:
            assert now["availability_slots"] == []  # the existing entry is kept
        else:
            assert now["availability_slots"] == was["availability_slots"]
            assert now["declared_availability"] == was["declared_availability"]
