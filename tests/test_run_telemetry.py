"""ADR-0031 T05e：事件写入与 OTel 导出分离（账本 T05 验收 255/256 行）。

口径
----
- **OTel 故障不阻断运行**（256 行「OTel 失败仍可继续」）：埋点实现爆炸时，
  run 照常执行、终态落账、视图与结果正文照常可用；隔离实现于
  observability/otel.record_turn（吞异常 + 告警一次），本文件以真实 API/
  Guard/事件盘验收其在运行链路上确实生效；
- **事件流是唯一权威事实源**（255 行「把事件写入与 OTel 导出分开」）：
  OTel 故障 run 与正常 run 的事件类型序列完全一致——观测通道的成败不改变
  事件事实，也不在事件流留下缺口；
- 注入失败按预期真实发生（「记录注入失败的运行报告」的机器证据）；
  故障恢复后新 run 回归正常（隔离按回合生效，不粘滞）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.workbench_support import RUNS_PATH, WorkbenchHarness


def _ask_body(client_request_id: str) -> dict[str, object]:
    return {
        "deployment_id": "finance",
        "mode": "ask",
        "question": "2013 年第二季度总交易额",
        "client_request_id": client_request_id,
    }


def _submit_and_drain(h: WorkbenchHarness, client_request_id: str) -> str:
    """提交到真实 API 并等待后台队列执行到终态；返回 run_id。"""
    response = h.request("POST", RUNS_PATH, json=_ask_body(client_request_id))
    assert response.status_code == 202, response.text
    run_id = str(response.json()["run_id"])
    h.drain()
    return run_id


def _view(h: WorkbenchHarness, run_id: str) -> dict[str, object]:
    response = h.request("GET", f"{RUNS_PATH}/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _event_types(h: WorkbenchHarness, run_id: str) -> list[str]:
    return [event.event_type for event in h.events(run_id)]


def test_otel_failure_does_not_stop_run_or_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """埋点故障（注入）：run 照常成功、结果可读、事件到终态——观测不反噬主链路。"""
    import observability.otel as otel

    failures: list[str] = []

    def _boom(*args: object, **kwargs: object) -> None:
        failures.append("otel")
        raise RuntimeError("OTel 导出故障（测试注入）")

    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        with monkeypatch.context() as m:
            m.setattr(otel, "_record_turn_impl", _boom)
            # 告警计数复位：本次注入的告警确实发生（context 退出后恢复原值）
            m.setattr(otel, "_WARNED_TELEMETRY_FAILURE", False)
            run_id = _submit_and_drain(h, "otel-fail-key")
        assert failures, "注入未生效：埋点实现未被调用"
        view = _view(h, run_id)
        assert view["status"] == "succeeded"
        assert view["result_availability"] == "available"
        assert view["result"] is not None
        assert view["result"]["kind"] == "answer"
        types = _event_types(h, run_id)
        assert types[0] == "RUN_ACCEPTED"
        assert types[-1] == "RUN_FINISHED"
        assert types.count("STATE_SNAPSHOT") == 1


def test_otel_failure_leaves_event_stream_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分离语义（255 行）：OTel 故障 run 与正常 run 的事件类型序列完全一致。"""

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("OTel 导出故障（测试注入）")

    with WorkbenchHarness(tmp_path) as h:
        h.seed_release()
        with monkeypatch.context() as m:
            m.setattr("observability.otel._record_turn_impl", _boom)
            failed_telemetry = _submit_and_drain(h, "otel-sep-fail")
        healthy = _submit_and_drain(h, "otel-sep-ok")
        assert _event_types(h, failed_telemetry) == _event_types(h, healthy)
        assert _view(h, failed_telemetry)["status"] == "succeeded"
        assert _view(h, healthy)["status"] == "succeeded"
