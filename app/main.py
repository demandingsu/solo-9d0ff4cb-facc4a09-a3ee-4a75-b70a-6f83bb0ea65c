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

from . import __version__, config, parsing, service

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
    return _render(request, "index.html", {"plates": plates})


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
    return _render(
        request,
        "plate.html",
        {"snapshot": snapshot, "message": message, "error": error},
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
