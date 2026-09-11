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


def test_download_bundle_before_publish_is_409(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    resp = client.get(f"/api/workspaces/{wsname}/runs/{run_id}/xlsx")
    assert resp.status_code == 409


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
