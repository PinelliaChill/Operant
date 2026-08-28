from __future__ import annotations

import hashlib
import io
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from operant.artifacts import (
    ArtifactConfigurationError,
    ArtifactCorruptionError,
    ArtifactNotFoundError,
    ArtifactSecurityError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactTooLargeError,
    ArtifactValidationError,
)


def _blob_path(root: Path, content_hash: str) -> Path:
    return root / ArtifactStore.storage_key_for_hash(content_hash)


def _content_with_hash_prefix(prefix: str) -> bytes:
    for counter in range(100_000):
        candidate = f"artifact-{counter}".encode()
        if hashlib.sha256(candidate).hexdigest().startswith(prefix):
            return candidate
    raise AssertionError("failed to find content hash prefix")


def test_put_read_verify_and_controlled_storage_key(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"canonical artifact bytes\x00\xff"
    expected_hash = hashlib.sha256(content).hexdigest()

    with ArtifactStore(root) as store:
        blob = store.put_bytes(content)
        assert blob.content_hash == expected_hash
        assert blob.size_bytes == len(content)
        assert (
            blob.storage_key == f"sha256/{expected_hash[:2]}/{expected_hash[2:4]}/{expected_hash}"
        )
        assert not Path(blob.storage_key).is_absolute()
        assert ".." not in Path(blob.storage_key).parts
        assert store.read(expected_hash, len(content)) == content
        assert store.verify(expected_hash, len(content)) is None

    assert _blob_path(root, expected_hash).read_bytes() == content
    assert stat.S_IMODE(_blob_path(root, expected_hash).stat().st_mode) & 0o077 == 0
    assert list((root / ".tmp").iterdir()) == []


def test_empty_content_is_addressed_and_verified(tmp_path: Path) -> None:
    expected_hash = hashlib.sha256(b"").hexdigest()
    with ArtifactStore(tmp_path / "artifacts") as store:
        blob = store.put_stream(io.BytesIO(b""))
        assert (blob.content_hash, blob.size_bytes) == (expected_hash, 0)
        assert store.read(expected_hash, 0) == b""


def test_repeated_and_concurrent_writes_deduplicate(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"same bytes from many processes" * 100
    expected_hash = hashlib.sha256(content).hexdigest()

    def write_once(_: int) -> tuple[str, int, str]:
        with ArtifactStore(root) as store:
            blob = store.put_bytes(content)
            return blob.content_hash, blob.size_bytes, blob.storage_key

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(write_once, range(48)))

    assert set(results) == {
        (
            expected_hash,
            len(content),
            ArtifactStore.storage_key_for_hash(expected_hash),
        )
    }
    shard = _blob_path(root, expected_hash).parent
    assert [entry.name for entry in shard.iterdir()] == [expected_hash]
    assert list((root / ".tmp").iterdir()) == []


def test_one_store_supports_concurrent_writers(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"shared store descriptor" * 100
    with ArtifactStore(root) as store, ThreadPoolExecutor(max_workers=8) as executor:
        blobs = list(executor.map(store.put_bytes, [content] * 32))
        assert {blob.content_hash for blob in blobs} == {hashlib.sha256(content).hexdigest()}
        assert store.read(blobs[0].content_hash, blobs[0].size_bytes) == content
    assert list((root / ".tmp").iterdir()) == []


def test_streaming_is_bounded_and_failures_clean_temporary_files(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    secret_marker = "do-not-leak-this-content"
    with ArtifactStore(root, max_size_bytes=8, chunk_size=3) as store:
        with pytest.raises(ArtifactTooLargeError) as too_large:
            store.put_stream([b"1234", b"5678", secret_marker.encode()])
        assert secret_marker not in str(too_large.value)

        with pytest.raises(ArtifactValidationError, match="stream must yield bytes"):
            store.put_stream([b"ok", bytearray(b"not-bytes")])  # type: ignore[list-item]

        class BrokenReader:
            def read(self, _: int) -> bytes:
                raise OSError(f"reader failed at {root}/{secret_marker}")

        with pytest.raises(ArtifactStoreError, match="storage operation failed") as broken:
            store.put_stream(BrokenReader())  # type: ignore[arg-type]
        assert str(root) not in str(broken.value)
        assert secret_marker not in str(broken.value)

    assert list((root / ".tmp").iterdir()) == []


def test_detects_content_and_size_corruption(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"original artifact"
    with ArtifactStore(root) as store:
        blob = store.put_bytes(content)
        target = _blob_path(root, blob.content_hash)

        target.write_bytes(b"x" * len(content))
        with pytest.raises(ArtifactCorruptionError, match="integrity verification"):
            store.verify(blob.content_hash, blob.size_bytes)
        with pytest.raises(ArtifactCorruptionError, match="integrity verification"):
            store.put_bytes(content)

        target.write_bytes(content + b"extra")
        with pytest.raises(ArtifactCorruptionError, match="integrity verification"):
            store.read(blob.content_hash, blob.size_bytes)


def test_rejects_invalid_hash_size_and_closed_store(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", max_size_bytes=4)
    with pytest.raises(ArtifactValidationError, match="content hash"):
        store.verify("../" + "a" * 61, 0)
    with pytest.raises(ArtifactValidationError, match="content hash"):
        store.verify("A" * 64, 0)
    with pytest.raises(ArtifactValidationError, match="artifact size"):
        store.verify("a" * 64, -1)
    with pytest.raises(ArtifactValidationError, match="artifact size"):
        store.verify("a" * 64, True)
    with pytest.raises(ArtifactTooLargeError, match="size limit"):
        store.verify("a" * 64, 5)
    with pytest.raises(ArtifactValidationError, match="content must be bytes"):
        store.put_bytes("not bytes")  # type: ignore[arg-type]
    store.close()
    store.close()
    with pytest.raises(ArtifactStoreError, match="closed"):
        store.put_bytes(b"x")


@pytest.mark.parametrize("root_name", ["relative", "../escape"])
def test_rejects_relative_or_traversing_root(tmp_path: Path, root_name: str) -> None:
    root = Path(root_name) if root_name == "relative" else tmp_path / root_name
    with pytest.raises(ArtifactConfigurationError, match="invalid artifact store root"):
        ArtifactStore(root)


def test_rejects_root_and_ancestor_symlinks_without_external_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactSecurityError, match="boundary violation") as root_error:
        ArtifactStore(root_link)
    assert str(outside) not in str(root_error.value)

    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactSecurityError, match="boundary violation"):
        ArtifactStore(ancestor_link / "nested" / "artifacts")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("managed_name", ["sha256", ".tmp"])
def test_rejects_symlinked_managed_directories(
    tmp_path: Path,
    managed_name: str,
) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / managed_name).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactSecurityError, match="boundary violation"):
        ArtifactStore(root)
    assert list(outside.iterdir()) == []


def test_detects_managed_directory_replacement_after_open(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    with ArtifactStore(root) as store:
        (root / "sha256").rename(root / "sha256-original")
        (root / "sha256").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ArtifactSecurityError, match="boundary violation"):
            store.put_bytes(b"must remain inside")

    assert list(outside.iterdir()) == []


def test_publish_rejects_root_replacement_race_without_deleting_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    detached_root = tmp_path / "artifacts-detached"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_marker = outside / "keep.txt"
    outside_marker.write_text("outside remains untouched")
    content = b"root replacement race"
    content_hash = hashlib.sha256(content).hexdigest()

    with ArtifactStore(root) as store:
        root_marker = root / "keep.txt"
        root_marker.write_text("detached root remains recoverable")

        def swapping_source():
            yield content[:8]
            root.rename(detached_root)
            root.symlink_to(outside, target_is_directory=True)
            yield content[8:]

        with pytest.raises(ArtifactSecurityError, match="boundary violation"):
            store.put_stream(swapping_source())

    assert outside_marker.read_text() == "outside remains untouched"
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]
    assert (detached_root / "keep.txt").read_text() == "detached root remains recoverable"
    assert _blob_path(detached_root, content_hash).read_bytes() == content
    assert list((detached_root / ".tmp").iterdir()) == []


def test_rejects_symlinked_shard_and_target_without_following_them(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    outside.mkdir()
    content = _content_with_hash_prefix("ab")
    content_hash = hashlib.sha256(content).hexdigest()

    with ArtifactStore(root) as store:
        (root / "sha256" / content_hash[:2]).symlink_to(outside, target_is_directory=True)
        with pytest.raises(ArtifactSecurityError, match="boundary violation"):
            store.put_bytes(content)
    assert list(outside.iterdir()) == []

    root2 = tmp_path / "artifacts-target"
    external_file = tmp_path / "external-file"
    external_file.write_bytes(b"external must not change")
    with ArtifactStore(root2) as store:
        target = _blob_path(root2, content_hash)
        target.parent.mkdir(parents=True)
        target.symlink_to(external_file)
        with pytest.raises(ArtifactSecurityError, match="boundary violation"):
            store.put_bytes(content)
    assert external_file.read_bytes() == b"external must not change"


def test_rejects_nonregular_blob_and_missing_blob(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content_hash = hashlib.sha256(b"directory target").hexdigest()
    with ArtifactStore(root) as store:
        with pytest.raises(ArtifactNotFoundError, match="not found"):
            store.verify(content_hash, 0)

        target = _blob_path(root, content_hash)
        target.mkdir(parents=True)
        with pytest.raises(ArtifactSecurityError, match="boundary violation"):
            store.verify(content_hash, 0)


def test_case_folded_hash_and_managed_aliases_are_not_accepted(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = _content_with_hash_prefix("ab")
    content_hash = hashlib.sha256(content).hexdigest()
    with (
        ArtifactStore(root) as store,
        pytest.raises(ArtifactValidationError, match="content hash"),
    ):
        store.verify(content_hash.upper(), len(content))

    wrong_case_shard = root / "sha256" / content_hash[:2].upper()
    wrong_case_shard.mkdir()
    lower_case_shard = root / "sha256" / content_hash[:2]
    if lower_case_shard.exists() and wrong_case_shard.samefile(lower_case_shard):
        with (
            ArtifactStore(root) as store,
            pytest.raises(ArtifactSecurityError, match="boundary violation"),
        ):
            store.put_bytes(content)
    else:
        # A case-sensitive filesystem keeps the controlled lowercase shard
        # distinct and never traverses the pre-existing uppercase directory.
        with ArtifactStore(root) as store:
            assert store.put_bytes(content).content_hash == content_hash


def test_constructor_rejects_invalid_limits(tmp_path: Path) -> None:
    for invalid in (0, -1, True):
        with pytest.raises(ArtifactConfigurationError, match="size limit"):
            ArtifactStore(tmp_path / f"size-{invalid}", max_size_bytes=invalid)
        with pytest.raises(ArtifactConfigurationError, match="chunk size"):
            ArtifactStore(tmp_path / f"chunk-{invalid}", chunk_size=invalid)


def test_source_can_be_chunk_iterable(tmp_path: Path) -> None:
    content = b"chunked input"
    with ArtifactStore(tmp_path / "artifacts", chunk_size=2) as store:
        blob = store.put_stream([b"chunk", b"ed ", b"input"])
        assert store.read(blob.content_hash, blob.size_bytes) == content


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_fifo_target_is_rejected_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    content = b"fifo target"
    content_hash = hashlib.sha256(content).hexdigest()
    with ArtifactStore(root) as store:
        target = _blob_path(root, content_hash)
        target.parent.mkdir(parents=True)
        os.mkfifo(target)
        with pytest.raises(ArtifactSecurityError, match="boundary violation"):
            store.put_bytes(content)
