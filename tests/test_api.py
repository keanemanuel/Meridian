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
    /api/workspaces/{id}`` so a protected "IFF Submissions" workspace — which
    that route refuses (403) — is still cleaned up. Best-effort: a failure
    here must never fail the test that already passed.
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


@pytest.fixture
def service_account_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A well-formed (entirely fake) service account key on disk.

    The export resolves and shape-checks the key before calling Google, so a
    placeholder like /dev/null is now correctly refused; these tests need a
    file that actually looks like a key."""
    from tests.test_credentials_bootstrap import KEY_JSON

    path = tmp_path / "service_account.json"
    path.write_text(KEY_JSON, encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(path))
    return path


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
    assert one.json()["sheet_id"] is None

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


def test_create_workspace_extracts_sheet_id_from_url(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wsname: str
) -> None:
    """POST /api/workspaces accepts an optional `sheet_url` and stores just
    the extracted ID, so the UI can link a Sheet at creation time."""
    monkeypatch.setattr("api.routers.workspaces.supabase_enabled", lambda: False)
    url = "https://docs.google.com/spreadsheets/d/1AbC_dE-fG123456/edit#gid=0"
    resp = client.post(
        "/api/workspaces",
        json={"name": wsname, "group": "Test Environment", "sheet_url": url},
    )
    assert resp.status_code == 201
    assert resp.json()["sheet_id"] == "1AbC_dE-fG123456"


def test_create_workspace_blank_sheet_url_links_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wsname: str
) -> None:
    monkeypatch.setattr("api.routers.workspaces.supabase_enabled", lambda: False)
    resp = client.post(
        "/api/workspaces",
        json={"name": wsname, "group": "Test Environment", "sheet_url": "   "},
    )
    assert resp.status_code == 201
    assert resp.json()["sheet_id"] is None


def test_patch_workspace_sheet_links_and_replaces(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wsname: str
) -> None:
    monkeypatch.setattr("api.routers.workspaces.supabase_enabled", lambda: False)
    assert _create_ws(client, wsname).status_code == 201

    first = client.patch(
        f"/api/workspaces/{wsname}/sheet",
        json={"sheet_url": "https://docs.google.com/spreadsheets/d/SHEET_ONE/edit"},
    )
    assert first.status_code == 200
    assert first.json()["sheet_id"] == "SHEET_ONE"

    second = client.patch(
        f"/api/workspaces/{wsname}/sheet",
        json={"sheet_url": "https://docs.google.com/spreadsheets/d/SHEET_TWO/edit"},
    )
    assert second.json()["sheet_id"] == "SHEET_TWO"


def test_patch_workspace_sheet_unknown_workspace_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.routers.workspaces.supabase_enabled", lambda: False)
    resp = client.patch(
        "/api/workspaces/ghost/sheet",
        json={"sheet_url": "https://docs.google.com/spreadsheets/d/ID/edit"},
    )
    assert resp.status_code == 404


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


def test_check_before_ingest_is_404(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    resp = client.post(f"/api/workspaces/{wsname}/check")
    assert resp.status_code == 404
    assert "detail" in resp.json()


def test_ingest_status_reflects_whether_applicants_exist(client: TestClient, wsname: str) -> None:
    """The UI gates Check / Schedule on this so it never fires them just to
    get "Run ingest first" back."""
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


def test_check_after_ingest_returns_advisor_table(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    resp = client.post(f"/api/workspaces/{wsname}/check")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["feasible"], bool)
    assert len(body["rows"]) >= 1
    assert {"division", "demand", "verdict"} <= set(body["rows"][0])


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
    ``rebalance_panels`` split runs at solve time and CREATIVE-BAL-1..k panels
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
    panels — real ids like ``CREATIVE-BAL-1`` — that never reach committed
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
    bal_ids = sorted(p for p in seen if "-BAL-" in p)
    assert bal_ids, f"expected a real rebalance split; panels seen: {seen}"

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
    bal_ids = sorted({r["panel_id"] for r in rows if "-BAL-" in r["panel_id"]})
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


# --------------------------------------------------------------- notify


def test_notify_invite_preview_renders(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]

    resp = client.post(f"/api/workspaces/{wsname}/runs/{run_id}/notify/invite/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["auto_sendable"] + body["held_for_manual"] == body["total"]


def test_notify_result_preview_without_scores_is_404(client: TestClient, wsname: str) -> None:
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    run_id = client.post(f"/api/workspaces/{wsname}/solve", json={"skip_check": True}).json()[
        "run_id"
    ]

    resp = client.post(f"/api/workspaces/{wsname}/runs/{run_id}/notify/result/preview")
    assert resp.status_code == 404
    assert "scores" in resp.json()["detail"].lower()


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
    assert client.post(f"/api/workspaces/{new_name}/check").status_code == 200


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


def test_live_submission_workspaces_cannot_be_deleted(client: TestClient, wsname: str) -> None:
    """The UI hides the button; the rule is enforced here so it cannot be
    clicked past from outside the UI."""
    _create_ws(client, wsname, group="IFF Submissions")
    refused = client.delete(f"/api/workspaces/{wsname}")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Live submission workspaces cannot be deleted."
    assert client.get(f"/api/workspaces/{wsname}").status_code == 200


def test_a_live_submission_workspace_can_still_be_renamed(client: TestClient, wsname: str) -> None:
    new_name = f"{wsname}-renamed"
    _create_ws(client, wsname, group="IFF Submissions")
    renamed = client.patch(f"/api/workspaces/{wsname}", json={"name": new_name})
    assert renamed.status_code == 200
    assert renamed.json()["group"] == "IFF Submissions"


# --------------------------------------------------------------- sheets export


def test_export_without_service_account_credentials_is_a_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wsname: str
) -> None:
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")

    refused = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")
    assert refused.status_code == 409
    assert "GOOGLE_SERVICE_ACCOUNT_FILE" in refused.json()["detail"]


def test_export_with_a_doubled_credential_paste_still_works(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wsname: str
) -> None:
    """The live failure: the key was pasted twice into the env var, json.load
    raised "Extra data: line 14 column 1 (char 2364)", and because that is a
    ValueError it came back as a bare 400 with no hint it was about
    credentials at all."""
    from tests.test_credentials_bootstrap import KEY_JSON
    from tests.test_sheets_export import _FakeClient

    import api.credentials_bootstrap as bootstrap
    from api.credentials_bootstrap import materialize_json_credentials

    monkeypatch.setattr(bootstrap, "_TMP_DIR", str(tmp_path / "creds"))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", KEY_JSON + KEY_JSON)
    bootstrap._PROBLEMS.clear()
    materialize_json_credentials()
    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: _FakeClient())

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")

    exported = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")
    assert exported.status_code == 200
    assert exported.json()["sheet_url"].startswith("https://docs.google.com/spreadsheets/d/")


def test_export_response_is_one_clean_json_object(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """`sheet_url` is always present, the body parses in one pass, and there
    is nothing after the closing brace."""
    from tests.test_sheets_export import _FakeClient

    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: _FakeClient())

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")
    exported = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")

    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/json")
    body, consumed = json.JSONDecoder().raw_decode(exported.text)
    assert consumed == len(exported.text), "response must be exactly one JSON document"
    assert set(body) == {
        "sheet_url",
        "sheet_id",
        "tabs",
        "rows_written",
        "clashes",
        "folder_id",
    }
    assert body["sheet_url"].startswith("https://docs.google.com/spreadsheets/d/")
    assert body["folder_id"] is None


def test_export_reports_a_disabled_google_api_as_an_actionable_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """The second live blocker: the service account's project had the Sheets
    API on but the Drive API off, so creating the spreadsheet 403'd. Passing
    Google's paragraph straight through reads like a silent failure."""

    class _DriveDisabled:
        def create(self, title: str, folder_id: str | None = None) -> None:
            raise RuntimeError(
                "APIError: [403]: Google Drive API has not been used in project "
                "198021261604 before or it is disabled. Enable it by visiting ..."
            )

    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: _DriveDisabled())

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")
    failed = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")

    assert failed.status_code == 409
    detail = failed.json()["detail"]
    assert "Google Drive API is not enabled" in detail
    assert "project=198021261604" in detail


def test_export_creates_the_spreadsheet_inside_the_configured_drive_folder(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """GOOGLE_DRIVE_FOLDER_ID, when set, is passed through to gspread's
    create() and echoed back in the response so the committee can confirm
    the export landed where they expect."""
    from tests.test_sheets_export import _FakeClient

    fake = _FakeClient()
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "1uB8vmBvYeQIdjhsKY--qfVdSKaDyNKqy")
    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: fake)

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")
    exported = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")

    assert exported.status_code == 200
    assert exported.json()["folder_id"] == "1uB8vmBvYeQIdjhsKY--qfVdSKaDyNKqy"
    assert fake.created[0].folder_id == "1uB8vmBvYeQIdjhsKY--qfVdSKaDyNKqy"


def test_a_blank_drive_folder_id_behaves_as_unset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """A dashboard that leaves the variable present but empty must not send
    an empty-string folder id to Google."""
    from tests.test_sheets_export import _FakeClient

    fake = _FakeClient()
    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "   ")
    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: fake)

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")
    exported = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")

    assert exported.json()["folder_id"] is None
    assert fake.created[0].folder_id is None


def test_storage_quota_error_without_a_folder_configured_names_the_env_var(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """The reported bug: exporting to the service account's own Drive root
    always 403s with storageQuotaExceeded, because a bare service account has
    no personal Drive storage — a platform limit, not a quota that filled up."""

    class _NoStorage:
        def create(self, title: str, folder_id: str | None = None) -> None:
            raise RuntimeError(
                "APIError: [403]: The user's Drive storage quota has been "
                "exceeded. storageQuotaExceeded"
            )

    monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: _NoStorage())

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")
    failed = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")

    assert failed.status_code == 409
    detail = failed.json()["detail"]
    assert "GOOGLE_DRIVE_FOLDER_ID" in detail
    assert "Shared Drive" in detail


def test_storage_quota_error_with_a_folder_configured_blames_the_folder_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """If the quota error persists with a folder set, an ordinary "My Drive"
    folder shared as Editor is the near-universal cause — Drive bills
    storage to the file's creator, not to the parent folder's owner."""

    class _NoStorage:
        def create(self, title: str, folder_id: str | None = None) -> None:
            raise RuntimeError(
                "APIError: [403]: The user's Drive storage quota has been "
                "exceeded. storageQuotaExceeded"
            )

    monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "some-folder-id")
    monkeypatch.setattr("api.routers.export.open_export_client", lambda _p: _NoStorage())

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")
    failed = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")

    assert failed.status_code == 409
    detail = failed.json()["detail"]
    assert "Shared Drive" in detail
    assert "member" in detail


def test_a_malformed_server_side_json_file_is_not_reported_as_a_bad_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, wsname: str
) -> None:
    """A JSONDecodeError is a ValueError, so main.py's handler used to answer
    400 with its bare message. That is what "Extra data: line 14 column 1"
    looked like to the user."""

    def _boom() -> str:
        raise json.JSONDecodeError("Extra data", "{}x", 2)

    monkeypatch.setattr("api.routers.export.service_account_file", _boom)
    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    client.post(f"/api/workspaces/{wsname}/solve")

    failed = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")
    assert failed.status_code == 500
    assert "credentials or configuration problem" in failed.json()["detail"]


def test_export_builds_a_shared_sheet_from_the_latest_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, service_account_key: Path, wsname: str
) -> None:
    """End to end over HTTP with gspread faked out: the point is that the run
    resolves, the tabs come out per day, and the Sheet URL comes back."""
    from tests.test_sheets_export import _FakeClient

    fake = _FakeClient()
    monkeypatch.setattr("api.routers.export.open_export_client", lambda _path: fake)

    _create_ws(client, wsname)
    _ingest_fixture(client, wsname)
    solved = client.post(f"/api/workspaces/{wsname}/solve")
    assert solved.status_code == 200

    exported = client.post(f"/api/workspaces/{wsname}/runs/latest/export/sheets")
    assert exported.status_code == 200
    body = exported.json()
    assert body["sheet_url"] == "https://docs.google.com/spreadsheets/d/sheet-123"
    assert body["tabs"]
    assert body["rows_written"] > 0
    assert fake.created[0].shares == [(None, "anyone", "reader", False)]


def test_export_of_an_unknown_run_is_a_404(
    client: TestClient, service_account_key: Path, wsname: str
) -> None:
    _create_ws(client, wsname)
    missing = client.post(f"/api/workspaces/{wsname}/runs/2020-01-01T00-00-00/export/sheets")
    assert missing.status_code == 404
