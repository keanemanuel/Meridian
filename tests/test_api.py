"""Endpoint tests for the FastAPI wrapper (beta, SPEC.md §14).

The API is a thin shell over the alpha core, so these tests check the shell:
routing, status codes, the {detail: ...} error shape, and that a run
produced through HTTP is the same shape the CLI writes. The scheduling logic
itself is covered by the `iff_scheduler` test suite.

Isolation: every test chdirs into a tmp dir, so `data/workspaces/...` (all
relative paths in `iff_scheduler.workspace`) lands under tmp and never
touches the real repo data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import app

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "applicants_raw.csv"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    return TestClient(app)


def _create_ws(client: TestClient, name: str = "beta-test", group: str = "Test Environment"):
    return client.post("/api/workspaces", json={"name": name, "group": group})


def _ingest_fixture(client: TestClient, name: str = "beta-test"):
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


# --------------------------------------------------------------- workspaces


def test_workspace_crud_lifecycle(client: TestClient) -> None:
    created = _create_ws(client)
    assert created.status_code == 201
    assert created.json()["name"] == "beta-test"
    assert created.json()["group"] == "Test Environment"

    listed = client.get("/api/workspaces")
    assert listed.status_code == 200
    assert [w["name"] for w in listed.json()] == ["beta-test"]

    one = client.get("/api/workspaces/beta-test")
    assert one.status_code == 200
    assert one.json()["sheet_id"] is None

    deleted = client.delete("/api/workspaces/beta-test")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": "beta-test"}

    assert client.get("/api/workspaces/beta-test").status_code == 404


def test_create_duplicate_workspace_conflicts(client: TestClient) -> None:
    assert _create_ws(client).status_code == 201
    dup = _create_ws(client)
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]


def test_unknown_workspace_returns_detail_404(client: TestClient) -> None:
    resp = client.get("/api/workspaces/nope")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Workspace 'nope' not found."}


# --------------------------------------------------------------- pipeline


def test_ingest_csv_upload(client: TestClient) -> None:
    _create_ws(client)
    resp = _ingest_fixture(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["applicants"] >= 1
    assert "report" in body


def test_check_before_ingest_is_404(client: TestClient) -> None:
    _create_ws(client)
    resp = client.post("/api/workspaces/beta-test/check")
    assert resp.status_code == 404
    assert "detail" in resp.json()


def test_check_after_ingest_returns_advisor_table(client: TestClient) -> None:
    _create_ws(client)
    _ingest_fixture(client)
    resp = client.post("/api/workspaces/beta-test/check")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["feasible"], bool)
    assert len(body["rows"]) >= 1
    assert {"division", "demand", "verdict"} <= set(body["rows"][0])


def test_solve_publish_and_assignments_flow(client: TestClient) -> None:
    _create_ws(client)
    _ingest_fixture(client)

    solved = client.post("/api/workspaces/beta-test/solve", json={"skip_check": True})
    assert solved.status_code == 200, solved.text
    run_id = solved.json()["run_id"]
    assert solved.json()["interviews_placed"] == solved.json()["interviews_required"]

    runs = client.get("/api/workspaces/beta-test/runs")
    assert runs.status_code == 200
    assert [r["run_id"] for r in runs.json()] == [run_id]

    detail = client.get(f"/api/workspaces/beta-test/runs/{run_id}")
    assert detail.status_code == 200
    assert "assignments.csv" in detail.json()["files"]
    assert detail.json()["metrics"]["run_id"] == run_id

    published = client.post(
        "/api/workspaces/beta-test/publish", json={"run": "latest", "formats": ["html"]}
    )
    assert published.status_code == 200
    assert published.json()["applicants"] >= 1

    assignments = client.get(f"/api/workspaces/beta-test/runs/{run_id}/assignments")
    assert assignments.status_code == 200
    rows = assignments.json()
    assert len(rows) == solved.json()["interviews_required"]
    assert all(":" in r["assignment_id"] for r in rows)


def test_solve_without_applicants_is_404(client: TestClient) -> None:
    _create_ws(client)
    resp = client.post("/api/workspaces/beta-test/solve", json={"skip_check": True})
    assert resp.status_code == 404


# --------------------------------------------------------------- schedule edits


def test_patch_assignment_rejects_unknown_slot(client: TestClient) -> None:
    _create_ws(client)
    _ingest_fixture(client)
    run_id = client.post("/api/workspaces/beta-test/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    rows = client.get(f"/api/workspaces/beta-test/runs/{run_id}/assignments").json()
    target = rows[0]["assignment_id"]

    resp = client.patch(
        f"/api/workspaces/beta-test/runs/{run_id}/assignments/{target}",
        json={"panel_id": rows[0]["panel_id"], "slot_id": "NOT-A-SLOT"},
    )
    assert resp.status_code == 422
    assert "detail" in resp.json()


def test_patch_assignment_locks_and_survives_resolve(client: TestClient) -> None:
    _create_ws(client)
    _ingest_fixture(client)
    run_id = client.post("/api/workspaces/beta-test/solve", json={"skip_check": True}).json()[
        "run_id"
    ]
    rows = client.get(f"/api/workspaces/beta-test/runs/{run_id}/assignments").json()

    # Move the first interview onto its own current panel + slot: a no-op
    # placement that is always legal, but still records a lock.
    first = rows[0]
    resp = client.patch(
        f"/api/workspaces/beta-test/runs/{run_id}/assignments/{first['assignment_id']}",
        json={"panel_id": first["panel_id"], "slot_id": first["slot_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is True
    assert resp.json()["assignment"]["is_locked"] is True

    resolved = client.post(f"/api/workspaces/beta-test/runs/{run_id}/resolve", json={})
    assert resolved.status_code == 200
    new_run = resolved.json()["run_id"]
    new_rows = client.get(f"/api/workspaces/beta-test/runs/{new_run}/assignments").json()
    locked = next(r for r in new_rows if r["assignment_id"] == first["assignment_id"])
    assert locked["panel_id"] == first["panel_id"]
    assert locked["slot_id"] == first["slot_id"]
    assert resolved.json()["locked"] >= 1


# --------------------------------------------------------------- notify


def test_notify_invite_preview_renders(client: TestClient) -> None:
    _create_ws(client)
    _ingest_fixture(client)
    run_id = client.post("/api/workspaces/beta-test/solve", json={"skip_check": True}).json()[
        "run_id"
    ]

    resp = client.post(f"/api/workspaces/beta-test/runs/{run_id}/notify/invite/preview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["auto_sendable"] + body["held_for_manual"] == body["total"]


def test_notify_result_preview_without_scores_is_404(client: TestClient) -> None:
    _create_ws(client)
    _ingest_fixture(client)
    run_id = client.post("/api/workspaces/beta-test/solve", json={"skip_check": True}).json()[
        "run_id"
    ]

    resp = client.post(f"/api/workspaces/beta-test/runs/{run_id}/notify/result/preview")
    assert resp.status_code == 404
    assert "scores" in resp.json()["detail"].lower()
