"""ARCHIVE-UPLOAD-ROBUST-1: a big Spaces upload in parts, each part retried on its own.

A file over THRESHOLD is sent as an S3 multipart upload (CreateMultipartUpload, UploadPart,
CompleteMultipartUpload): parts of PART_BYTES (larger when the file would need more than
MAX_PARTS), PARALLEL at a time, each with its own signed sha256 and Content-MD5 like a single
PUT. Every request has a socket timeout (spaces.TIMEOUT_S: a connection that hangs fails after
that long without a byte) and every PUT body a deadline (DEADLINE_S: a trickle fails too). With
`Spaces(attempts=N)`, a request that fails on the way (a reset, a timeout, a 5xx, a 429, or
RequestTimeout / SlowDown) is sent again up to N times in all, after BACKOFF_S: only that
part, never the whole file. A part that still fails aborts the upload (so no parts are left
behind in the bucket) and raises. Spaces' limits are kept: a single PUT up to 5 GiB, parts of
5 MiB to 5 GiB, at most 10,000 parts.

The object's ETag is then the MD5 of its parts' MD5s with "-<parts>" (multipart_etag), which
Spaces.put_verified checks in place of the file's MD5.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from . import ArchiveError

log = logging.getLogger(__name__)

THRESHOLD = 64 << 20  # files over this go in parts
PART_BYTES = 16 << 20
MAX_PARTS = 10_000
PARALLEL = 4  # parts in flight at once: at most PARALLEL * PART_BYTES in memory
ATTEMPTS = 4  # the raw_upload job's tries of each request
BACKOFF_S = (2.0, 8.0, 30.0)  # the wait before the 2nd, 3rd and later tries
DEADLINE_S = 300.0  # the longest one PUT body (a part, or a small file) may take to send
SEND = 1 << 20
RETRIED = ("RequestTimeout", "SlowDown", "InternalError", "ServiceUnavailable")


class SpacesError(ArchiveError):
    """A Spaces request that failed: `kind` names how (the exception or the answer's code),
    `transient` whether the same request may get through when sent again."""

    def __init__(self, message: str, kind: str, transient: bool) -> None:
        super().__init__(message)
        self.kind, self.transient = kind, transient


def answered(message: str, status: int, code: str | None) -> SpacesError:
    """The error of an unexpected answer: transient for a 5xx, a 429 and the retried codes."""
    kind = code or str(status)
    return SpacesError(message, kind, status >= 500 or status == 429 or kind in RETRIED)


def retry(call: Callable[[], Any], attempts: int, sleep: Callable[[float], None]) -> Any:
    """call(), sent again after a transient SpacesError, at most `attempts` times in all."""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except SpacesError as exc:
            if not exc.transient or attempt >= attempts:
                raise
            wait_s = BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1]
            log.warning("%s; try %d of %d in %.0f s", exc, attempt + 1, attempts, wait_s)
            sleep(wait_s)
    raise ArchiveError("no attempt")  # attempts < 1


def timed(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """A request body's chunks; TimeoutError once DEADLINE_S passed since the first."""
    end = time.monotonic() + DEADLINE_S
    for chunk in chunks:
        if time.monotonic() > end:
            raise TimeoutError("the request body took longer than its deadline")
        yield chunk


def file_chunks(path: Path, length: int) -> Iterator[bytes]:
    """The first `length` bytes of a file, SEND at a time."""
    with Path(path).open("rb") as file:
        while length > 0:
            chunk = file.read(min(SEND, length))
            if not chunk:
                raise ArchiveError(f"{Path(path).name} got shorter while it was sent")
            length -= len(chunk)
            yield chunk


def _slices(data: bytes) -> Iterator[memoryview]:
    view = memoryview(data)
    return (view[at : at + SEND] for at in range(0, len(data), SEND))


def spans(total: int) -> list[tuple[int, int]]:
    """Each part's (offset, length) of a file of `total` bytes."""
    size = max(PART_BYTES, -(-total // MAX_PARTS))
    return [(offset, min(size, total - offset)) for offset in range(0, total, size)]


def multipart_etag(path: Path, total: int) -> str:
    """The ETag Spaces gives the file uploaded in parts by `upload`."""
    digests = b""
    with Path(path).open("rb") as file:
        for offset, length in spans(total):
            part = os.pread(file.fileno(), length, offset)
            digests += hashlib.md5(part, usedforsecurity=False).digest()
    return f"{hashlib.md5(digests, usedforsecurity=False).hexdigest()}-{len(spans(total))}"


def upload(spaces: Any, key: str, path: Path, total: int, headers: dict[str, str]) -> int:
    """Store the file at key in parts (headers: the object's ACL and x-amz-meta- values);
    returns the number of parts. Any failure aborts the upload and raises."""
    started = {"Content-Type": "application/octet-stream", **headers}
    _, _, data = spaces.call("POST", key, query={"uploads": ""}, headers=started)
    found = re.search(rb"<UploadId>([^<]+)</UploadId>", data)
    if found is None:
        raise ArchiveError(f"Spaces POST {key} answered without an UploadId")
    upload_id = found.group(1).decode()
    try:
        etags = _parts(spaces, key, path, upload_id, total)
        _complete(spaces, key, upload_id, etags)
    except BaseException:
        _abort(spaces, key, upload_id)
        raise
    return len(etags)


def _parts(spaces: Any, key: str, path: Path, upload_id: str, total: int) -> list[str]:
    """Send every part, PARALLEL at a time; the first part that fails cancels those not sent."""
    with Path(path).open("rb") as file, ThreadPoolExecutor(PARALLEL) as pool:
        futures = [
            pool.submit(_part, spaces, key, (upload_id, number), file.fileno(), span)
            for number, span in enumerate(spans(total), 1)
        ]
        done, pending = wait(futures, return_when=FIRST_EXCEPTION)
        failed = next((f.exception() for f in futures if f in done and f.exception()), None)
        if failed is not None:
            for future in pending:
                future.cancel()
            raise failed
        return [future.result() for future in futures]


def _part(spaces: Any, key: str, part: tuple[str, int], fd: int, span: tuple[int, int]) -> str:
    """Send one part (part = (upload id, part number)); its ETag, checked to be its MD5."""
    (upload_id, number), (offset, length) = part, span
    data = os.pread(fd, length, offset)
    if len(data) != length:
        raise ArchiveError(f"{Path(key).name} got shorter while it was sent")
    md5 = hashlib.md5(data, usedforsecurity=False)
    headers = {
        "Content-Length": str(length),
        "Content-MD5": base64.b64encode(md5.digest()).decode(),
    }
    _, answer, _ = spaces.call(
        "PUT",
        key,
        query={"partNumber": str(number), "uploadId": upload_id},
        headers=headers,
        body=lambda: timed(_slices(data)),
        payload=hashlib.sha256(data).hexdigest(),
    )
    etag = answer.get("etag", "").strip('"')
    if etag != md5.hexdigest():
        raise ArchiveError(f"Spaces part {number} of {key} answered another ETag than its MD5")
    return etag


def _complete(spaces: Any, key: str, upload_id: str, etags: list[str]) -> None:
    parts = "".join(
        f'<Part><PartNumber>{n}</PartNumber><ETag>"{etag}"</ETag></Part>'
        for n, etag in enumerate(etags, 1)
    )
    body = f"<CompleteMultipartUpload>{parts}</CompleteMultipartUpload>".encode()
    # S3 may answer 200 and then an error: sent again like a 5xx.
    spaces.call(
        "POST",
        key,
        query={"uploadId": upload_id},
        body=lambda: body,
        payload=hashlib.sha256(body).hexdigest(),
        check=lambda data: b"<Error>" not in data,
    )


def _abort(spaces: Any, key: str, upload_id: str) -> None:
    """Drop the upload's parts (never an object); a failure is only logged."""
    try:
        spaces.call("DELETE", key, query={"uploadId": upload_id}, expect=(200, 204, 404))
    except ArchiveError as exc:
        log.warning("the parts of %s were not dropped: %s", key, exc)
