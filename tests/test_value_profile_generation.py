"""值域再生成：显式快照、真实校验路径与失败不覆盖；仅替外部测量/SQL。"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from sqlglot import exp, parse_one

from data import snapshot, value_profile

SHA = "abcdef1"
COUNTS = {"dwd": {"dim_store": 3, "dim_item": 3, "date_dim": 3, "store_sales": 3}}
IDS = {"dwd": {"dim_store": 11, "dim_item": 12, "date_dim": 13, "store_sales": 14}}
RANGE = "2000-01-01T00:00:00~2000-12-31T23:59:59"


class ColumnExecutor:
    """替外部 Doris 返回计数与频次；SQL 仍经生产 Guard 校验。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_after: int | None = None

    def __call__(self, sql: str) -> list[tuple[object, ...]]:
        self.calls.append(sql)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise RuntimeError("测试数据库中断")
        query = parse_one(sql, read="doris")
        assert isinstance(query, exp.Select)
        assert query.args.get("limit") is not None
        first = query.expressions[0]
        if isinstance(first, exp.Count):
            return [(1, 3, 3)]
        assert isinstance(first, exp.Column)
        values = {
            "s_state": "TN",
            "s_city": "Midway",
            "s_store_sk": 1,
            "i_category": "Books",
            "i_brand": "brand1",
        }
        return [(values[first.name], 3)]


@pytest.fixture
def profile_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    metas = tmp_path / "snapshots"
    metas.mkdir()
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "sample.txt").write_bytes(b"public")
    meta = {
        "sha": SHA,
        "data_range": RANGE,
        "raw_size_bytes": 6,
        "row_counts": COUNTS,
        "snapshot_ids": IDS,
    }
    (metas / f"{SHA}.meta.json").write_text(json.dumps(meta))
    output = tmp_path / "values"
    output.mkdir()
    existing = output / "atlas_retail_analytics.s_state.json"
    existing.write_text(json.dumps({"snapshot_sha": "abcdef0", "aliases": {"tennessee": "TN"}}))
    monkeypatch.setattr(value_profile, "SNAPSHOT_DIR", metas)
    monkeypatch.setattr(snapshot, "measure_row_counts", lambda: COUNTS)
    monkeypatch.setattr(snapshot, "measure_snapshot_ids", lambda: IDS)
    monkeypatch.setattr(snapshot, "measure_data_range", lambda: RANGE)
    return {"raw_dir": raw, "values_dir": output, "executor": ColumnExecutor()}


def _files(path: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in path.iterdir()}


def test_explicit_snapshot_preserves_aliases_and_binds_every_profile(
    profile_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_GIT_SHA", "bbbbbbb")
    payloads = value_profile.generate(["retail"], 200, False, snapshot_sha=SHA, **profile_env)
    assert {p["snapshot_sha"] for p in payloads} == {SHA}
    assert len(payloads) == 5
    state = next(p for p in payloads if p["field"] == "s_state")
    assert state["aliases"] == {"tennessee": "TN"}
    assert state["values"] == [{"value": "TN", "count": 3}]
    assert state["row_count"] == 3
    assert state["null_count"] == 0
    assert len(_files(profile_env["values_dir"])) == 5
    saved = json.loads(
        (profile_env["values_dir"] / "atlas_retail_analytics.s_state.json").read_text()
    )
    assert saved == state


@pytest.mark.parametrize("phase", ["before", "after"])
def test_fingerprint_drift_never_overwrites_profiles(
    profile_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    initial = _files(profile_env["values_dir"])
    measurements = 0

    def measure() -> dict[str, dict[str, int]]:
        nonlocal measurements
        measurements += 1
        if phase == "before" or measurements > 1:
            return {"dwd": {"dim_store": 4}}
        return COUNTS

    monkeypatch.setattr(snapshot, "measure_row_counts", measure)
    with pytest.raises(RuntimeError, match="快照"):
        value_profile.generate(["retail"], 200, False, snapshot_sha=SHA, **profile_env)
    assert _files(profile_env["values_dir"]) == initial
    assert bool(profile_env["executor"].calls) == (phase == "after")


def test_sql_failure_never_leaves_partially_refreshed_profiles(profile_env: dict[str, Any]) -> None:
    initial = _files(profile_env["values_dir"])
    profile_env["executor"].fail_after = 3
    with pytest.raises(RuntimeError, match="数据库中断"):
        value_profile.generate(["retail"], 200, False, snapshot_sha=SHA, **profile_env)
    assert _files(profile_env["values_dir"]) == initial


def test_dangling_alias_prevents_every_write(profile_env: dict[str, Any]) -> None:
    target = profile_env["values_dir"] / "atlas_retail_analytics.s_store_sk.json"
    target.write_text(json.dumps({"aliases": {"unknown": "999"}}))
    initial = _files(profile_env["values_dir"])
    with pytest.raises(RuntimeError, match="人工别名"):
        value_profile.generate(["retail"], 200, False, snapshot_sha=SHA, **profile_env)
    assert _files(profile_env["values_dir"]) == initial


def test_dry_run_measures_without_writing(profile_env: dict[str, Any]) -> None:
    initial = _files(profile_env["values_dir"])
    payloads = value_profile.generate(["retail"], 200, True, snapshot_sha=SHA, **profile_env)
    assert len(payloads) == 5
    assert profile_env["executor"].calls
    assert _files(profile_env["values_dir"]) == initial


@pytest.mark.parametrize("sha", ["../outside", "", "abc0000"])
def test_invalid_or_missing_snapshot_never_queries(profile_env: dict[str, Any], sha: str) -> None:
    initial = _files(profile_env["values_dir"])
    with pytest.raises((ValueError, RuntimeError)):
        value_profile.generate(["retail"], 200, False, snapshot_sha=sha, **profile_env)
    assert profile_env["executor"].calls == []
    assert _files(profile_env["values_dir"]) == initial


def test_explicit_raw_directory_is_real_and_counted(tmp_path: Path) -> None:
    raw = tmp_path / "public-data"
    raw.mkdir()
    (raw / "part1").write_bytes(b"abc")
    (raw / "part2").write_bytes(b"de")
    assert snapshot.raw_size_bytes(raw) == 5
    with pytest.raises(FileNotFoundError):
        snapshot.raw_size_bytes(tmp_path / "missing")


def test_cli_explicit_snapshot_and_raw_directory(
    profile_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        value_profile, "execute_sql", lambda sql: (profile_env["executor"](sql), [])
    )
    monkeypatch.setattr(
        value_profile,
        "generate",
        partial(value_profile.generate, values_dir=profile_env["values_dir"]),
    )
    assert (
        value_profile.main(
            [
                "--snapshot-sha",
                SHA,
                "--raw-dir",
                str(profile_env["raw_dir"]),
                "--domain",
                "retail",
                "--dry-run",
            ]
        )
        == 0
    )
    assert f"快照 {SHA}" in capsys.readouterr().out


def test_default_uses_code_identity_not_latest_snapshot(
    profile_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ATLAS_GIT_SHA", SHA)
    payloads = value_profile.generate(["retail"], 200, True, **profile_env)
    assert {p["snapshot_sha"] for p in payloads} == {SHA}
    monkeypatch.setenv("ATLAS_GIT_SHA", "fffffff")
    with pytest.raises(RuntimeError, match="快照"):
        value_profile.generate(["retail"], 200, False, **profile_env)


def test_linked_snapshot_never_queries(profile_env: dict[str, Any]) -> None:
    link = value_profile.SNAPSHOT_DIR / "aaaaaaa.meta.json"
    link.symlink_to(value_profile.SNAPSHOT_DIR / f"{SHA}.meta.json")
    with pytest.raises(RuntimeError, match="快照"):
        value_profile.generate(["retail"], 200, False, snapshot_sha="aaaaaaa", **profile_env)
    assert profile_env["executor"].calls == []


def test_snapshot_metadata_change_during_sampling_prevents_writes(
    profile_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = _files(profile_env["values_dir"])

    def measure() -> dict[str, dict[str, int]]:
        if profile_env["executor"].calls:
            path = value_profile.SNAPSHOT_DIR / f"{SHA}.meta.json"
            meta = json.loads(path.read_text())
            meta["notes"] = "采集期间被另一个进程改写"
            path.write_text(json.dumps(meta))
        return COUNTS

    monkeypatch.setattr(snapshot, "measure_row_counts", measure)
    with pytest.raises(RuntimeError, match="快照"):
        value_profile.generate(["retail"], 200, False, snapshot_sha=SHA, **profile_env)
    assert _files(profile_env["values_dir"]) == initial


def test_linked_profile_never_overwrites_other_files(profile_env: dict[str, Any]) -> None:
    outside = profile_env["values_dir"].parent / "unrelated.json"
    outside.write_text('{"aliases": {}}')
    link = profile_env["values_dir"] / "atlas_retail_analytics.s_store_sk.json"
    link.symlink_to(outside)
    initial = _files(profile_env["values_dir"])
    with pytest.raises(RuntimeError, match="符号链接"):
        value_profile.generate(["retail"], 200, False, snapshot_sha=SHA, **profile_env)
    assert _files(profile_env["values_dir"]) == initial
    assert outside.read_text() == '{"aliases": {}}'
