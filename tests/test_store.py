import errno
import hashlib
import io
import json
import os

import pytest

from semcull.models import SemcullError, new_id
from semcull.store import Store, directory, write_json


def test_fidelity_lines_and_permissions(store, capture):
    data = "α\r\n🙂\nlast".encode()
    obs = capture(data)
    assert store.read(obs, (0, len(data))) == data
    assert store.line_range(obs, 2, 2) == (4, 9)
    assert store.line_range(obs, 3, 100) == (9, len(data))
    assert os.stat(store.root / "observations" / obs / "source.bin").st_mode & 0o777 == 0o600
    output = io.BytesIO()
    store.show(obs, output, all_source=True)
    assert output.getvalue() == data


@pytest.mark.parametrize("data", [b"", b" \r\n", b"a\x00b", b"a\xff", b"a\xf0\x9f"])
def test_invalid_capture_leaves_no_ready_source(store, data):
    with pytest.raises(SemcullError):
        store.capture(io.BytesIO(data))
    assert list((store.root / "observations").iterdir()) == []
    assert list((store.root / "staging").iterdir()) == []


def test_interrupted_capture_cleanup(store):
    class Broken:
        def read(self, size):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        store.capture(Broken())
    assert not list((store.root / "staging").iterdir())


def test_stream_reads_bounded_chunks(store):
    class Input:
        count = 0

        def read(self, size):
            assert size == 65536
            self.count += 1
            return b"a" * size if self.count <= 20 else b""

    obs = store.capture(Input())
    assert store.metadata(obs)["bytes"] == 20 * 65536
    assert store.line_range(obs, 1, 1) == (0, 20 * 65536)


def test_eviction_oldest_creation_not_read(tmp_path):
    with Store(tmp_path / "store", max_observations=2) as store:
        first = store.capture(io.BytesIO(b"one"))
        second = store.capture(io.BytesIO(b"two"))
        store.read(first, (0, 3))
        third = store.capture(io.BytesIO(b"three"))
        with pytest.raises(SemcullError):
            store.metadata(first)
        assert store.metadata(second) and store.metadata(third)
        assert store.delete_all() == {
            "schema_version": "1",
            "deleted": 2,
            "not_found": 0,
            "failed": 0,
        }
        assert store.delete_all()["deleted"] == 0


def test_session_scoped_stores_reuse_and_isolate_files(tmp_path):
    base = tmp_path / "semcull-user"
    one = base / "sessions" / hashlib.sha256(b"session-one").hexdigest()
    two = base / "sessions" / hashlib.sha256(b"session-two").hexdigest()

    with Store(one, session_scoped=True) as store:
        obs = store.capture(io.BytesIO(b"session one output"))
        assert os.stat(base).st_mode & 0o777 == 0o700
        assert os.stat(base / "sessions").st_mode & 0o777 == 0o700
        assert os.stat(one).st_mode & 0o777 == 0o700

    with Store(one, session_scoped=True) as store:
        assert store.read(obs, (0, 18)) == b"session one output"

    with Store(two, session_scoped=True) as store:
        with pytest.raises(SemcullError):
            store.metadata(obs)


def test_show_caps_and_actionable_ranges(store, capture):
    obs = capture(b"a" * 20000)
    output = io.BytesIO()
    warning = store.show(obs, output)
    assert len(output.getvalue()) == 16384
    assert warning["details"]["returned_bytes"] == [3616, 20000]
    assert "--bytes 0:3616" in warning["message"]
    output = io.BytesIO()
    warning = store.show(obs, output, byte_range=(0, 20000))
    assert warning["details"]["returned_bytes"] == [0, 16384]
    assert "--bytes 16384:20000" in warning["message"]


def test_show_utf8_and_exact_byte_mode(store, capture):
    obs = capture("🙂🙂".encode())
    output = io.BytesIO()
    store.show(obs, output, limit=5)
    assert output.getvalue() == "🙂".encode()
    output = io.BytesIO()
    store.show(obs, output, byte_range=(1, 3))
    assert output.getvalue() == "🙂".encode()[1:3]


def test_show_default_last_200_lines(store, capture):
    data = b"".join(f"{n}\n".encode() for n in range(300))
    obs = capture(data)
    output = io.BytesIO()
    assert store.show(obs, output) is None
    assert output.getvalue().startswith(b"100\n")


def test_no_overwrite_json(store):
    with directory(store.fd, "staging") as fd:
        write_json(fd, "test.json", {"value": 1})
        with pytest.raises(FileExistsError):
            write_json(fd, "test.json", {"value": 2})
    assert json.loads((store.root / "staging/test.json").read_text()) == {"value": 1}


def test_root_symlink_and_permissions_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        Store(link)
    target.chmod(0o755)
    with pytest.raises(SemcullError):
        Store(target)


def test_delete_does_not_follow_symlink(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_text("safe")
    obs = new_id("obs")
    (store.root / "observations" / obs).symlink_to(outside)
    result = store.delete_all()
    assert result["failed"] == 1 and sentinel.read_text() == "safe"


def test_source_symlink_rejected(store, capture, tmp_path):
    obs = capture(b"hello")
    source = store.root / "observations" / obs / "source.bin"
    source.unlink()
    other = tmp_path / "other"
    other.write_text("hello")
    source.symlink_to(other)
    with pytest.raises(OSError):
        store.read(obs, (0, 5))


def test_capture_disk_full_cleanup(store, monkeypatch):
    import semcull.store as module

    original = module.os.fdopen

    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def write(self, data):
            raise OSError(errno.ENOSPC, "simulated full disk")

    def fdopen(fd, mode, *args, **kwargs):
        stream = original(fd, mode, *args, **kwargs)
        return FailingWriter(stream) if mode == "wb" else stream

    monkeypatch.setattr(module.os, "fdopen", fdopen)
    with pytest.raises(OSError):
        store.capture(io.BytesIO(b"hello"))
    assert not list((store.root / "staging").iterdir())
    assert not list((store.root / "observations").iterdir())


def test_sparse_index_large_file(store, capture):
    obs = capture((b"a" * 100 + b"\n") * 12000)
    assert store.line_range(obs, 11999, 12000) == (11998 * 101, 12000 * 101)


def test_delete_all_snapshot_and_continue_after_failure(store, capture, monkeypatch):
    one = capture(b"one")
    capture(b"two")
    original = store.delete
    created = []

    def deleting(obs, **kwargs):
        if not created:
            created.append(capture(b"new"))
        if obs == one:
            raise OSError("simulated permission failure")
        return original(obs, **kwargs)

    monkeypatch.setattr(store, "delete", deleting)
    result = store.delete_all()
    assert result["deleted"] == 1 and result["failed"] == 1
    assert store.metadata(created[0])
    assert store.metadata(one)


def test_observation_id_collision_does_not_overwrite(store, capture, monkeypatch):
    obs = capture(b"original")
    import semcull.store as module

    original = module.new_id
    monkeypatch.setattr(
        module, "new_id", lambda prefix: obs if prefix == "obs" else original(prefix)
    )
    with pytest.raises(SemcullError, match="collision"):
        capture(b"replacement")
    assert store.read(obs, (0, 8)) == b"original"


def test_staging_from_other_invocation_is_not_swept(store, capture):
    stage = store.root / "staging" / new_id("obs")
    stage.mkdir(mode=0o700)
    capture()
    assert stage.exists()
