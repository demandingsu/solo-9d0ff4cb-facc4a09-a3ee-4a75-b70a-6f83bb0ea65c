"""Service-level concurrency with real SQLite connections (no ASGI layer)."""

from __future__ import annotations

import threading

from app import service
from tests.conftest import LAYOUT_CSV, READINGS_CSV, readings_override


def _seed() -> int:
    plate = service.create_plate("concurrent")
    service.ingest(
        plate["id"],
        map_data=LAYOUT_CSV.encode(),
        map_filename="layout.csv",
        readings_data=READINGS_CSV.encode(),
        readings_filename="readings.csv",
    )
    return plate["id"]


def test_parallel_ingest_allocates_unique_versions_and_one_active(app_env):
    plate_id = _seed()
    barrier = threading.Barrier(6)
    results: list[dict] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        payload = readings_override(A4=1.0 + index * 0.01).encode()
        barrier.wait()
        try:
            outcome = service.ingest(
                plate_id, readings_data=payload, readings_filename=f"r{index}.csv"
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert {result["outcome"] for result in results} == {"published"}

    snapshot = service.get_plate_snapshot(plate_id)
    reading_seqs = sorted(version["seq"] for version in snapshot["reading_versions"])
    assert reading_seqs == [1, 2, 3, 4, 5, 6, 7]

    actives = [pub for pub in snapshot["publications"] if pub["active"]]
    assert len(actives) == 1
    latest_reading = snapshot["latest_reading_version"]["id"]
    assert actives[0]["inputs"]["reading"]["id"] == latest_reading
    assert actives[0]["inputs"]["map"]["seq"] == 1

    # Each published/stale publication retains its own matched input pair.
    for pub in snapshot["publications"]:
        if pub["status"] == "failed":
            continue
        assert pub["inputs"]["map"]["id"] is not None
        assert pub["inputs"]["reading"]["id"] is not None


def test_parallel_reads_during_writes_observe_consistent_pairs(app_env):
    plate_id = _seed()
    stop = threading.Event()
    problems: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        while not stop.is_set():
            snap = service.get_plate_snapshot(plate_id)
            pub = snap["active_publication"]
            if pub is None:
                continue
            with lock:
                if pub["inputs"]["map"]["seq"] > snap["latest_map_version"]["seq"]:
                    problems.append("map version ahead of latest")
                if pub["inputs"]["reading"]["seq"] > snap["latest_reading_version"]["seq"]:
                    problems.append("reading version ahead of latest")
            if pub["status"] == "published":
                blank = pub["result"]["blank"]["mean"]
                pc = pub["result"]["positive_control"]["mean"]
                # QC conclusion stored with the publication must match its data.
                if not pc >= blank + 0.5:
                    with lock:
                        problems.append("qc conclusion inconsistent with stored data")

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in readers:
        thread.start()
    try:
        for index in range(5):
            service.ingest(
                plate_id,
                readings_data=readings_override(A6=1.2 + index * 0.01).encode(),
                readings_filename="r.csv",
            )
    finally:
        stop.set()
        for thread in readers:
            thread.join()

    assert problems == []


def test_recompute_and_ingest_race_never_leave_two_active(app_env):
    plate_id = _seed()

    def do_ingest(index: int) -> None:
        service.ingest(
            plate_id,
            readings_data=readings_override(A2=0.6 + index * 0.01).encode(),
            readings_filename="r.csv",
        )

    def do_recompute() -> None:
        try:
            service.recompute(plate_id)
        except service.ConflictError:
            pass

    threads = [threading.Thread(target=do_ingest, args=(index,)) for index in range(3)]
    threads += [threading.Thread(target=do_recompute) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = service.get_plate_snapshot(plate_id)
    assert len([pub for pub in snapshot["publications"] if pub["active"]]) == 1
    assert snapshot["active_publication"]["status"] in {"published", "stale"}
