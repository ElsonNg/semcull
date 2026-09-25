"""Private temporary archives with immutable JSON records.

All managed traversal uses directory file descriptors and O_NOFOLLOW. Holding
a descriptor does not promise that another process won't evict the archive.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import shutil
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from .models import SemcullError, new_id, strict_json, validate_id
from .windows import trim_utf8

CHUNK = 65536
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _private(fd: int, *, directory: bool = False):
    info = os.fstat(fd)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SemcullError(
            "unsafe_store", "Store artifacts must be private and owned by the current user.", 4
        )


@contextmanager
def directory(parent: int, name: str):
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        _private(fd, directory=True)
        yield fd
    finally:
        os.close(fd)


def write_json(parent: int, name: str, value: dict):
    """Publish a complete file using an exclusive hard link, never overwrite."""
    temporary = f".tmp-{new_id('write')}"
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _private_child(parent: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        _private(fd, directory=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_json(parent: int, name: str) -> dict:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _private(fd)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            result = strict_json(stream.read())
        if not isinstance(result, dict):
            raise ValueError("Expected object")
        return result
    except (ValueError, UnicodeError):
        raise SemcullError("corrupt_record", "A saved record is invalid.", 4) from None
    finally:
        os.close(fd)


class Store:
    def __init__(
        self, root: Path, max_observations: int = 200, *, session_scoped: bool = False
    ):
        self.root = root
        self.max_observations = max_observations
        if session_scoped:
            # Create and verify the user's namespace, sessions directory, and
            # hashed session directory relative to already-open descriptors.
            base = root.parent.parent
            try:
                parent_fd = os.open(base.parent, DIR_FLAGS)
            except OSError as exc:
                raise SemcullError(
                    "unsafe_store", "Store parent directory is unavailable or unsafe.", 4
                ) from exc
            try:
                base_fd = _private_child(parent_fd, base.name)
                try:
                    sessions_fd = _private_child(base_fd, root.parent.name)
                    try:
                        self.fd = _private_child(sessions_fd, root.name)
                    finally:
                        os.close(sessions_fd)
                finally:
                    os.close(base_fd)
            finally:
                os.close(parent_fd)
        else:
            # Only the dedicated final component is created. Do not create
            # arbitrary custom parent trees or chmod a directory that belongs
            # to the caller.
            try:
                root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            self.fd = os.open(root, DIR_FLAGS)
        try:
            _private(self.fd, directory=True)
            for name in ("observations", "staging", "trash"):
                try:
                    os.mkdir(name, 0o700, dir_fd=self.fd)
                except FileExistsError:
                    pass
                with directory(self.fd, name):
                    pass
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    @contextmanager
    def observation(self, observation_id: str):
        validate_id(observation_id, "obs")
        try:
            with directory(self.fd, "observations") as parent:
                with directory(parent, observation_id) as fd:
                    yield fd
        except FileNotFoundError:
            raise SemcullError(
                "not_found",
                "Observation or requested artifact is unavailable.",
                observation_id=observation_id,
            ) from None

    @contextmanager
    def evaluation(self, observation_id: str, evaluation_id: str):
        validate_id(evaluation_id, "eval")
        with self.observation(observation_id) as obs:
            with directory(obs, "evaluations") as parent:
                with directory(parent, evaluation_id) as fd:
                    yield fd

    def metadata(self, observation_id: str) -> dict:
        with self.observation(observation_id) as fd:
            value = read_json(fd, "metadata.json")
            if (
                value.get("observation_id") != observation_id
                or type(value.get("bytes")) is not int
                or value["bytes"] < 0
                or type(value.get("lines")) is not int
                or value["lines"] < 1
                or type(value.get("created_ns")) is not int
            ):
                raise SemcullError("corrupt_record", "Invalid observation metadata.", 4)
            return value

    def _snapshot(self) -> list[tuple[int, str]]:
        result = []
        with directory(self.fd, "observations") as parent:
            names = os.listdir(parent)
        for name in names:
            try:
                validate_id(name, "obs")
            except SemcullError:
                continue
            try:
                result.append((self.metadata(name)["created_ns"], name))
            except SemcullError as exc:
                if exc.code != "not_found":
                    raise
        return sorted(result)

    def cleanup(self):
        self._clear_trash()
        entries = self._snapshot()
        for _, name in entries[: max(0, len(entries) - self.max_observations + 1)]:
            self.delete(name, missing_ok=True)

    def _clear_trash(self):
        # Never sweep staging from another process: without locks or leases we
        # cannot safely distinguish an abandoned capture from a slow live one.
        with directory(self.fd, "trash") as trash:
            for name in os.listdir(trash):
                try:
                    validate_id(name, "obs")
                except SemcullError:
                    continue
                try:
                    with directory(trash, name):
                        pass
                    shutil.rmtree(name, dir_fd=trash)
                except FileNotFoundError:
                    pass

    def capture(self, stream: BinaryIO) -> str:
        self.cleanup()
        observation_id = new_id("obs")
        created = time.time_ns()
        with directory(self.fd, "staging") as staging:
            os.mkdir(observation_id, 0o700, dir_fd=staging)
            try:
                with directory(staging, observation_id) as fd:
                    digest = hashlib.sha256()
                    decoder = codecs.getincrementaldecoder("utf-8")("strict")
                    total = newlines = line_start = 0
                    nonempty = False
                    last = b""
                    stride, next_checkpoint = 1024 * 1024, 1024 * 1024
                    checkpoints = [[0, 1]]
                    source_fd = os.open(
                        "source.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd
                    )
                    with os.fdopen(source_fd, "wb") as target:
                        while chunk := stream.read(CHUNK):
                            if b"\x00" in chunk:
                                raise SemcullError(
                                    "invalid_input", "Binary input is not supported."
                                )
                            text = decoder.decode(chunk)
                            nonempty = nonempty or bool(text.strip())
                            target.write(chunk)
                            digest.update(chunk)
                            cursor = 0
                            while (position := chunk.find(b"\n", cursor)) >= 0:
                                newlines += 1
                                line_start = total + position + 1
                                if line_start >= next_checkpoint:
                                    checkpoints.append([line_start, newlines + 1])
                                    if len(checkpoints) >= 4096:
                                        checkpoints = checkpoints[::2]
                                        stride *= 2
                                    next_checkpoint = line_start + stride
                                cursor = position + 1
                            total += len(chunk)
                            last = chunk[-1:]
                        decoder.decode(b"", final=True)
                    if not nonempty:
                        raise SemcullError("empty_input", "Source is empty or whitespace-only.")
                    metadata = {
                        "schema_version": "1",
                        "observation_id": observation_id,
                        "created_ns": created,
                        "bytes": total,
                        "lines": newlines + (last != b"\n"),
                        "sha256": digest.hexdigest(),
                    }
                    write_json(fd, "metadata.json", metadata)
                    write_json(
                        fd, "source-index.json", {"schema_version": "1", "checkpoints": checkpoints}
                    )
                    os.mkdir("evaluations", 0o700, dir_fd=fd)
                with directory(self.fd, "observations") as ready:
                    # UUID destination is private; refuse collisions explicitly.
                    try:
                        os.stat(observation_id, dir_fd=ready, follow_symlinks=False)
                    except FileNotFoundError:
                        os.rename(
                            observation_id, observation_id, src_dir_fd=staging, dst_dir_fd=ready
                        )
                    else:
                        raise SemcullError(
                            "id_collision",
                            "Observation ID collision; capture was not published.",
                            4,
                        )
            except BaseException as exc:
                try:
                    shutil.rmtree(observation_id, dir_fd=staging)
                except FileNotFoundError:
                    pass
                except OSError:
                    raise SemcullError(
                        "cleanup_failed",
                        "Capture failed and its incomplete staging directory could not be removed.",
                        4,
                    ) from None
                if isinstance(exc, UnicodeError):
                    raise SemcullError("invalid_input", "Source must be valid UTF-8.") from None
                raise
        return observation_id

    @contextmanager
    def source(self, observation_id: str):
        expected = self.metadata(observation_id)["bytes"]
        with self.observation(observation_id) as fd:
            source_fd = os.open("source.bin", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                _private(source_fd)
                if os.fstat(source_fd).st_size != expected:
                    raise SemcullError("corrupt_source", "Saved source size changed.", 4)
                with os.fdopen(source_fd, "rb", closefd=False) as stream:
                    yield stream
            finally:
                os.close(source_fd)

    def read(self, observation_id: str, bounds: tuple[int, int]) -> bytes:
        start, end = bounds
        with self.source(observation_id) as stream:
            stream.seek(start)
            result = stream.read(end - start)
        if len(result) != end - start:
            raise SemcullError("corrupt_source", "Saved source changed during retrieval.", 4)
        return result

    def line_range(self, observation_id: str, start: int, end: int) -> tuple[int, int]:
        metadata = self.metadata(observation_id)
        if start < 1 or end < start or start > metadata["lines"]:
            raise SemcullError(
                "invalid_range", "Line range is reversed or starts outside the source."
            )
        end = min(end, metadata["lines"])
        with self.observation(observation_id) as fd:
            checkpoints = read_json(fd, "source-index.json").get("checkpoints")
        if not isinstance(checkpoints, list) or not checkpoints:
            raise SemcullError("corrupt_record", "Invalid source index.", 4)
        offset, line = 0, 1
        previous = (-1, 0)
        for entry in checkpoints:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or any(type(x) is not int for x in entry)
                or not previous[0] < entry[0] <= metadata["bytes"]
                or not previous[1] < entry[1] <= metadata["lines"] + 1
            ):
                raise SemcullError("corrupt_record", "Invalid source index entry.", 4)
            previous = tuple(entry)
            if entry[1] <= start:
                offset, line = entry
        left = offset if line == start else None
        with self.source(observation_id) as stream:
            stream.seek(offset)
            while chunk := stream.read(CHUNK):
                cursor = 0
                while (position := chunk.find(b"\n", cursor)) >= 0:
                    boundary = offset + position + 1
                    line += 1
                    if line == start:
                        left = boundary
                    if line == end + 1:
                        return left, boundary
                    cursor = position + 1
                offset += len(chunk)
        if left is None:
            raise SemcullError("corrupt_source", "Source index does not match source.", 4)
        return left, metadata["bytes"]

    def show(
        self,
        observation_id: str,
        output: BinaryIO,
        *,
        lines=None,
        byte_range=None,
        all_source=False,
        limit=16384,
        command_prefix="semcull",
    ) -> dict | None:
        meta = self.metadata(observation_id)
        tail = lines is None and byte_range is None and not all_source
        if lines is not None:
            requested = self.line_range(observation_id, *lines)
        elif byte_range is not None:
            start, end = byte_range
            if start < 0 or end < start or start > meta["bytes"]:
                raise SemcullError("invalid_range", "Byte range is outside the source.")
            requested = (start, min(end, meta["bytes"]))
        elif all_source:
            requested = (0, meta["bytes"])
        else:
            requested = self.line_range(observation_id, max(1, meta["lines"] - 199), meta["lines"])
        start, end = requested
        if all_source:
            with self.source(observation_id) as stream:
                while chunk := stream.read(CHUNK):
                    output.write(chunk)
            return None
        if tail:
            start = max(start, end - limit)
        else:
            end = min(end, start + limit)
        data = self.read(observation_id, (start, end))
        if byte_range is None:
            start, data = trim_utf8(data, start, tail=tail)
        end = start + len(data)
        output.write(data)
        if (start, end) == requested:
            return None
        next_range = (requested[0], start) if tail else (end, requested[1])
        message = (
            f"Output capped at {limit} bytes; edge lines may be partial. "
            f"Retrieve omitted text with: {command_prefix} show {observation_id} --bytes {next_range[0]}:{next_range[1]}. "
            f"For the entire source without a cap, use: {command_prefix} show {observation_id} --all."
        )
        return SemcullError("output_truncated", message, returned_bytes=[start, end]).diagnostic(
            "warning", observation_id=observation_id
        )

    def create_evaluation(self, observation_id: str, request: dict) -> str:
        evaluation_id = new_id("eval")
        with self.observation(observation_id) as obs:
            with directory(obs, "evaluations") as parent:
                os.mkdir(evaluation_id, 0o700, dir_fd=parent)
                with directory(parent, evaluation_id) as fd:
                    os.mkdir("windows", 0o700, dir_fd=fd)
                    write_json(fd, "request.json", request)
        return evaluation_id

    def save_window(self, observation_id: str, evaluation_id: str, value: dict):
        with self.evaluation(observation_id, evaluation_id) as fd:
            with directory(fd, "windows") as windows:
                write_json(windows, f"{new_id('win')}.json", value)

    def save_result(self, observation_id: str, evaluation_id: str, result: dict):
        with self.evaluation(observation_id, evaluation_id) as fd:
            write_json(fd, "result.json", result)

    def select_evaluation(self, observation_id: str, evaluation_id: str | None) -> str:
        if evaluation_id:
            with self.evaluation(observation_id, evaluation_id) as fd:
                read_json(fd, "request.json")
            return evaluation_id
        with self.observation(observation_id) as obs, directory(obs, "evaluations") as fd:
            names = []
            for name in os.listdir(fd):
                try:
                    validate_id(name, "eval")
                except SemcullError:
                    continue
                names.append(name)
        if not names:
            raise SemcullError(
                "not_found", "No saved evaluation exists; supply an intent to check."
            )
        if len(names) != 1:
            raise SemcullError("evaluation_required", "Multiple evaluations exist. Specify --eval.")
        return self.select_evaluation(observation_id, names[0])

    def evaluation_request(self, observation_id: str, evaluation_id: str) -> dict:
        with self.evaluation(observation_id, evaluation_id) as fd:
            return read_json(fd, "request.json")

    def evaluation_result(self, observation_id: str, evaluation_id: str) -> dict:
        with self.evaluation(observation_id, evaluation_id) as fd:
            request = read_json(fd, "request.json")
            try:
                return read_json(fd, "result.json")
            except FileNotFoundError:
                records = []
                with directory(fd, "windows") as windows:
                    for name in os.listdir(windows):
                        if name.startswith("win_") and name.endswith(".json"):
                            validate_id(name[:-5], "win")
                            records.append(read_json(windows, name))
                return {
                    "schema_version": "1",
                    "observation_id": observation_id,
                    "evaluation_id": evaluation_id,
                    "run_status": "incomplete",
                    "request": request,
                    "window_records": records,
                    "results": sorted(
                        [r["result"] for r in records if "result" in r],
                        key=lambda r: r["examined_bytes"],
                    ),
                    "reason": "final_manifest_unavailable",
                }

    def delete(self, observation_id: str, *, missing_ok=False) -> dict:
        validate_id(observation_id, "obs")
        with directory(self.fd, "observations") as ready, directory(self.fd, "trash") as trash:
            try:
                with directory(ready, observation_id):
                    pass
                target = new_id("obs")
                os.rename(observation_id, target, src_dir_fd=ready, dst_dir_fd=trash)
            except FileNotFoundError:
                if not missing_ok:
                    raise SemcullError("not_found", "Observation is already missing.") from None
                return {
                    "schema_version": "1",
                    "observation_id": observation_id,
                    "status": "not_found",
                }
            shutil.rmtree(target, dir_fd=trash)
        return {"schema_version": "1", "observation_id": observation_id, "status": "deleted"}

    def delete_all(self) -> dict:
        counts = {"deleted": 0, "not_found": 0, "failed": 0}
        with directory(self.fd, "observations") as ready:
            names = os.listdir(ready)
        for name in names:
            try:
                validate_id(name, "obs")
            except SemcullError:
                continue
            try:
                counts[self.delete(name, missing_ok=True)["status"]] += 1
            except (OSError, SemcullError):
                counts["failed"] += 1
        return {"schema_version": "1", **counts}
