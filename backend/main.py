from __future__ import annotations

import asyncio
import io
import tarfile
import uuid
from contextlib import suppress
from typing import Any, Dict, Tuple, Union

import docker
from docker.errors import APIError, DockerException, ImageNotFound
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState

DOCKER_IMAGE = "python:3-alpine"
CONTAINER_TIMEOUT_SECONDS = 120

app = FastAPI()

origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root() -> Dict[str, str]:
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: Union[str, None] = None) -> Dict[str, Union[int, None, str]]:
    return {"item_id": item_id, "q": q}


def _package_code_tar(filename: str, contents: str) -> bytes:
    file_bytes = contents.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        tarinfo = tarfile.TarInfo(name=filename)
        tarinfo.size = len(file_bytes)
        tarinfo.mode = 0o644
        tar.addfile(tarinfo, io.BytesIO(file_bytes))
    tar_stream.seek(0)
    return tar_stream.read()


def _copy_code_into_container(container: docker.models.containers.Container, script_name: str, code: str) -> None:
    archive = _package_code_tar(script_name, code)
    container.put_archive(path="/tmp", data=archive)


async def _ensure_image(client: docker.DockerClient) -> None:
    try:
        await asyncio.to_thread(client.images.get, DOCKER_IMAGE)
    except ImageNotFound:
        await asyncio.to_thread(client.images.pull, DOCKER_IMAGE)


def _create_container(client: docker.DockerClient, script_name: str) -> docker.models.containers.Container:
    return client.containers.create(
        image=DOCKER_IMAGE,
        command=["python", "-u", f"/tmp/{script_name}"],
        detach=True,
        stdin_open=True,
        tty=False,
        network_disabled=True,
        mem_limit="256m",
        working_dir="/tmp",
    )


def _stream_logs(stream, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
    try:
        for stdout, stderr in stream:
            if stdout:
                loop.call_soon_threadsafe(queue.put_nowait, ("stdout", stdout.decode("utf-8", errors="replace")))
            if stderr:
                loop.call_soon_threadsafe(queue.put_nowait, ("stderr", stderr.decode("utf-8", errors="replace")))
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, ("__EOF__", ""))


async def _forward_logs(queue: asyncio.Queue, websocket: WebSocket) -> None:
    while True:
        stream_kind, payload = await queue.get()
        if stream_kind == "__EOF__":
            break
        await websocket.send_json({stream_kind: payload})


def _send_initial_stdin(container: docker.models.containers.Container, data: str) -> None:
    if not data:
        return
    sock = container.attach_socket(params={"stdin": 1, "stream": 1})
    try:
        sock._sock.sendall(data.encode("utf-8"))
        with suppress(OSError):
            sock._sock.shutdown(1)  # type: ignore[attr-defined]
    finally:
        sock.close()


async def _wait_for_container(container: docker.models.containers.Container) -> Dict[str, Any]:
    wait_future = asyncio.to_thread(container.wait)
    return await asyncio.wait_for(wait_future, timeout=CONTAINER_TIMEOUT_SECONDS)


async def _cleanup_container(container: docker.models.containers.Container) -> None:
    with suppress(APIError, DockerException):
        await asyncio.to_thread(container.remove, force=True)


@app.websocket("/ws/run")
async def run_python(websocket: WebSocket):
    await websocket.accept()

    container = None
    client: docker.DockerClient | None = None
    queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()
    stream_task: asyncio.Task | None = None
    forward_task: asyncio.Task | None = None
    log_stream = None

    try:
        payload = await websocket.receive_json()
        code = payload.get("code", "")
        initial_stdin = payload.get("stdin", "")

        if not isinstance(code, str) or not code.strip():
            await websocket.send_json(
                {
                    "stderr": "No Python code received. Please submit code to execute.",
                    "status": "error",
                }
            )
            return

        try:
            client = docker.from_env()
        except DockerException as exc:
            await websocket.send_json(
                {
                    "stderr": f"Backend is unable to reach the Docker daemon: {exc}",
                    "status": "error",
                }
            )
            return

        await websocket.send_json({"message": "Preparing runtime environment…" })
        await _ensure_image(client)

        script_name = f"{uuid.uuid4().hex}.py"
        container = _create_container(client, script_name)
        await asyncio.to_thread(_copy_code_into_container, container, script_name, code)

        loop = asyncio.get_running_loop()

        log_stream = container.attach(stream=True, stdout=True, stderr=True, demux=True, logs=True)
        stream_task = asyncio.create_task(asyncio.to_thread(_stream_logs, log_stream, loop, queue))
        forward_task = asyncio.create_task(_forward_logs(queue, websocket))

        await asyncio.to_thread(container.start)
        await websocket.send_json({"message": "Container started. Streaming output…" })

        if isinstance(initial_stdin, str) and initial_stdin:
            await asyncio.to_thread(_send_initial_stdin, container, initial_stdin)

        wait_result = await _wait_for_container(container)
        status_code = wait_result.get("StatusCode", 0) if isinstance(wait_result, dict) else 0

        # Ensure all logs are flushed
        if stream_task is not None:
            await stream_task
        if forward_task is not None:
            await forward_task

        await websocket.send_json(
            {
                "message": "Execution finished.",
                "exit_code": status_code,
            }
        )
    except WebSocketDisconnect:
        if container is not None:
            with suppress(APIError, DockerException):
                await asyncio.to_thread(container.kill)
        return
    except asyncio.TimeoutError:
        if container is not None:
            with suppress(APIError, DockerException):
                await asyncio.to_thread(container.kill)
        await websocket.send_json(
            {
                "stderr": "Execution timed out.",
                "status": "timeout",
            }
        )
    except Exception as exc:  # pragma: no cover
        await websocket.send_json(
            {
                "stderr": f"An unexpected error occurred: {exc}",
                "status": "error",
            }
        )
    finally:
        if stream_task is not None and not stream_task.done():
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

        if forward_task is not None and not forward_task.done():
            await queue.put(("__EOF__", ""))
            forward_task.cancel()
            with suppress(asyncio.CancelledError):
                await forward_task

        if container is not None:
            await _cleanup_container(container)

        if log_stream is not None and hasattr(log_stream, "close"):
            with suppress(Exception):
                log_stream.close()

        if client is not None:
            with suppress(Exception):
                client.close()

        with suppress(Exception):
            if not websocket.application_state == WebSocketState.DISCONNECTED:
                await websocket.close()
