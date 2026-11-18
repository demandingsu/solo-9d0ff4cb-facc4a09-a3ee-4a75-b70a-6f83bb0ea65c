"""End-to-end tests for loading-chain verification."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_lifecycle import new_plate


def layout(pairs, extra=""):
    lines = ["well,role,sample_id"]
    for well, sample in pairs:
        lines.append(f"{well},sample,{sample}")
    lines.append("H1,blank,")
    lines.append("H2,positive_control,")
    return "\n".join(lines) + "\n" + extra


def upload_map(client, plate_id, pairs, name="map.csv", note=""):
    response = client.post(
        f"/api/plates/{plate_id}/uploads",
        data={"note": note},
        files={"map_file": (name, layout(pairs).encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response


def seed_plates(client):
    source = new_plate(client, "source")
    middle = new_plate(client, "middle")
    target = new_plate(client, "target")
    upload_map(client, source, [("A1", "S1"), ("A2", "S2"), ("A3", "S1")])
    upload_map(client, middle, [("A1", "M1"), ("B1", "M2"), ("C1", "M3")])
    upload_map(client, target, [("A1", "S1"), ("A2", "S2"), ("A3", "S1")])
    return source, middle, target


def log_csv(rows):
    lines = ["operation,source_plate,source_well,target_plate,target_well"]
    for row in rows:
        lines.append(",".join(str(item) for item in row))
    return ("\n".join(lines) + "\n").encode()


def verify(client, source, target, rows, note=""):
    return client.post(
        "/api/chains",
        data={"source_plate_id": source, "target_plate_id": target, "note": note},
        files={"log_file": ("transfer.csv", log_csv(rows), "text/csv")},
    )


def get_chain(client, verification_id):
    response = client.get(f"/api/chains/{verification_id}")
    assert response.status_code == 200, response.text
    return response.json()["verification"]


def snapshot_maps(client, plate_id):
    return client.get(f"/api/plates/{plate_id}").json()["map_versions"]


MULTI_HOP_OK = [
    (1, "source", "A1", "middle", "A1"),
    (2, "source", "A2", "middle", "B1"),
    (3, "middle", "A1", "target", "A1"),
    (4, "middle", "B1", "target", "A2"),
    (5, "source", "A3", "target", "A3"),
]


def test_multi_hop_paths_match_registration(client):
    source, middle, target = seed_plates(client)
    response = verify(client, source, target, MULTI_HOP_OK)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["outcome"] == "completed"
    assert result["summary"]["summary"]["trustworthy"] is True

    verification = get_chain(client, result["verification_id"])
    assert verification["status"] == "completed"
    assert verification["active"] is True
    assert verification["inputs_are_latest"] is True
    roles = {plate["name"]: plate["role"] for plate in verification["plates"]}
    assert roles == {"source": "source", "middle": "intermediate", "target": "target"}

    wells = {row["well"]: row for row in verification["result"]["target_wells"]}
    assert wells["A1"]["status"] == "match"
    assert wells["A1"]["paths"][0] == [
        "source A1",
        "source A1→middle A1（#1）",
        "middle A1→target A1（#3）",
    ]
    assert wells["A2"]["paths"][0][-1] == "middle B1→target A2（#4）"
    assert wells["A3"]["paths"][0] == ["source A3", "source A3→target A3（#5）"]

    page = client.get(f"/chains/{result['verification_id']}")
    assert page.status_code == 200
    assert "middle A1→target A1" in page.text
    assert "S1" in page.text


def test_map_correction_marks_verification_stale_until_recomputed(client):
    source, middle, target = seed_plates(client)
    first = verify(client, source, target, MULTI_HOP_OK).json()["result"]

    # Correct the target registration S2 -> S9: a new map version is stored
    # and the old conclusion is immediately stale, but still viewable.
    upload_map(client, target, [("A1", "S9"), ("A2", "S2"), ("A3", "S1")], name="target-v2.csv")
    stale = get_chain(client, first["verification_id"])
    assert stale["status"] == "stale"
    assert stale["active"] is True
    assert stale["inputs_are_latest"] is False
    assert stale["stale_reason"] == "孔位图已更新至 v2"
    assert stale["result"]["target_wells"]  # old report still present

    page = client.get(f"/chains/{first['verification_id']}")
    assert "已过期" in page.text

    # Recompute publishes a new complete result and supersedes the old one.
    outcome = client.post(f"/api/chains/{first['verification_id']}/recompute").json()["result"]
    assert outcome["outcome"] == "completed"
    assert outcome["verification_seq"] == 2
    new = get_chain(client, outcome["verification_id"])
    assert new["status"] == "completed"
    assert new["active"] is True
    assert new["summary"]["mismatched"] == 1
    mismatch = new["result"]["mismatches"][0]
    assert mismatch["well"] == "A1"
    assert mismatch["registered"] == "S9"
    assert mismatch["derived"] == "S1"
    assert get_chain(client, first["verification_id"])["status"] == "superseded"


def test_intermediate_plate_correction_invalidates_verification(client):
    source, middle, target = seed_plates(client)
    first = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    upload_map(client, middle, [("A1", "M1"), ("B1", "M2"), ("C1", "M3"), ("D1", "M4")], name="middle-v2.csv")
    stale = get_chain(client, first["verification_id"])
    assert stale["status"] == "stale"
    assert stale["outdated_plates"] == [{"name": "middle", "pinned_seq": 1}]


def test_source_plate_correction_invalidates_verification(client):
    source, _, target = seed_plates(client)
    first = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    upload_map(client, source, [("A1", "S1"), ("A2", "S2"), ("A3", "S1")], name="source-v2.csv")
    stale = get_chain(client, first["verification_id"])
    assert stale["status"] == "stale"
    assert stale["outdated_plates"] == [{"name": "source", "pinned_seq": 1}]
    assert "孔位图已更新至 v2" in stale["stale_reason"]
    # The stale report is still viewable with its original pins and result.
    page = client.get(f"/chains/{first['verification_id']}")
    assert page.status_code == 200
    assert stale["plates"][0]["map_version_seq"] == 1  # source pin kept
    assert stale["result"]["target_wells"]

    # Recompute publishes a fresh complete result against map v2.
    outcome = client.post(f"/api/chains/{first['verification_id']}/recompute").json()["result"]
    assert outcome["outcome"] == "completed"
    fresh = get_chain(client, outcome["verification_id"])
    assert fresh["inputs_are_latest"] is True
    source_pin = next(p for p in fresh["plates"] if p["role"] == "source")
    assert source_pin["map_version_seq"] == 2


def test_later_addition_does_not_rewrite_earlier_transfer(client):
    source = new_plate(client, "source")
    middle = new_plate(client, "middle")
    target = new_plate(client, "target")
    upload_map(client, source, [("A1", "S1"), ("A2", "S2")])
    upload_map(client, middle, [("A1", "M1"), ("B1", "M2")])
    upload_map(client, target, [("A1", "S1")])

    # Op 1 moves S1 out of middle A1 before op 3 adds S2 into it.  The already
    # transferred material cannot be rewritten by the later addition.
    result = verify(
        client,
        source,
        target,
        [
            (1, "source", "A1", "middle", "A1"),
            (2, "middle", "A1", "target", "A1"),
            (3, "source", "A2", "middle", "A1"),
        ],
    ).json()["result"]
    assert result["outcome"] == "completed"
    verification = get_chain(client, result["verification_id"])
    wells = {row["well"]: row for row in verification["result"]["target_wells"]}
    assert wells["A1"]["status"] == "match"
    assert wells["A1"]["sample_ids"] == ["S1"]
    # The later addition did create a mix, but only upstream: reported, not
    # propagated into the target conclusion.
    assert verification["summary"]["mixed_anywhere"] == 1
    upstream = [c for c in verification["checks"] if c["id"] == "upstream_mix"]
    assert upstream and upstream[0]["detail"] == "middle A1"


def test_mix_upstream_propagates_to_downstream_wells(client):
    source = new_plate(client, "source")
    middle = new_plate(client, "middle")
    target = new_plate(client, "target")
    upload_map(client, source, [("A1", "S1"), ("A2", "S2")])
    upload_map(client, middle, [("A1", "M1"), ("B1", "M2")])
    upload_map(client, target, [("A1", "S1"), ("A2", "S2")])

    result = verify(
        client,
        source,
        target,
        [
            (1, "source", "A1", "middle", "A1"),
            (2, "source", "A2", "middle", "A1"),
            (3, "middle", "A1", "target", "A1"),
            (4, "middle", "A1", "target", "A2"),
        ],
    ).json()["result"]
    assert result["outcome"] == "completed"
    verification = get_chain(client, result["verification_id"])
    wells = {row["well"]: row for row in verification["result"]["target_wells"]}
    assert wells["A1"]["status"] == "mixed"
    assert wells["A2"]["status"] == "mixed"
    assert wells["A1"]["sample_ids"] == ["S1", "S2"]
    assert len(wells["A1"]["paths"]) == 2
    assert verification["summary"]["mixed"] == 2
    assert verification["conclusions_trustworthy"] is True


def test_missing_and_mismatch_are_reported(client):
    source = new_plate(client, "source")
    target = new_plate(client, "target")
    upload_map(client, source, [("A1", "S1"), ("A2", "S2")])
    upload_map(client, target, [("A1", "S2"), ("A2", "S2")])
    result = verify(
        client,
        source,
        target,
        [(1, "source", "A1", "target", "A1")],
    ).json()["result"]
    verification = get_chain(client, result["verification_id"])
    wells = {row["well"]: row for row in verification["result"]["target_wells"]}
    assert wells["A1"]["status"] == "mismatch"  # registered S2, derived S1
    assert wells["A2"]["status"] == "missing"
    summary = verification["summary"]
    assert summary["mismatched"] == 1
    assert summary["missing"] == 1


def test_invalid_well_fails_without_replacing_stale_result(client):
    source, middle, target = seed_plates(client)
    good = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    upload_map(client, target, [("A1", "S1"), ("A2", "S2"), ("A3", "S1")], name="v2.csv")
    assert get_chain(client, good["verification_id"])["status"] == "stale"

    failed = verify(
        client,
        source,
        target,
        [(1, "source", "A9", "target", "A1")],
    ).json()["result"]
    assert failed["outcome"] == "failed"
    assert failed["active_is_stale"] is True
    verification = get_chain(client, failed["verification_id"])
    assert verification["status"] == "failed"
    assert verification["active"] is False
    assert verification["conclusions_trustworthy"] is False
    assert verification["result"] is None
    assert any("A9" in item["reason"] for item in verification["invalid_operations"])
    # The stale old version stays active and viewable, with its own inputs.
    old = get_chain(client, good["verification_id"])
    assert old["status"] == "stale"
    assert old["active"] is True
    assert old["result"]["target_wells"]
    page = client.get(f"/chains/{good['verification_id']}")
    assert "已过期" in page.text


def test_empty_source_read_is_invalid(client):
    source = new_plate(client, "source")
    middle = new_plate(client, "middle")
    target = new_plate(client, "target")
    upload_map(client, source, [("A1", "S1")])
    upload_map(client, middle, [("A1", "M1")])
    upload_map(client, target, [("A1", "S1")])
    result = verify(
        client,
        source,
        target,
        [(1, "middle", "A1", "target", "A1"), (2, "source", "A1", "middle", "A1")],
    ).json()["result"]
    assert result["outcome"] == "failed"
    verification = get_chain(client, result["verification_id"])
    assert "还没有任何样本来源" in verification["invalid_operations"][0]["reason"]


def test_duplicate_operation_numbers_reject_log(client):
    source, _, target = seed_plates(client)
    before = snapshot_maps(client, target)
    response = client.post(
        "/api/chains",
        data={"source_plate_id": source, "target_plate_id": target},
        files={
            "log_file": (
                "transfer.csv",
                log_csv(
                    [
                        (1, "source", "A1", "target", "A1"),
                        (1, "source", "A2", "target", "A2"),
                    ]
                ),
                "text/csv",
            )
        },
    )
    assert response.status_code == 400
    assert "重复" in response.json()["error"]
    # No version, no verification created; existing inputs untouched.
    assert client.get("/api/chains").json()["verifications"] == []
    assert snapshot_maps(client, target) == before


def test_malformed_log_does_not_invalidate_existing_result(client):
    source, _, target = seed_plates(client)
    good = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    response = client.post(
        "/api/chains",
        data={"source_plate_id": source, "target_plate_id": target},
        files={"log_file": ("bad.csv", b"well,role\nA1,sample\n", "text/csv")},
    )
    assert response.status_code == 400
    current = get_chain(client, good["verification_id"])
    assert current["status"] == "completed"
    assert current["inputs_are_latest"] is True


def test_transfer_log_replacement_stales_previous_result(client):
    source, _, target = seed_plates(client)
    first = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    replacement = [
        (1, "source", "A1", "target", "A1"),
        (2, "source", "A2", "target", "A2"),
        (3, "source", "A3", "target", "A3"),
    ]
    second = verify(client, source, target, replacement).json()["result"]
    assert second["outcome"] == "completed"
    old = get_chain(client, first["verification_id"])
    assert old["status"] == "superseded"
    current = get_chain(client, second["verification_id"])
    assert current["status"] == "completed"
    assert current["log"]["seq"] == 2
    assert current["log_outdated"] is False


def test_stale_verification_is_recomputed_against_new_version(client):
    source, middle, target = seed_plates(client)
    good = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    original_map = [("A1", "S1"), ("A2", "S2"), ("A3", "S1")]
    upload_map(client, target, [("A1", "S9"), ("A2", "S2"), ("A3", "S1")], name="v2.csv")
    assert get_chain(client, good["verification_id"])["status"] == "stale"
    # Correcting back is a new version too; recomputation pins that version.
    upload_map(client, target, original_map, name="v3.csv")
    outcome = client.post(f"/api/chains/{good['verification_id']}/recompute").json()["result"]
    assert outcome["outcome"] == "completed"
    current = get_chain(client, outcome["verification_id"])
    assert current["status"] == "completed"
    assert current["inputs_are_latest"] is True
    assert current["summary"]["mismatched"] == 0
    target_pins = [p for p in current["plates"] if p["role"] == "target"]
    assert target_pins[0]["map_version_seq"] == 3
    assert get_chain(client, good["verification_id"])["status"] == "superseded"


def test_plate_detail_lists_participating_verifications(client):
    source, middle, target = seed_plates(client)
    result = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    for plate_id, expected_role in ((source, "源板"), (middle, "中间板"), (target, "待核板")):
        page = client.get(f"/plates/{plate_id}")
        assert page.status_code == 200
        assert expected_role in page.text
    assert f"/chains/{result['verification_id']}" in client.get(f"/plates/{middle}").text


def test_log_download_is_bound_to_version(client):
    source, _, target = seed_plates(client)
    result = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    response = client.get(f"/chains/{result['verification_id']}/log")
    assert response.status_code == 200
    assert response.content == log_csv(MULTI_HOP_OK)
    assert response.headers["x-content-sha256"]
    verification = get_chain(client, result["verification_id"])
    assert verification["log"]["sha256"]
    assert verification["log"]["seq"] == 1


def test_concurrent_verifications_publish_one_complete_result(client):
    source, middle, target = seed_plates(client)
    barrier = threading.Barrier(4)

    def worker(index: int):
        barrier.wait()
        return verify(client, source, target, MULTI_HOP_OK).json()["result"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(worker, range(4)))
    assert all(item["outcome"] == "completed" for item in outcomes)
    # Four sequential log versions were stored, each with a unique seq.
    assert sorted(item["new_log_version"] for item in outcomes) == [1, 2, 3, 4]

    listed = client.get("/api/chains").json()["verifications"]
    assert len(listed) == 1
    active = get_chain(client, listed[0]["id"])
    assert active["active"] is True
    assert active["status"] == "completed"
    assert active["inputs_are_latest"] is True
    assert active["log"]["seq"] == 4
    # Every plate pin and the log belong to one committed batch.
    assert len(active["plates"]) == 3
    for plate in active["plates"]:
        assert plate["map_version_seq"] == 1
    assert active["result"]["target_wells"]
    assert len(active["result"]["target_wells"]) == 5  # 3 sample + 2 control/blank


def test_concurrent_map_correction_and_verification(client):
    source, _, target = seed_plates(client)

    def correction():
        return upload_map(
            client, target, [("A1", "S1"), ("A2", "S2"), ("A3", "S1")], name="corrected.csv"
        )

    def verification():
        return verify(client, source, target, MULTI_HOP_OK)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(correction), pool.submit(verification)]
        responses = [future.result() for future in futures]
    assert all(response.status_code == 200 for response in responses)

    listed = client.get("/api/chains").json()["verifications"]
    assert len(listed) == 1
    active = get_chain(client, listed[0]["id"])
    # The published result is either fresh (completed + latest) or stale; it
    # must never claim to be current while pinning an older map.
    if active["status"] == "completed":
        assert active["inputs_are_latest"] is True
    else:
        assert active["status"] == "stale"
        assert active["inputs_are_latest"] is False


def test_same_plate_rejected(client):
    source = new_plate(client, "only")
    upload_map(client, source, [("A1", "S1")])
    response = client.post(
        "/api/chains",
        data={"source_plate_id": source, "target_plate_id": source},
        files={"log_file": ("t.csv", log_csv([(1, "only", "A1", "only", "A1")]), "text/csv")},
    )
    assert response.status_code == 409


def test_missing_map_blocks_verification(client):
    source = new_plate(client, "source")
    target = new_plate(client, "target")
    upload_map(client, source, [("A1", "S1")])
    response = client.post(
        "/api/chains",
        data={"source_plate_id": source, "target_plate_id": target},
        files={"log_file": ("t.csv", log_csv([(1, "source", "A1", "target", "A1")]), "text/csv")},
    )
    assert response.status_code == 409
    assert "孔位图" in response.json()["error"]
    assert client.get("/api/chains").json()["verifications"] == []


def test_transfer_into_blank_well_is_structural_failure(client):
    source, _, target = seed_plates(client)
    result = verify(
        client,
        source,
        target,
        [
            (1, "source", "A1", "target", "H1"),  # H1 is a blank well
            (2, "source", "A2", "target", "A2"),
            (3, "source", "A3", "target", "A3"),
        ],
    ).json()["result"]
    assert result["outcome"] == "failed"
    assert "不是样本孔" in result["summary"]["invalid_operations"][0]["reason"]


def test_repeated_transfer_of_same_sample_is_not_mixed(client):
    source, _, target = seed_plates(client)
    result = verify(
        client,
        source,
        target,
        [
            (1, "source", "A1", "target", "A1"),
            (2, "source", "A3", "target", "A1"),  # A3 is the same registered S1
            (3, "source", "A2", "target", "A2"),
            (4, "source", "A1", "target", "A3"),
        ],
    ).json()["result"]
    assert result["outcome"] == "completed"
    verification = get_chain(client, result["verification_id"])
    wells = {row["well"]: row for row in verification["result"]["target_wells"]}
    assert wells["A1"]["status"] == "match"
    assert wells["A1"]["sample_ids"] == ["S1"]
    assert len(wells["A1"]["paths"]) == 2  # two source chains, same sample
    assert wells["A1"]["merged_chains"] is True
    assert wells["A1"]["mixed"] is False
    assert wells["A1"]["operations"] == [1, 2]


def test_html_new_form_flow(client):
    source, _, target = seed_plates(client)
    page = client.get("/chains/new")
    assert page.status_code == 200
    assert "转移日志" in page.text
    response = client.post(
        "/chains/new",
        data={"source_plate_id": source, "target_plate_id": target},
        files={"log_file": ("transfer.csv", log_csv(MULTI_HOP_OK), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/chains/")
    detail = client.get(location)
    assert detail.status_code == 200
    assert "一致" in detail.text


def test_source_selected_without_log_referencing_it_fails(client):
    source, middle, target = seed_plates(client)
    # Log only moves within the middle plate: it never reads the chosen source.
    result = verify(
        client,
        source,
        target,
        [
            (1, "source", "A1", "middle", "A1"),
            (2, "middle", "A1", "middle", "B1"),
        ],
    ).json()["result"]
    verification = get_chain(client, result["verification_id"])
    check = next(c for c in verification["checks"] if c["id"] == "log_references_selected_plates")
    assert check["ok"] is False
    assert "待核板" in check["detail"]
    assert verification["status"] == "failed"


def test_sample_files_are_valid_logs(client):
    # The bundled sample uses Chinese plate names; create matching plates.
    for name in ("源板", "中间板", "待核板"):
        new_plate(client, name)
    ids = {
        plate["name"]: plate["id"]
        for plate in client.get("/api/plates").json()["plates"]
    }
    maps = {
        "源板": [("A1", "S1"), ("A2", "S2"), ("A3", "S1")],
        "中间板": [("A1", "M1"), ("B1", "M2")],
        "待核板": [("A1", "S1"), ("A2", "S2"), ("A3", "S1")],
    }
    for name, pairs in maps.items():
        upload_map(client, ids[name], pairs, name=f"{name}.csv")
    sample = (
        "\n".join(
            [
                "operation,source_plate,source_well,target_plate,target_well",
                "1,源板,A1,中间板,A1",
                "2,源板,A2,中间板,B1",
                "3,中间板,A1,待核板,A1",
                "4,中间板,B1,待核板,A2",
                "5,源板,A3,待核板,A3",
            ]
        )
        + "\n"
    )
    response = client.post(
        "/api/chains",
        data={"source_plate_id": ids["源板"], "target_plate_id": ids["待核板"]},
        files={"log_file": ("transfer_chain.csv", sample.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["summary"]["summary"]["matched"] == 3


def test_multiple_target_plates_share_one_source(client):
    """A source may feed several target plates; each pair has its own result."""
    source = new_plate(client, "source")
    target_a = new_plate(client, "target-a")
    target_b = new_plate(client, "target-b")
    upload_map(client, source, [("A1", "S1"), ("A2", "S2")])
    upload_map(client, target_a, [("A1", "S1"), ("A2", "S2")])
    upload_map(client, target_b, [("A1", "S1"), ("A2", "S2")])

    first = verify(
        client,
        source,
        target_a,
        [(1, "source", "A1", "target-a", "A1"), (2, "source", "A2", "target-a", "A2")],
    ).json()["result"]
    second = verify(
        client,
        source,
        target_b,
        [(1, "source", "A1", "target-b", "A1"), (2, "source", "A2", "target-b", "A2")],
    ).json()["result"]
    assert first["outcome"] == second["outcome"] == "completed"

    # Each source/target pair starts its own sequence at 1.
    assert first["verification_seq"] == second["verification_seq"] == 1
    assert first["new_log_version"] == second["new_log_version"] == 1
    listed = client.get("/api/chains").json()["verifications"]
    assert len(listed) == 2
    assert {item["target_plate"]["name"] for item in listed} == {"target-a", "target-b"}
    assert all(item["status"] == "completed" for item in listed)


def test_gb18030_tsv_log_with_chinese_headers(client):
    for name in ("源板", "待核板"):
        new_plate(client, name)
    ids = {
        plate["name"]: plate["id"]
        for plate in client.get("/api/plates").json()["plates"]
    }
    upload_map(client, ids["源板"], [("A1", "S1")], name="s.csv")
    upload_map(client, ids["待核板"], [("A1", "S1")], name="t.csv")
    text = "操作序号\t源板\t源板孔位\t目标板\t目标板孔位\n1\t源板\tA1\t待核板\ta1\n"
    response = client.post(
        "/api/chains",
        data={"source_plate_id": ids["源板"], "target_plate_id": ids["待核板"]},
        files={"log_file": ("transfer.tsv", text.encode("gb18030"), "text/tab-separated-values")},
    )
    assert response.status_code == 200, response.text
    verification = get_chain(client, response.json()["result"]["verification_id"])
    assert verification["summary"]["matched"] == 1


def test_verification_download_and_binding_survive_replacement(client):
    source, _, target = seed_plates(client)
    first = verify(client, source, target, MULTI_HOP_OK).json()["result"]
    replacement = [
        (1, "source", "A1", "target", "A1"),
        (2, "source", "A2", "target", "A2"),
    ]
    verify(client, source, target, replacement)
    old = get_chain(client, first["verification_id"])
    assert old["status"] == "superseded"
    assert old["log"]["seq"] == 1
    assert old["log_outdated"] is True
    old_log = client.get(f"/chains/{first['verification_id']}/log")
    assert old_log.status_code == 200
    assert old_log.content == log_csv(MULTI_HOP_OK)
