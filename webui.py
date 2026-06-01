from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path
import socket
import threading
import webbrowser

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from queue_service import AnswerQueueService


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "webui_static"
service = AnswerQueueService(BASE_DIR)


class WarmUpRequest(BaseModel):
    provider_keys: list[str] = Field(default_factory=list)
    browser: str = "msedge"
    show_browser: bool = False


class EnqueueRequest(BaseModel):
    question_text: str
    provider_keys: list[str] = Field(default_factory=list)
    timeout_seconds: int = 90
    browser: str = "msedge"
    show_browser: bool = False
    max_parallel_tasks: int = Field(default=1, ge=1, le=8)


class ForceStopRequest(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


class ClearQueueRequest(BaseModel):
    task_ids: list[str] = Field(default_factory=list)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            service.shutdown()

    app = FastAPI(title="理论题作答助手 Web UI", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def get_state(selected_task_id: str | None = None) -> dict:
        return service.get_snapshot(selected_task_id)

    @app.post("/api/sessions/open")
    def open_sessions(payload: WarmUpRequest) -> dict:
        try:
            service.warm_up(payload.provider_keys, payload.browser, payload.show_browser)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/enqueue")
    def enqueue_tasks(payload: EnqueueRequest) -> dict:
        try:
            if payload.timeout_seconds < 20:
                raise RuntimeError("单站超时建议至少 20 秒。")
            task_ids = service.enqueue_questions(
                payload.question_text,
                payload.provider_keys,
                payload.timeout_seconds,
                payload.browser,
                payload.show_browser,
                payload.max_parallel_tasks,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "task_ids": task_ids}

    @app.post("/api/tasks/force-stop")
    def force_stop(payload: ForceStopRequest) -> dict:
        task_ids = service.force_stop(payload.task_ids or None)
        return {"ok": True, "task_ids": task_ids}

    @app.post("/api/tasks/clear")
    def clear_queue(payload: ClearQueueRequest) -> dict:
        task_ids = service.clear_queue(payload.task_ids or None)
        return {"ok": True, "task_ids": task_ids}

    @app.post("/api/sessions/close")
    def close_sessions() -> dict:
        try:
            service.close_sessions()
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    return app


app = create_app()


def find_available_port(preferred_port: int, host: str = "127.0.0.1") -> int:
    for port in range(preferred_port, preferred_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"从 {preferred_port} 开始没有找到可用端口。")


def main() -> None:
    parser = argparse.ArgumentParser(description="理论题作答助手 Web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    port = find_available_port(args.port, args.host)
    url = f"http://{args.host}:{port}"
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    print(f"Web UI running at {url}")
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
