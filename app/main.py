"""FastAPI web application for plate absorbance publication."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, chain_service, config, parsing, service

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.data_dir().mkdir(parents=True, exist_ok=True)
    config.uploads_dir().mkdir(parents=True, exist_ok=True)
    from . import db

    db.init_db()
    yield


app = FastAPI(title="板式吸光度发布系统", version=__version__, lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)


def _render(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    payload = {"request": request, "version": __version__, **context}
    return templates.TemplateResponse(request, name, payload, status_code=status_code)


class _HttpError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


def _plate_or_404(plate_id: int) -> dict[str, Any]:
    try:
        return service.get_plate_snapshot(plate_id)
    except service.NotFoundError as exc:
        raise _HttpError(404, str(exc)) from exc


def _chain_or_404(verification_id: int, *, target_plate_id: int | None = None) -> dict[str, Any]:
    try:
        return chain_service.get_chain_verification(
            verification_id, target_plate_id=target_plate_id
        )
    except chain_service.NotFoundError as exc:
        raise _HttpError(404, str(exc)) from exc


@app.exception_handler(_HttpError)
async def _http_error_handler(request: Request, exc: _HttpError):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": exc.message}, status_code=exc.status_code)
    return _render(
        request,
        "error.html",
        {"message": exc.message, "status_code": exc.status_code},
        status_code=exc.status_code,
    )


@app.get("/healthz")
async def healthz():
    from . import db

    conn = db.connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"status": "ok", "version": __version__}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    plates = service.list_plates()
    return _render(
        request,
        "index.html",
        {"plates": plates, "verifications": chain_service.list_chain_verifications(limit=5)},
    )


@app.post("/plates")
async def create_plate(request: Request, name: str = Form("")):
    try:
        plate = service.create_plate(name)
    except parsing.InputError as exc:
        plates = service.list_plates()
        return _render(
            request,
            "index.html",
            {"plates": plates, "error": str(exc), "form_name": name},
            status_code=400,
        )
    except service.ConflictError as exc:
        plates = service.list_plates()
        return _render(
            request,
            "index.html",
            {"plates": plates, "error": str(exc), "form_name": name},
            status_code=409,
        )
    return RedirectResponse(f"/plates/{plate['id']}", status_code=303)


async def _read_upload(upload: UploadFile | None, label: str) -> tuple[bytes | None, str | None]:
    if upload is None or not upload.filename:
        return None, None
    limit = config.max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise parsing.InputError(f"{label}文件超过上限 {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise parsing.InputError(f"{label}文件为空")
    return data, upload.filename


@app.get("/plates/{plate_id}", response_class=HTMLResponse)
async def plate_detail(request: Request, plate_id: int, message: str | None = None, error: str | None = None):
    snapshot = _plate_or_404(plate_id)
    chain_summary = chain_service.plate_chain_summary(plate_id)
    return _render(
        request,
        "plate.html",
        {"snapshot": snapshot, "message": message, "error": error, **chain_summary},
    )


@app.post("/plates/{plate_id}/chains")
async def plate_chain_upload(
    plate_id: int,
    source_plate_id: int = Form(...),
    target_plate_id: int = Form(...),
    log_file: UploadFile | None = None,
    note: str = Form(""),
):
    """Upload a transfer log for a source/target pair selected on a plate."""
    _plate_or_404(plate_id)
    try:
        log_data, log_name = await _read_upload(log_file, "转移日志")
        if log_data is None:
            raise parsing.InputError("请选择要上传的转移日志文件")
        result = chain_service.verify_chain(
            source_plate_id,
            target_plate_id,
            log_data=log_data,
            log_filename=log_name,
            note=note,
        )
    except parsing.InputError as exc:
        return _render(
            request,
            "plate.html",
            {
                "snapshot": service.get_plate_snapshot(plate_id),
                "error": str(exc),
                **chain_service.plate_chain_summary(plate_id),
            },
            status_code=400,
        )
    except (chain_service.ConflictError, service.ConflictError) as exc:
        return _render(
            request,
            "plate.html",
            {
                "snapshot": service.get_plate_snapshot(plate_id),
                "error": str(exc),
                **chain_service.plate_chain_summary(plate_id),
            },
            status_code=409,
        )
    if result["outcome"] == "completed":
        message = f"核对完成（第 {result['verification_seq']} 次），已发布可信结果。"
    else:
        message = "核对失败，本次结论不可信，未替换上一版结果。"
    return RedirectResponse(
        f"/chains/{result['verification_id']}?message={_quote(message)}",
        status_code=303,
    )


@app.post("/plates/{plate_id}/uploads")
async def upload_inputs(
    plate_id: int,
    map_file: UploadFile | None = None,
    readings_file: UploadFile | None = None,
    note: str = Form(""),
):
    _plate_or_404(plate_id)
    try:
        map_data, map_name = await _read_upload(map_file, "孔位图")
        readings_data, readings_name = await _read_upload(readings_file, "孔位读数")
        result = service.ingest(
            plate_id,
            map_data=map_data,
            map_filename=map_name,
            readings_data=readings_data,
            readings_filename=readings_name,
            note=note,
        )
    except parsing.InputError as exc:
        return _render(
            request,
            "plate.html",
            {"snapshot": service.get_plate_snapshot(plate_id), "error": str(exc)},
            status_code=400,
        )
    except service.ConflictError as exc:
        return _render(
            request,
            "plate.html",
            {"snapshot": service.get_plate_snapshot(plate_id), "error": str(exc)},
            status_code=409,
        )

    if result["outcome"] == "published":
        message = f"已发布第 {result['publication_seq']} 版。"
    elif result["outcome"] == "failed":
        message = "重算未通过质控，未发布新版。"
        if result.get("active_is_stale"):
            message += " 上一版已标为过期，仍可查看。"
    else:
        message = result.get("message", "")
    return RedirectResponse(
        f"/plates/{plate_id}?message={_quote(message)}",
        status_code=303,
    )


@app.post("/plates/{plate_id}/recompute")
async def recompute(plate_id: int):
    _plate_or_404(plate_id)
    try:
        result = service.recompute(plate_id)
    except service.ConflictError as exc:
        return RedirectResponse(f"/plates/{plate_id}?error={_quote(str(exc))}", status_code=303)
    if result["outcome"] == "published":
        message = f"重算成功，已启用第 {result['publication_seq']} 版。"
    elif result["outcome"] == "failed":
        message = "重算未通过质控，旧版仍为过期状态。"
    else:
        message = result.get("message", "")
    return RedirectResponse(f"/plates/{plate_id}?message={_quote(message)}", status_code=303)


def _quote(value: str) -> str:
    return quote(value, safe="")


@app.get("/plates/{plate_id}/publications/{publication_id}", response_class=HTMLResponse)
async def publication_detail(request: Request, plate_id: int, publication_id: int):
    try:
        publication = service.get_publication(plate_id, publication_id)
    except service.NotFoundError as exc:
        raise _HttpError(404, str(exc)) from exc
    snapshot = _plate_or_404(plate_id)
    return _render(
        request,
        "publication.html",
        {"publication": publication, "plate": snapshot["plate"]},
    )


@app.get("/plates/{plate_id}/versions/{kind}/{version_id}/file")
async def download_version(plate_id: int, kind: str, version_id: int):
    try:
        path, row = service.get_version_file(plate_id, version_id, kind)
    except service.NotFoundError as exc:
        raise _HttpError(404, str(exc)) from exc
    filename = row["original_filename"] or path.name
    disposition = f'attachment; filename="{_ascii_filename(filename)}"'
    return Response(
        content=path.read_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": disposition,
            "X-Content-SHA256": row["sha256"],
        },
    )


def _ascii_filename(name: str) -> str:
    safe = name.replace('"', "_").replace("\\", "_").replace("/", "_")
    safe = safe.replace("\r", "_").replace("\n", "_")
    if all(32 <= ord(char) < 127 for char in safe):
        return safe
    return f"download-{quote(safe, safe='')}"


# ---------------------------------------------------------------------------
# Loading-chain verification (HTML)


def _chain_landing(message: str | None = None, error: str | None = None, **extra: Any):
    plates = service.list_plates()
    with_map = [plate for plate in plates if plate["map_version_count"] > 0]
    return {
        "plates": plates,
        "plates_with_map": with_map,
        "message": message,
        "error": error,
        **extra,
    }


@app.get("/chains", response_class=HTMLResponse)
async def chains_index(
    request: Request,
    message: str | None = None,
    error: str | None = None,
    target_plate_id: int | None = None,
):
    verifications = chain_service.list_chain_verifications(target_plate_id=target_plate_id)
    return _render(
        request,
        "chains.html",
        _chain_landing(
            message,
            error,
            verifications=verifications,
            filter_target=target_plate_id,
        ),
    )


@app.get("/chains/new", response_class=HTMLResponse)
async def chain_new(request: Request, error: str | None = None, source_plate_id: int | None = None, target_plate_id: int | None = None):
    return _render(
        request,
        "chain_new.html",
        _chain_landing(
            None,
            error,
            form_source=source_plate_id,
            form_target=target_plate_id,
        ),
    )


@app.post("/chains/new")
async def chain_create(
    source_plate_id: int = Form(...),
    target_plate_id: int = Form(...),
    log_file: UploadFile | None = None,
    note: str = Form(""),
):
    try:
        log_data, log_name = await _read_upload(log_file, "转移日志")
        if log_data is None:
            raise parsing.InputError("请选择要上传的转移日志文件")
        result = chain_service.verify_chain(
            source_plate_id,
            target_plate_id,
            log_data=log_data,
            log_filename=log_name,
            note=note,
        )
    except parsing.InputError as exc:
        return RedirectResponse(
            f"/chains/new?error={_quote(str(exc))}&source_plate_id={source_plate_id}"
            f"&target_plate_id={target_plate_id}",
            status_code=303,
        )
    except (chain_service.ConflictError, service.NotFoundError) as exc:
        return RedirectResponse(
            f"/chains/new?error={_quote(str(exc))}&source_plate_id={source_plate_id}"
            f"&target_plate_id={target_plate_id}",
            status_code=303,
        )
    if result["outcome"] == "completed":
        message = f"核对完成（第 {result['verification_seq']} 次），已发布可信结果。"
    else:
        message = "核对失败：本次结论不可信，没有替换既有结果。"
    return RedirectResponse(
        f"/chains/{result['verification_id']}?message={_quote(message)}",
        status_code=303,
    )


@app.get("/chains/{verification_id}", response_class=HTMLResponse)
async def chain_detail(
    request: Request,
    verification_id: int,
    message: str | None = None,
    error: str | None = None,
):
    verification = _chain_or_404(verification_id)
    return _render(
        request,
        "chain_detail.html",
        {"verification": verification, "message": message, "error": error},
    )


@app.post("/chains/{verification_id}/recompute")
async def chain_recompute(verification_id: int):
    verification = _chain_or_404(verification_id)
    try:
        result = chain_service.recompute_chain(
            verification["target_plate"]["id"], verification_id
        )
    except chain_service.ConflictError as exc:
        return RedirectResponse(
            f"/chains/{verification_id}?error={_quote(str(exc))}", status_code=303
        )
    if result["outcome"] in {"completed", "reaffirmed"}:
        message = f"核对已重新计算（第 {result['verification_seq']} 次）。"
    elif result["outcome"] == "unchanged":
        message = result.get("message", "输入未变化。")
    else:
        message = "核对失败，本次结果不可信；上一版若已过期仍可查看。"
    return RedirectResponse(
        f"/chains/{verification_id}?message={_quote(message)}", status_code=303
    )


@app.get("/chains/{verification_id}/log")
async def chain_log_file(verification_id: int):
    verification = _chain_or_404(verification_id)
    try:
        path, row = chain_service.get_log_file(verification_id)
    except chain_service.NotFoundError as exc:
        raise _HttpError(404, str(exc)) from exc
    filename = row["original_filename"] or path.name
    content_type = "text/csv; charset=utf-8"
    return Response(
        content=path.read_bytes(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{_ascii_filename(filename)}"',
            "X-Content-SHA256": row["sha256"],
        },
    )


# ---------------------------------------------------------------------------
# JSON API (used by tests and scripted imports)


@app.get("/api/plates")
async def api_plates():
    return {"ok": True, "plates": service.list_plates()}


@app.post("/api/plates")
async def api_create_plate(payload: dict):
    try:
        plate = service.create_plate(str(payload.get("name", "")))
    except parsing.InputError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except service.ConflictError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "plate": plate}, status_code=201)


@app.get("/api/plates/{plate_id}")
async def api_plate(plate_id: int):
    try:
        snapshot = service.get_plate_snapshot(plate_id)
    except service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    return {"ok": True, **snapshot}


@app.post("/api/plates/{plate_id}/uploads")
async def api_upload(
    plate_id: int,
    map_file: UploadFile | None = None,
    readings_file: UploadFile | None = None,
    note: str = Form(""),
):
    try:
        service.get_plate_snapshot(plate_id)
    except service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    try:
        map_data, map_name = await _read_upload(map_file, "孔位图")
        readings_data, readings_name = await _read_upload(readings_file, "孔位读数")
        result = service.ingest(
            plate_id,
            map_data=map_data,
            map_filename=map_name,
            readings_data=readings_data,
            readings_filename=readings_name,
            note=note,
        )
    except parsing.InputError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except service.ConflictError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "result": result, "plate": service.get_plate_snapshot(plate_id)}


@app.post("/api/plates/{plate_id}/recompute")
async def api_recompute(plate_id: int):
    try:
        service.get_plate_snapshot(plate_id)
    except service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    try:
        result = service.recompute(plate_id)
    except service.ConflictError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "result": result, "plate": service.get_plate_snapshot(plate_id)}


@app.get("/api/plates/{plate_id}/publications/{publication_id}")
async def api_publication(plate_id: int, publication_id: int):
    try:
        publication = service.get_publication(plate_id, publication_id)
    except service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    return {"ok": True, "publication": publication}


# ---------------------------------------------------------------------------
# JSON API for chain verification


@app.get("/api/chains")
async def api_chains(target_plate_id: int | None = None):
    return {
        "ok": True,
        "verifications": chain_service.list_chain_verifications(
            target_plate_id=target_plate_id
        ),
    }


@app.post("/api/chains")
async def api_chain_verify(
    source_plate_id: int = Form(...),
    target_plate_id: int = Form(...),
    log_file: UploadFile | None = None,
    note: str = Form(""),
):
    try:
        log_data, log_name = await _read_upload(log_file, "转移日志")
        if log_data is None:
            raise parsing.InputError("请上传转移日志文件")
        result = chain_service.verify_chain(
            source_plate_id,
            target_plate_id,
            log_data=log_data,
            log_filename=log_name,
            note=note,
        )
    except parsing.InputError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except (chain_service.ConflictError, service.ConflictError, service.NotFoundError) as exc:
        status = 404 if isinstance(exc, service.NotFoundError) else 409
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)
    return {"ok": True, "result": result}


@app.get("/api/chains/{verification_id}")
async def api_chain(verification_id: int):
    try:
        verification = chain_service.get_chain_verification(verification_id)
    except chain_service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    return {"ok": True, "verification": verification}


@app.post("/api/chains/{verification_id}/recompute")
async def api_chain_recompute(verification_id: int):
    try:
        verification = chain_service.get_chain_verification(verification_id)
        result = chain_service.recompute_chain(
            verification["target_plate"]["id"], verification_id
        )
    except chain_service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except chain_service.ConflictError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "result": result}


@app.get("/api/chains/{verification_id}/log")
async def api_chain_log(verification_id: int):
    try:
        path, row = chain_service.get_log_file(verification_id)
    except chain_service.NotFoundError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    return Response(
        content=path.read_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={"X-Content-SHA256": row["sha256"]},
    )
