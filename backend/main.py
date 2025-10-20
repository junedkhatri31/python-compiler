from __future__ import annotations

import asyncio
import io
import socket
import tarfile
import textwrap
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
STDIN_SENTINEL = "__PYRUNNER_STDIN_REQUEST__"
RUNNER_SCRIPT = textwrap.dedent(
    f"""
    import runpy
    import sys

    SENTINEL = "{STDIN_SENTINEL}"


    class StdinProxy:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def _notify(self):
            print(SENTINEL, flush=True)

        def readline(self, *args, **kwargs):
            self._notify()
            return self._wrapped.readline(*args, **kwargs)

        def read(self, *args, **kwargs):
            self._notify()
            return self._wrapped.read(*args, **kwargs)

        def readlines(self, *args, **kwargs):
            self._notify()
            return self._wrapped.readlines(*args, **kwargs)

        def __iter__(self):
            return self

        def __next__(self):
            line = self.readline()
            if line == "":
                raise StopIteration
            return line

        def __getattr__(self, name):
            return getattr(self._wrapped, name)


    def main() -> None:
        if len(sys.argv) < 2:
            raise SystemExit("Runner expects the target script path as the first argument.")

        target = sys.argv[1]
        sys.argv = [target, *sys.argv[2:]]
        sys.stdin = StdinProxy(sys.stdin)
        runpy.run_path(target, run_name="__main__")


    if __name__ == "__main__":
        main()
    """
).strip()

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
    script_bytes = contents.encode("utf-8")
    runner_bytes = RUNNER_SCRIPT.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        script_info = tarfile.TarInfo(name=filename)
        script_info.size = len(script_bytes)
        script_info.mode = 0o644
        tar.addfile(script_info, io.BytesIO(script_bytes))

        runner_info = tarfile.TarInfo(name="__runner__.py")
        runner_info.size = len(runner_bytes)
        runner_info.mode = 0o644
        tar.addfile(runner_info, io.BytesIO(runner_bytes))
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
        command=["python", "-u", "/tmp/__runner__.py", f"/tmp/{script_name}"],
        detach=True,
        stdin_open=True,
        tty=False,
        network_disabled=True,
        mem_limit="64m",
        working_dir="/tmp",
    )


def _process_stdout_buffer(buffer: str, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> str:
    sentinel = STDIN_SENTINEL
    position = 0
    while True:
        idx = buffer.find(sentinel, position)
        if idx == -1:
            break
        chunk = buffer[position:idx]
        if chunk:
            loop.call_soon_threadsafe(queue.put_nowait, ("stdout", chunk))
        loop.call_soon_threadsafe(queue.put_nowait, ("stdin_request", ""))
        position = idx + len(sentinel)

    remaining = buffer[position:]
    keep = max(0, len(sentinel) - 1)
    if len(remaining) > keep:
        emit = remaining[:-keep]
        if emit:
            loop.call_soon_threadsafe(queue.put_nowait, ("stdout", emit))
        leftover = remaining[-keep:]
    else:
        leftover = remaining

    return leftover


def _stream_logs(stream, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
    stdout_buffer = ""
    try:
        for stdout, stderr in stream:
            if stdout:
                stdout_buffer += stdout.decode("utf-8", errors="replace")
                stdout_buffer = _process_stdout_buffer(stdout_buffer, loop, queue)
            if stderr:
                loop.call_soon_threadsafe(queue.put_nowait, ("stderr", stderr.decode("utf-8", errors="replace")))
    finally:
        if stdout_buffer:
            loop.call_soon_threadsafe(queue.put_nowait, ("stdout", stdout_buffer))
        loop.call_soon_threadsafe(queue.put_nowait, ("__EOF__", ""))


def _open_stdin_socket(container: docker.models.containers.Container):
    return container.attach_socket(params={"stdin": 1, "stream": 1})


def _close_stdin_socket(sock) -> None:
    if sock is None:
        return
    with suppress(OSError):
        sock._sock.shutdown(socket.SHUT_WR)  # type: ignore[attr-defined]
    sock.close()


async def _write_to_stdin(sock, lock: asyncio.Lock, data: str) -> None:
    if sock is None:
        return
    if data is None:
        return

    async with lock:
        def _send() -> None:
            try:
                sock._sock.sendall(data.encode("utf-8"))
            except (BrokenPipeError, OSError):
                pass

        await asyncio.to_thread(_send)


async def _consume_client_messages(
    websocket: WebSocket,
    sock,
    lock: asyncio.Lock,
    container: docker.models.containers.Container,
) -> None:
    if lock is None:
        lock = asyncio.Lock()
    while True:
        message = await websocket.receive_json()
        msg_type = message.get("type")
        if msg_type == "stdin":
            await _write_to_stdin(sock, lock, message.get("data", ""))
        elif msg_type == "stdin_close":
            await asyncio.to_thread(_close_stdin_socket, sock)
            break
        elif msg_type == "stop":
            await websocket.send_json(
                {
                    "message": "Termination requested by client. Stopping container…",
                    "status": "stopping",
                }
            )
            with suppress(APIError, DockerException):
                await asyncio.to_thread(container.kill)
            await asyncio.to_thread(_close_stdin_socket, sock)
            break


async def _forward_logs(queue: asyncio.Queue, websocket: WebSocket) -> None:
    while True:
        stream_kind, payload = await queue.get()
        if stream_kind == "__EOF__":
            break
        if stream_kind == "stdin_request":
            await websocket.send_json({"stdin_requested": True})
        else:
            await websocket.send_json({stream_kind: payload})


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
    stdin_socket = None
    stdin_lock: asyncio.Lock | None = None
    queue: asyncio.Queue[Tuple[str, str]] = asyncio.Queue()
    stream_task: asyncio.Task | None = None
    forward_task: asyncio.Task | None = None
    stdin_task: asyncio.Task | None = None
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

        try:
            stdin_socket = _open_stdin_socket(container)
        except DockerException as exc:
            await websocket.send_json(
                {
                    "stderr": f"Failed to attach to container stdin: {exc}",
                    "status": "error",
                }
            )
            return
        stdin_lock = asyncio.Lock()

        log_stream = container.attach(stream=True, stdout=True, stderr=True, demux=True, logs=True)
        stream_task = asyncio.create_task(asyncio.to_thread(_stream_logs, log_stream, loop, queue))
        forward_task = asyncio.create_task(_forward_logs(queue, websocket))

        await asyncio.to_thread(container.start)
        await websocket.send_json({"message": "Container started. Streaming output…" })

        if stdin_socket is not None and stdin_lock is not None:
            stdin_task = asyncio.create_task(_consume_client_messages(websocket, stdin_socket, stdin_lock, container))

            if isinstance(initial_stdin, str) and initial_stdin:
                initial_payload = initial_stdin if initial_stdin.endswith("\n") else f"{initial_stdin}\n"
                await _write_to_stdin(stdin_socket, stdin_lock, initial_payload)

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
            with suppress(asyncio.CancelledError, WebSocketDisconnect):
                await forward_task

        if stdin_task is not None:
            if not stdin_task.done():
                stdin_task.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect):
                await stdin_task

        if stdin_socket is not None:
            with suppress(Exception):
                _close_stdin_socket(stdin_socket)

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
