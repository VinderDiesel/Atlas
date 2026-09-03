# observability/ — Atlas 可观测栈（Day 50-51）

## 组成

| 路径 | 角色 |
|---|---|
| `otel.py` | 回合级 OTel 埋点（Day 50）：`atlas.turn` span + 指标；`configure_otel()` 幂等配置 |
| `dashboards/atlas-data-agent.json` | Grafana 面板（QPS / Guard 拒绝率 / 时延 p95 / token 速率 / kind 拆分） |
| `provisioning/` | Grafana provisioning：数据源（uid `prometheus`）、面板 provider、告警规则（`atlas-alerts.yaml`） |
| `otel-collector.yaml` | Collector：OTLP HTTP 4318 收 → Prometheus exporter 8889 |
| `prometheus.yml` | Prometheus 抓取 collector:8889 |

## 数据链路

```
agent/graph.py ask ──record_turn──▶ SDK（OTLP HTTP :4318）
                                        │
                                        ▼
                                 otel-collector ──▶ prometheus(:8889) ──▶ Grafana(:3001)
```

## 启用（三选一，全部幂等）

1. 环境变量（进程启动前）：`export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`
2. `configure_otel()`（读同一个环境变量）
3. 测试/程序化注入 provider（见 tests/test_otel.py）

Collector + Prometheus 是 compose 可选 profile（默认 `make up` 不占内存）：

```bash
docker compose --profile obs up -d otel-collector prometheus
```

## 诚实注记（AGENTS.md 第 9 节口径）

- **指标单位**：`gen_ai.token_cost` 单位为 token。未接定价表，不换算 USD——避免伪精确。
  确定性链路（无 LLM）0 token，该指标与面板**无数据点属正常**，不虚报模型调用。
- **时延口径**：`atlas.turn.latency` 为回合执行耗时（`TurnResult.latency_ms`，Doris 执行段）。
  无流式 LLM，不做 TTFT 伪细分；"TTFT" 类展示均为回合总时延近似语义。
- **告警阈值是配置占位**：MVP 无真实流量，阈值（拒绝率 >20%、p95 >5s、故障率 >10%）
  不是实测分布统计边界，需接入真实负载后校准。
- **默认零 I/O**：未配置 endpoint 时埋点 no-op；埋点故障隔离（见 otel.py docstring），
  观测绝不反噬主链路。

## 一致性防漂移

`tests/test_dashboards.py` 断言：面板/告警引用的指标名与 `otel.py` 注册名一致、
datasource uid 与 provisioning 一致、JSON/YAML 可解析。改动指标名必须同步面板。

## 验收口径

门槛条目「OTel trace 能追到 question_id → metric_id → SQL → cost」：
- 机器验证：tests/test_otel.py（span 属性链 + 指标数据点，in-memory exporter）；
- 可视化验证：面板数据来自同一指标注册，链路见上（需起 obs profile）。
