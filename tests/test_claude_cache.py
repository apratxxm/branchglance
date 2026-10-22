import json
import struct
from datetime import datetime, timezone
from email.utils import format_datetime

import pytest
import zstandard

from token_tracker.claude_cache import MAX_BODY, _body, _stream, reset_info

NOW = 1_789_315_200
ORG = "00000000-0000-0000-0000-000000000001"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def record(tmp_path, *, org=ORG, observed=NOW, five=NOW+3600, week=NOW+3*86400, codec="zstd", suffix="", slot=0):
    cache = tmp_path / "Cache" / "Cache_Data"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / "data_1"
    index = bytearray(path.read_bytes()) if path.exists() else bytearray(8192+256*8)
    struct.pack_into("<IIhhi", index, 0, 0xC104CAC3, 0x20000, 1, 0, 256)
    key = f"1/0/https://claude.ai/api/organizations/{org}/usage{suffix}".encode()
    body = json.dumps({"five_hour": {"utilization": 50, "resets_at": iso(five) if five else None},
                       "seven_day": {"utilization": 40, "resets_at": iso(week) if week else None}}).encode()
    if codec == "zstd":
        body = zstandard.ZstdCompressor(write_content_size=False).compress(body)
    headers = ("HTTP/1.1 200 OK\0date: " + format_datetime(datetime.fromtimestamp(observed, timezone.utc), usegmt=True)
               + "\0content-encoding: " + codec + "\0").encode()
    base = 8192+slot*256
    struct.pack_into("<iI4i4I", index, base+32, len(key), 0, len(headers), len(body), 0, 0,
                     0x80000000+slot*2+1, 0x80000000+slot*2+2, 0, 0)
    index[base+96:base+96+len(key)] = key
    path.write_bytes(index)
    (cache / f"f_{slot*2+1:06x}").write_bytes(headers)
    (cache / f"f_{slot*2+2:06x}").write_bytes(body)
    return cache


def samples(*extra):
    return [{"org": ORG, "t": NOW*1000, "u": {"fh": 50, "sd": 40}}, *extra]


@pytest.mark.parametrize("codec", ["zstd", "identity"])
def test_account_scoped_cache_reads_independent_deadlines(tmp_path, codec):
    record(tmp_path, codec=codec)
    record(tmp_path, org=ORG[:-1]+"2", observed=NOW+10, slot=1)
    result = reset_info(tmp_path, samples(), NOW+15)
    assert result == {"fiveHour": NOW+3600, "weekly": NOW+3*86400, "observedAt": NOW}
    assert ORG not in json.dumps(result)


def test_latest_response_wins_and_null_is_not_replaced_by_old_schedule(tmp_path):
    record(tmp_path, suffix="?skip_spend=1")
    record(tmp_path, observed=NOW+60, five=None, slot=1)
    assert reset_info(tmp_path, samples(), NOW+70)["fiveHour"] is None
    assert reset_info(tmp_path, samples(), NOW+70)["weekly"] == NOW+3*86400


def test_expired_and_reset_windows_are_independently_rejected(tmp_path):
    record(tmp_path)
    assert reset_info(tmp_path, samples(), NOW+4000)["fiveHour"] is None
    assert reset_info(tmp_path, samples(), NOW+4000)["lastKnownFiveHour"] == NOW+3600
    later = {"org": ORG, "t": (NOW+200)*1000, "u": {"fh": 2, "sd": 42}}
    result = reset_info(tmp_path, samples(later), NOW+300)
    assert result["fiveHour"] is None and result["weekly"] == NOW+3*86400
    # A rise then drop invalidates a schedule even if utilization exceeds its initial value.
    high = {"org": ORG, "t": (NOW+100)*1000, "u": {"fh": 90, "sd": 70}}
    low = {"org": ORG, "t": (NOW+200)*1000, "u": {"fh": 60, "sd": 60}}
    assert reset_info(tmp_path, samples(high, low), NOW+300)["weekly"] is None


def test_mismatched_percent_impossible_schedule_and_future_response(tmp_path):
    record(tmp_path, five=NOW+86400)
    assert reset_info(tmp_path, samples(), NOW)["fiveHour"] is None
    mismatched = [{"org": ORG, "t": NOW*1000, "u": {"fh": 50, "sd": 90}}]
    assert reset_info(tmp_path, mismatched, NOW)["weekly"] is None
    assert reset_info(tmp_path, samples(), NOW-1000)["observedAt"] is None


def test_truncated_or_evicted_cache_is_unknown(tmp_path):
    cache = record(tmp_path)
    (cache / "f_000002").write_bytes(b"partial")
    assert reset_info(tmp_path, samples(), NOW)["weekly"] is None
    record(tmp_path)
    path = cache / "data_1"
    data = bytearray(path.read_bytes())
    struct.pack_into("<i", data, 8192+20, 1)
    path.write_bytes(data)
    assert reset_info(tmp_path, samples(), NOW)["observedAt"] is None


def test_bounded_decompression_and_block_addresses(tmp_path):
    huge = zstandard.ZstdCompressor().compress(b" "*(MAX_BODY+100))
    assert _body(huge, b"zstd") is None
    assert _body(b"broken", b"zstd") is None
    assert _stream(tmp_path, 0xA0010000, 300) == b""  # one 256-byte allocation
    block = bytearray(8192+1024)
    block[8192+512:8192+515] = b"abc"
    (tmp_path / "data_1").write_bytes(block)
    assert _stream(tmp_path, 0xA0010002, 3) == b"abc"
