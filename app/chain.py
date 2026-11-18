"""Loading-chain verification: parse robot transfer logs and trace provenance.

A transfer log lists pipetting operations in execution order::

    operation,source_plate,source_well,target_plate,target_well
    1,source,A1,intermediate,A1
    2,source,A2,intermediate,B1
    3,intermediate,A1,target,A1
    4,intermediate,B1,target,B2

The trace is a deterministic simulation of volume-level provenance:

* Every sample well of the selected *source* plate starts with its registered
  sample at ``T0 + 1`` (it may have been dispensed from its original tube at
  any time; only transfers listed in this log have known relative order).
* Sample wells of intermediate/target plates start empty at ``T0``.
* A transfer reads the source well *as of* the operation's position in the
  order.  Later additions to that well therefore can never rewrite what an
  earlier transfer already moved -- the physically correct copy-on-read
  semantics.  This is why provenance is recorded per operation and not as a
  "reverse map" from the final plate contents.
* Reading a well copies all provenance currently in it.  When a mix is moved
  on, the descendants carry every contributing chain, so a mixed intermediate
  well taints everything downstream.
* A well holding provenance from more than one chain is *mixed*.  A target
  sample well that ends up empty is *missing*.

Structural problems (duplicate operation numbers, operations that read a well
with no provenance at the time, transfers into blank/control/unused wells,
a source plate that is actually the target plate, ...) make the reconstruction
incomplete.  Such a run raises no exception here -- the caller records a failed
attempt with the diagnostic issues, and no trustworthy conclusion is exposed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import parsing

T0 = -(10**9)  # before any logged operation
SOURCE_SEED_OFFSET = 1  # source sample wells pre-date every transfer

_OP_HEADERS = {
    "operation",
    "operations",
    "op",
    "op_id",
    "op_no",
    "op_number",
    "order",
    "step",
    "seq",
    "sequence",
    "transfer",
    "transfer_id",
    "transfer_no",
    "id",
    "操作序号",
    "操作号",
    "操序号",
    "序号",
    "步骤",
    "步骤号",
    "转移序号",
    "编号",
}
_SOURCE_PLATE_HEADERS = {
    "source_plate",
    "sourceplate",
    "source",
    "from_plate",
    "fromplate",
    "from",
    "源板",
    "源板名称",
    "来源板",
    "来源板名称",
    "源",
}
_TARGET_PLATE_HEADERS = {
    "target_plate",
    "targetplate",
    "target",
    "to_plate",
    "toplate",
    "to",
    "目标板",
    "目标板名称",
    "待核板",
    "待核板名称",
    "目标",
}
_SOURCE_WELL_HEADERS = {
    "source_well",
    "sourcewell",
    "from_well",
    "fromwell",
    "source_position",
    "source_well_id",
    "源孔位",
    "源孔",
    "源板孔位",
    "源板孔",
    "来源孔位",
    "来源孔",
}
_TARGET_WELL_HEADERS = {
    "target_well",
    "targetwell",
    "to_well",
    "towell",
    "target_position",
    "target_well_id",
    "目标孔位",
    "目标孔",
    "目标板孔位",
    "目标板孔",
    "待核孔位",
    "待核孔",
}

_INT_RE = re.compile(r"^[0-9]+$")


class TransferLogError(parsing.InputError):
    """A transfer-log file could not be interpreted."""


@dataclass
class ParsedTransferLog:
    operations: list[dict[str, Any]]
    warnings: list[str] = field(default_factory=list)


def _header_norm(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lower())


def _find(header: list[str], aliases: set[str]) -> int | None:
    return next((index for index, cell in enumerate(header) if cell in aliases), None)


def parse_transfer_log(data: bytes) -> ParsedTransferLog:
    """Parse and structurally validate a transfer CSV/TSV file.

    Structural means: usable header, integer operation numbers without
    duplicates, plate names present, syntactically valid wells.  Whether a
    well exists in a plate map, whether a read happens too early, and similar
    semantic problems are decided by :func:`simulate` so that the log itself
    remains storable and can be inspected.
    """
    text = parsing.decode_text(data)
    rows = parsing._read_rows(text)
    header = [_header_norm(cell) for cell in rows[0]]

    op_idx = _find(header, _OP_HEADERS)
    source_plate_idx = _find(header, _SOURCE_PLATE_HEADERS)
    target_plate_idx = _find(header, _TARGET_PLATE_HEADERS)
    source_well_idx = _find(header, _SOURCE_WELL_HEADERS)
    target_well_idx = _find(header, _TARGET_WELL_HEADERS)

    missing = [
        label
        for label, index in (
            ("操作序号", op_idx),
            ("源板", source_plate_idx),
            ("源板孔位", source_well_idx),
            ("目标板", target_plate_idx),
            ("目标板孔位", target_well_idx),
        )
        if index is None
    ]
    if missing:
        raise TransferLogError(
            "无法识别转移日志表头，缺少列：" + "、".join(missing)
            + "。请提供 operation、source_plate、source_well、target_plate、target_well"
        )

    operations: list[dict[str, Any]] = []
    seen: dict[int, int] = {}
    lines: dict[int, int] = {}

    assert op_idx is not None  # guarded above
    assert source_plate_idx is not None
    assert target_plate_idx is not None
    assert source_well_idx is not None
    assert target_well_idx is not None

    for line_no, row in enumerate(rows[1:], start=2):
        def cell(index: int) -> str:
            return row[index].strip() if index < len(row) else ""

        raw_op = cell(op_idx)
        source_plate = cell(source_plate_idx)
        target_plate = cell(target_plate_idx)
        raw_source_well = cell(source_well_idx)
        raw_target_well = cell(target_well_idx)
        if not (raw_op or source_plate or target_plate or raw_source_well or raw_target_well):
            continue
        if not raw_op:
            raise TransferLogError(f"第 {line_no} 行缺少操作序号")
        if not _INT_RE.match(raw_op):
            raise TransferLogError(f"第 {line_no} 行的操作序号“{raw_op}”不是正整数")
        operation = int(raw_op)
        if operation <= 0:
            raise TransferLogError(f"第 {line_no} 行的操作序号必须大于 0")
        if operation in seen:
            raise TransferLogError(
                f"操作序号 {operation} 重复（第 {seen[operation]} 行与第 {line_no} 行），"
                "无法确定执行顺序"
            )
        seen[operation] = line_no
        lines[operation] = line_no
        if not source_plate:
            raise TransferLogError(f"第 {line_no} 行缺少源板名称")
        if not target_plate:
            raise TransferLogError(f"第 {line_no} 行缺少目标板名称")
        if not raw_source_well:
            raise TransferLogError(f"第 {line_no} 行缺少源板孔位")
        if not raw_target_well:
            raise TransferLogError(f"第 {line_no} 行缺少目标板孔位")
        try:
            source_well = parsing.normalize_well(raw_source_well)
            target_well = parsing.normalize_well(raw_target_well)
        except parsing.InputError as exc:
            raise TransferLogError(f"第 {line_no} 行：{exc}") from exc
        operations.append(
            {
                "operation": operation,
                "source_plate": source_plate,
                "source_well": source_well,
                "target_plate": target_plate,
                "target_well": target_well,
                "line": line_no,
            }
        )

    if not operations:
        raise TransferLogError("转移日志没有任何操作行")

    operations.sort(key=lambda item: item["operation"])
    warnings: list[str] = []
    if len(operations) != len(seen):
        warnings.append("转移日志中有重复操作序号，已拒绝保存")
    return ParsedTransferLog(operations=operations, warnings=warnings)


# ---------------------------------------------------------------------------
# Simulation


def _well_key(plate: str, well: str) -> tuple[str, str]:
    return (plate, well)


def _origin(chain_id: str, sample_id: str, plate: str, well: str) -> dict[str, Any]:
    return {
        "chain": chain_id,
        "sample_id": sample_id,
        "origin_plate": plate,
        "origin_well": well,
    }


def _format_hop(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": event["operation"],
        "from_plate": event["from_plate"],
        "from_well": event["from_well"],
        "to_plate": event["to_plate"],
        "to_well": event["to_well"],
    }


def simulate(
    contexts: dict[str, dict[str, Any]],
    operations: list[dict[str, Any]],
    *,
    source_plate: str,
    target_plate: str,
) -> dict[str, Any]:
    """Simulate the transfer log and return a serialisable verification result.

    ``contexts`` maps plate name to
    ``{"plate_id", "role", "samples": {well: sample_id}, "roles": {well: role}}``.
    Only plates referenced by the log plus the selected source/target need to
    be present; missing ones are reported as invalid operations.
    """
    samples_by_plate = {name: ctx["samples"] for name, ctx in contexts.items()}
    roles_by_plate = {name: ctx["roles"] for name, ctx in contexts.items()}
    plate_ids = {name: ctx["plate_id"] for name, ctx in contexts.items()}
    plate_roles = {name: ctx["role"] for name, ctx in contexts.items()}

    # Per-cell provenance lists.  Each entry identifies one physical chain of
    # liquid: a chain id (origin plate/well), its sample id and origin.
    state: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for name, ctx in contexts.items():
        for well, sample_id in ctx["samples"].items():
            if ctx["role"] == "source":
                state[_well_key(name, well)] = [
                    _origin(f"{name}:{well}", sample_id, name, well)
                ]
            else:
                state[_well_key(name, well)] = []

    events: list[dict[str, Any]] = []
    invalid_operations: list[dict[str, Any]] = []

    def invalid(op: dict[str, Any], reason: str) -> None:
        invalid_operations.append(
            {
                "operation": op["operation"],
                "line": op.get("line"),
                "source_plate": op["source_plate"],
                "source_well": op["source_well"],
                "target_plate": op["target_plate"],
                "target_well": op["target_well"],
                "reason": reason,
            }
        )

    for op in operations:
        src_plate = op["source_plate"]
        dst_plate = op["target_plate"]
        src_well = op["source_well"]
        dst_well = op["target_well"]

        if src_plate not in contexts:
            invalid(op, f"源板“{src_plate}”没有可用孔位图")
            continue
        if dst_plate not in contexts:
            invalid(op, f"目标板“{dst_plate}”没有可用孔位图")
            continue
        if src_plate == target_plate:
            # The target's own map is the thing under test; allowing it as a
            # source would make every provenance claim circular.
            invalid(op, "待核板不能作为上样来源")
            continue
        if dst_plate == source_plate:
            invalid(op, "不能向所选源板加样（源板登记编号是追溯起点）")
            continue
        if dst_well not in roles_by_plate[dst_plate]:
            invalid(
                op,
                f"目标板“{dst_plate}”孔位 {dst_well} 未在孔位图中登记",
            )
            continue
        if roles_by_plate[dst_plate][dst_well] != "sample":
            invalid(
                op,
                f"目标板“{dst_plate}”孔位 {dst_well} 不是样本孔（"
                f"{roles_by_plate[dst_plate][dst_well]}）",
            )
            continue
        if src_well not in roles_by_plate[src_plate]:
            invalid(
                op,
                f"源板“{src_plate}”孔位 {src_well} 未在孔位图中登记",
            )
            continue
        source_prov = state.get(_well_key(src_plate, src_well)) or []
        if not source_prov:
            invalid(
                op,
                f"源板“{src_plate}”孔位 {src_well} 在操作 {op['operation']} 时"
                "还没有任何样本来源（孔位为空或操作顺序有误）",
            )
            continue

        source_copy = list(source_prov)
        events.append(
            {
                "operation": op["operation"],
                "from_plate": src_plate,
                "from_well": src_well,
                "to_plate": dst_plate,
                "to_well": dst_well,
                "chain_ids": [entry["chain"] for entry in source_copy],
                "sample_ids": sorted({entry["sample_id"] for entry in source_copy}),
            }
        )
        target_state = state.setdefault(_well_key(dst_plate, dst_well), [])
        target_state.extend(source_copy)

    # Chain paths across the whole simulated log.
    hops_by_chain: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        hop = _format_hop(event)
        for chain_id in event["chain_ids"]:
            hops_by_chain.setdefault(chain_id, []).append(hop)

    chains: dict[str, dict[str, Any]] = {}
    for (plate, well), prov in state.items():
        for entry in prov:
            chain = chains.setdefault(
                entry["chain"],
                {
                    "chain_id": entry["chain"],
                    "sample_id": entry["sample_id"],
                    "origin_plate": entry["origin_plate"],
                    "origin_well": entry["origin_well"],
                    "destinations": set(),
                },
            )
            chain["destinations"].add((plate, well))

    chain_list = []
    for chain in sorted(chains.values(), key=lambda item: item["chain_id"]):
        chain_list.append(
            {
                "chain_id": chain["chain_id"],
                "sample_id": chain["sample_id"],
                "origin_plate": chain["origin_plate"],
                "origin_well": chain["origin_well"],
                "hops": hops_by_chain.get(chain["chain_id"], []),
                "destinations": [
                    {"plate": plate, "well": well}
                    for plate, well in sorted(chain["destinations"])
                ],
            }
        )

    # A simulated sequence for every chain, truncated at its first arrival in
    # the requested well, so a later unrelated transfer of the same liquid
    # does not appear in that well's path.
    def path_for(chain_id: str, dst_plate: str, dst_well: str) -> list[str]:
        chain_hops = hops_by_chain.get(chain_id, [])
        origin = chains[chain_id]
        parts = [f"{origin['origin_plate']} {origin['origin_well']}"]
        for hop in chain_hops:
            parts.append(
                f"{hop['from_plate']} {hop['from_well']}→{hop['to_plate']} {hop['to_well']}"
                f"（#{hop['operation']}）"
            )
            if hop["to_plate"] == dst_plate and hop["to_well"] == dst_well:
                break
        return parts

    # Final content of a well can contain the same chain twice (e.g. two drops
    # from one source).  Collapse duplicates for display: a duplicated chain
    # contributes one sample, and "mixed" means different samples, not drops.
    def unique_provenance(prov: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for entry in prov:
            if entry["chain"] in seen:
                continue
            seen.add(entry["chain"])
            result.append(entry)
        return result

    target_wells: list[dict[str, Any]] = []
    well_names = set(roles_by_plate.get(target_plate, {}))
    well_names.update(well for plate, well in state if plate == target_plate)
    for well in sorted(well_names, key=lambda value: (value[0], int(value[1:]))):
        role = roles_by_plate.get(target_plate, {}).get(well)
        registered = samples_by_plate.get(target_plate, {}).get(well)
        prov = unique_provenance(state.get(_well_key(target_plate, well), []))
        chain_ids = sorted({entry["chain"] for entry in prov})
        sample_ids = sorted({entry["sample_id"] for entry in prov})
        moved = sorted(
            {
                event["operation"]
                for event in events
                if event["to_plate"] == target_plate and event["to_well"] == well
            }
        )
        entry: dict[str, Any] = {
            "well": well,
            "role": role,
            "registered_sample_id": registered,
            "chain_ids": chain_ids,
            "sample_ids": sample_ids,
            "derived_sample_id": sample_ids[0] if len(sample_ids) == 1 else None,
            "mixed": len(sample_ids) > 1,
            "merged_chains": len(chain_ids) > 1,
            "operations": moved,
            "paths": [path_for(chain_id, target_plate, well) for chain_id in chain_ids],
        }
        if role is None:
            entry["status"] = "unregistered"
        elif role != "sample":
            entry["status"] = "non_sample"
        elif not chain_ids:
            entry["status"] = "missing"
        elif len(sample_ids) > 1:
            entry["status"] = "mixed"
        elif sample_ids[0] != registered:
            entry["status"] = "mismatch"
        else:
            entry["status"] = "match"
        target_wells.append(entry)

    sample_wells = [row for row in target_wells if row["role"] == "sample"]
    missing = [row["well"] for row in sample_wells if row["status"] == "missing"]
    mixed = [row["well"] for row in sample_wells if row["status"] == "mixed"]
    mismatched = [
        {
            "well": row["well"],
            "registered": row["registered_sample_id"],
            "derived": row["derived_sample_id"],
            "sample_ids": row["sample_ids"],
            "paths": row["paths"],
        }
        for row in sample_wells
        if row["status"] == "mismatch"
    ]
    non_sample_flows = [
        row["well"]
        for row in target_wells
        if row["status"] in {"unregistered", "non_sample"}
        and (row["chain_ids"] or row["operations"])
    ]
    # Any well anywhere that ends up holding more than one distinct sample is
    # flagged so the UI can explain a downstream mix, not just a mixed target.
    mixed_anywhere = sorted(
        ({"plate": plate, "well": well} for (plate, well), prov in state.items()
         if len({entry["sample_id"] for entry in prov}) > 1),
        key=lambda item: (item["plate"], item["well"]),
    )

    issues: list[str] = []
    checks: list[dict[str, Any]] = []

    def add_check(check_id: str, ok: bool, label: str, detail: str = "") -> None:
        checks.append({"id": check_id, "ok": ok, "label": label, "detail": detail})
        if not ok:
            issues.append(f"{label}{('：' + detail) if detail else ''}")

    add_check(
        "operations_valid",
        not invalid_operations,
        "全部转移操作可执行（板/孔位已登记、读源时已有样本来源）",
        "；".join(
            f"操作 {item['operation']}（{item['source_plate']} {item['source_well']}"
            f" → {item['target_plate']} {item['target_well']}）：{item['reason']}"
            for item in invalid_operations
        ),
    )
    add_check(
        "target_sample_wells",
        bool(sample_wells),
        "待核板登记了样本孔",
        "" if sample_wells else "待核板孔位图没有样本孔",
    )
    add_check(
        "all_target_samples_traced",
        bool(sample_wells) and not missing,
        "每个待核样本孔都能追溯到来源",
        "缺少来源：" + "、".join(missing) if missing else "",
    )
    add_check(
        "no_unregistered_flows",
        not non_sample_flows,
        "转移只落入待核板已登记的样本孔",
        ("、".join(non_sample_flows) + " 收到样本但不是已登记样本孔")
        if non_sample_flows
        else "",
    )
    add_check(
        "single_source_per_well",
        not mixed,
        "每个待核样本孔只含同一样本（未混入不同样本）",
        "混样孔位：" + "、".join(mixed) if mixed else "",
    )
    add_check(
        "registration_matches",
        not mismatched,
        "追溯得到的样本编号与待核板登记一致",
        "；".join(
            f"{item['well']} 登记 {item['registered']}，追溯为 {item['derived']}"
            for item in mismatched
        )
        if mismatched
        else "",
    )
    # Mixing that happened upstream and was not carried downstream is still
    # reported, but it does not invalidate the downstream conclusion.
    target_mixed = set(mixed)
    upstream_mixed = [
        item
        for item in mixed_anywhere
        if not (item["plate"] == target_plate and item["well"] in target_mixed)
    ]
    if upstream_mixed:
        checks.append(
            {
                "id": "upstream_mix",
                "ok": True,
                "label": "上游孔位发生过混样（未流向下游）",
                "detail": "、".join(
                    f"{item['plate']} {item['well']}" for item in upstream_mixed
                ),
            }
        )

    trustworthy = not invalid_operations and bool(sample_wells)

    matches = [row["well"] for row in sample_wells if row["status"] == "match"]
    summary = {
        "operations_total": len(operations),
        "operations_applied": len(events),
        "operations_invalid": len(invalid_operations),
        "target_sample_wells": len(sample_wells),
        "matched": len(matches),
        "mismatched": len(mismatched),
        "missing": len(missing),
        "mixed": len(mixed),
        "chain_count": len(chain_list),
        "mixed_anywhere": len(mixed_anywhere),
        "trustworthy": trustworthy,
    }

    return {
        "source_plate_id": plate_ids.get(source_plate),
        "target_plate_id": plate_ids.get(target_plate),
        "conclusions_trustworthy": trustworthy,
        "checks": checks,
        "issues": issues,
        "summary": summary,
        "target_wells": target_wells,
        "chains": chain_list,
        "events": events,
        "invalid_operations": invalid_operations,
        "mismatches": mismatched,
        "missing_wells": missing,
        "mixed_wells": mixed,
        "mixed_anywhere": mixed_anywhere,
        "plate_roles": plate_roles,
    }

