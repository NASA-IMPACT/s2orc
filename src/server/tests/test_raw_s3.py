"""The S3 read path (RAW_S3_MAP_PATH) returns the same records as the local-file path.

No network: the S3 client is replaced by one that serves byte ranges from the packed file.
"""

import io

import orjson
import pytest

import api
from repack_raw_to_s3 import build_map, pack_records

RECORDS = [
    {"corpusid": 1, "title": "Insulin resistance", "body": {"text": "x" * 5000}},
    {"corpusid": 2, "content": {"text": "naïve unicode ✓ " * 300, "annotations": {}}},
    {"corpusid": 3, "title": "short"},
]


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key, Range):
        start, end = map(int, Range.removeprefix("bytes=").split("-"))
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)][start:end + 1])}


@pytest.fixture
def raw(tmp_path, monkeypatch):
    data = tmp_path / "data"
    work = tmp_path / "work"
    data.mkdir()
    work.mkdir()
    src = data / "part-0001"
    lines = [orjson.dumps(r) + b"\n" for r in RECORDS]
    src.write_bytes(b"".join(lines))

    zst = work / "part-0001.zst"
    rows, raw_bytes, packed_bytes = pack_records(src, zst, level=3)
    assert raw_bytes == src.stat().st_size and packed_bytes == zst.stat().st_size
    rows.tofile(open(work / "part-0001.map", "wb"))
    map_path = tmp_path / "raw_s3_map.sqlite"
    build_map(work, map_path, "bucket", "raw/", 3)

    monkeypatch.setattr(api, "RAW_S3_MAP_PATH", str(map_path))
    monkeypatch.setattr(api, "ORIGINAL_DATA_PATH", str(data))
    monkeypatch.setattr(api, "_raw_local", api.threading.local())
    api._open_raw_s3()
    monkeypatch.setitem(api._raw_s3, "client", FakeS3({("bucket", "raw/part-0001.zst"): zst.read_bytes()}))
    offsets = [sum(map(len, lines[:i])) for i in range(len(lines))]
    return src, offsets


def test_s3_records_match_local(raw):
    src, offsets = raw
    for offset, expected in zip(offsets, RECORDS):
        from_s3 = api.read_json_from_s3(src.name, offset)
        assert from_s3 == expected == api.read_json_at_offset(str(src), offset)


def test_load_raw_document_uses_s3(raw):
    src, offsets = raw
    doc, version = api.load_raw_document(f"{src.name}___{offsets[1]}__v2")
    assert doc == RECORDS[1] and version == "v2"


def test_unknown_offset_and_file(raw):
    src, offsets = raw
    assert api.read_json_from_s3(src.name, offsets[1] + 1) is None
    with pytest.raises(FileNotFoundError):
        api.read_json_from_s3("no-such-file", 0)
