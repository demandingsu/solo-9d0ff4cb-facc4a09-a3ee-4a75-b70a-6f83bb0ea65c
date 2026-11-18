"""End-to-end tests for versioned publication and the web UI."""

from __future__ import annotations

import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.conftest import LAYOUT_CSV, READINGS_CSV, readings_override


def new_plate(client, name="plate-1"):
    response = client.post("/api/plates", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["plate"]["id"]


def upload(client, plate_id, *, map_text=None, readings_text=None, note=""):
    files = {}
    if map_text is not None:
        files["map_file"] = ("layout.csv", map_text.encode(), "text/csv")
    if readings_text is not None:
        files["readings_file"] = ("readings.csv", readings_text.encode(), "text/csv")
    return client.post(
        f"/api/plates/{plate_id}/uploads", data={"note": note}, files=files
    )


def snapshot(client, plate_id):
    response = client.get(f"/api/plates/{plate_id}")
    assert response.status_code == 200
    return response.json()


def test_initial_publish_and_report(client):
    plate_id = new_plate(client)
    response = upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["outcome"] == "published"

    snap = snapshot(client, plate_id)
    active = snap["active_publication"]
    assert active["status"] == "published"
    assert active["result"]["samples"][0]["sample_id"] == "S1"
    assert active["result"]["samples"][0]["mean"] == pytest.approx(0.55)
    assert active["inputs"]["map"]["seq"] == 1
    assert active["inputs"]["reading"]["seq"] == 1

    page = client.get(f"/plates/{plate_id}/publications/{active['id']}")
    assert page.status_code == 200
    assert "0.550000" in page.text
    assert "S1" in page.text


def test_upload_makes_active_stale_then_recompute_supersedes(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    first = snapshot(client, plate_id)["active_publication"]
    assert first["status"] == "published"

    # A corrective reading upload: values change, recompute succeeds immediately.
    response = upload(client, plate_id, readings_text=readings_override(A2=0.8, A3=0.9))
    assert response.status_code == 200
    body = response.json()
    assert body["result"]["outcome"] == "published"
    assert body["result"]["stale_publication_id"] == first["id"]

    snap = snapshot(client, plate_id)
    active = snap["active_publication"]
    assert active["status"] == "published"
    assert active["id"] != first["id"]
    assert active["seq"] == 2
    assert active["inputs"]["reading"]["seq"] == 2
    # New blank correction is applied to the new reading version.
    assert active["result"]["samples"][0]["mean"] == pytest.approx((0.7 + 0.8) / 2)

    history = {pub["id"]: pub for pub in snap["publications"]}
    assert history[first["id"]]["status"] == "superseded"
    assert history[first["id"]]["superseded_by"]["id"] == active["id"]
    # The old publication is still viewable and keeps its original inputs.
    old_page = client.get(f"/plates/{plate_id}/publications/{first['id']}")
    assert old_page.status_code == 200
    old = client.get(f"/api/plates/{plate_id}/publications/{first['id']}").json()["publication"]
    assert old["inputs"]["reading"]["seq"] == 1
    assert old["result"]["blank"]["mean"] == pytest.approx(0.1)
    assert old["inputs_are_latest"] is False


def test_failed_recompute_keeps_stale_old_version(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    first = snapshot(client, plate_id)["active_publication"]

    # A reading upload that breaks the positive-control floor: it stays stale.
    response = upload(client, plate_id, readings_text=readings_override(A6=0.3))
    assert response.status_code == 200
    body = response.json()["result"]
    assert body["outcome"] == "failed"
    assert body["active_is_stale"] is True

    snap = snapshot(client, plate_id)
    active = snap["active_publication"]
    assert active["id"] == first["id"]
    assert active["status"] == "stale"
    assert active["inputs"]["reading"]["seq"] == 1  # original inputs preserved
    assert "阳性对照" in active["stale_reason"]

    failed = [pub for pub in snap["publications"] if pub["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["result"] is None
    assert failed[0]["qc"]["passed"] is False

    # Detail page still renders the stale version with its original input.
    page = client.get(f"/plates/{plate_id}/publications/{first['id']}")
    assert page.status_code == 200
    assert "已过期" in page.text

    # Retrying without changing inputs still fails and keeps the old stale one.
    retry = client.post(f"/api/plates/{plate_id}/recompute").json()
    assert retry["result"]["outcome"] == "failed"
    again = snapshot(client, plate_id)["active_publication"]
    assert again["id"] == first["id"]
    assert again["status"] == "stale"


def test_corrected_map_recovers_stale_publication(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    first = snapshot(client, plate_id)["active_publication"]

    # Bad QC upload: sample gets 3 replicates in the corrected map.
    bad_map = LAYOUT_CSV.replace("A6,positive_control,", "A6,sample,S1")
    response = upload(client, plate_id, map_text=bad_map)
    assert response.json()["result"]["outcome"] == "failed"
    assert snapshot(client, plate_id)["active_publication"]["status"] == "stale"

    # Correcting the map back (new map version) restores publication.
    response = upload(client, plate_id, map_text=LAYOUT_CSV, note="纠正孔位图")
    body = response.json()["result"]
    assert body["outcome"] == "published"
    assert body["map_version_seq"] == 3
    snap = snapshot(client, plate_id)
    assert snap["active_publication"]["status"] == "published"
    assert snap["active_publication"]["inputs"]["map"]["seq"] == 3
    assert snap["active_publication"]["seq"] == 3  # 1 published, 2 failed, 3 published


def test_malformed_upload_does_not_create_version(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    before = snapshot(client, plate_id)

    response = upload(client, plate_id, readings_text="well,absorbance\nA1,oops\n")
    assert response.status_code == 400
    assert "不是数字" in response.json()["error"]

    after = snapshot(client, plate_id)
    assert len(after["reading_versions"]) == len(before["reading_versions"])
    assert after["active_publication"]["status"] == "published"


def test_upload_before_both_inputs_then_publish(client):
    plate_id = new_plate(client)
    response = upload(client, plate_id, map_text=LAYOUT_CSV)
    assert response.json()["result"]["outcome"] == "inputs_incomplete"
    assert snapshot(client, plate_id)["active_publication"] is None

    response = upload(client, plate_id, readings_text=READINGS_CSV)
    assert response.json()["result"]["outcome"] == "published"


def test_published_report_binds_inputs_even_after_new_upload(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    first = snapshot(client, plate_id)["active_publication"]

    upload(client, plate_id, readings_text=readings_override(A1=0.2))
    second = snapshot(client, plate_id)["active_publication"]
    assert second["id"] != first["id"]

    old = client.get(f"/api/plates/{plate_id}/publications/{first['id']}").json()["publication"]
    assert old["result"]["blank"]["mean"] == pytest.approx(0.1)
    assert old["inputs"]["reading"]["seq"] == 1
    new = client.get(f"/api/plates/{plate_id}/publications/{second['id']}").json()["publication"]
    assert new["result"]["blank"]["mean"] == pytest.approx(0.2)
    assert new["inputs"]["reading"]["seq"] == 2


def test_download_original_inputs(client):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    snap = snapshot(client, plate_id)
    reading_version = snap["reading_versions"][0]
    response = client.get(f"/plates/{plate_id}/versions/reading/{reading_version['id']}/file")
    assert response.status_code == 200
    assert response.content.decode() == READINGS_CSV
    assert response.headers["x-content-sha256"] == reading_version["sha256"]


def test_html_pages_render(client):
    plate_id = new_plate(client)
    assert client.get("/").status_code == 200
    assert client.get(f"/plates/{plate_id}").status_code == 200
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    page = client.get(f"/plates/{plate_id}")
    assert page.status_code == 200
    assert "已发布" in page.text


def test_concurrent_uploads_serialise_versions(client, app_env):
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)

    def worker(index: int):
        return upload(client, plate_id, readings_text=readings_override(A2=0.6 + index * 0.01))

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(worker, range(8)))

    assert all(response.status_code == 200 for response in responses)
    snap = snapshot(client, plate_id)
    reading_seqs = sorted(version["seq"] for version in snap["reading_versions"])
    assert reading_seqs == list(range(1, 10))  # no duplicate seq under concurrency
    # Exactly one active publication, bound to the newest reading version.
    actives = [pub for pub in snap["publications"] if pub["active"]]
    assert len(actives) == 1
    assert actives[0]["inputs"]["reading"]["seq"] == 9
    assert actives[0]["status"] == "published"
    # Every publication's inputs are a consistent matched pair.
    for pub in snap["publications"]:
        if pub["status"] in {"published", "stale", "superseded"}:
            assert pub["inputs"]["map"] is not None
            assert pub["inputs"]["reading"] is not None


def test_snapshot_never_mixes_versions(client, app_env):
    """A report read during concurrent uploads must keep matched inputs."""
    plate_id = new_plate(client)
    upload(client, plate_id, map_text=LAYOUT_CSV, readings_text=READINGS_CSV)
    stop = threading.Event()
    errors: list[str] = []

    def reader():
        while not stop.is_set():
            try:
                snap = snapshot(client, plate_id)
                pub = snap["active_publication"]
                if pub is None:
                    continue
                # The published mean must be derivable from its bound inputs.
                map_seq = pub["inputs"]["map"]["seq"]
                reading_seq = pub["inputs"]["reading"]["seq"]
                if map_seq > snap["latest_map_version"]["seq"]:
                    errors.append("map from future")
                if reading_seq > snap["latest_reading_version"]["seq"]:
                    errors.append("reading from future")
                if pub["status"] == "published":
                    mean = pub["result"]["samples"][0]["mean"]
                    blank = pub["result"]["blank"]["mean"]
                    expected = ((0.6 - blank) + (0.7 - blank)) / 2
                    assert mean == pytest.approx(expected)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    try:
        for index in range(6):
            response = upload(client, plate_id, readings_text=readings_override(A4=1.0 + index * 0.01))
            assert response.status_code == 200
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    assert errors == []


def test_missing_plate_404(client):
    assert client.get("/api/plates/9999").status_code == 404
    assert client.get("/plates/9999").status_code == 404
    assert client.get("/plates/9999/publications/1").status_code == 404
