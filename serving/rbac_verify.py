"""Polaris 层 RBAC 验证（回归工具）：不同 principal 对同一 catalog 的可见性差异。

定位
----
本工具是 catalog 对象级授权的可重复回归验证：Polaris 角色/授权变更后
运行，确认"同一 REST 端点、不同 principal 的元数据与读表可见性符合最小权限
预期"。与 serving/rls_verify.py（SQL 谓词层行级）组成纵深防御的两层回归。

口径（诚实声明）
----------------
- 本工具验证的是 **catalog 对象级（表粒度）可见性**：同一 Iceberg catalog
  （atlas，25 张表：tpcdi 原始层 17 + dwd 8）下，两个 principal 走同一个
  REST catalog 端点，授权不同 → 元数据/读表结果不同。
- 行级（行过滤）不在 Polaris 层做——行级下推发生在 SQL 谓词层
  （serving/rls_verify.py）。两层合起来 = 纵深防御：
  应用 JWT → Guard 行级谓词 → Polaris principal 对象级。
- 授权模型（实测收敛）：受限 principal 经
  principal-role analyst_principal_role → catalog-role analyst_catalog_role，
  仅授 dwd.dim_broker / dwd.dim_customer 两表的
  TABLE_LIST + TABLE_READ_DATA + TABLE_FULL_METADATA + TABLE_READ_PROPERTIES。
- 实测结果（2026-09-02）：受限 principal list_namespaces/list_tables 均
  Forbidden（防枚举）；load 已授权表 OK；load fact_trades/tpcdi.trade
  Forbidden。root principal 全部可见。

用法：
    uv run python serving/rbac_verify.py            # 验证并输出报告
    uv run python serving/rbac_verify.py --ensure   # 幂等补齐对象（principal
                                                     #   已存在时跳过，secret 首次创建时输出）

环境变量（.env）：
    POLARIS_CLIENT_ID / POLARIS_CLIENT_SECRET   # root（管理）
    POLARIS_RBAC_CLIENT_ID / POLARIS_RBAC_CLIENT_SECRET  # 受限 principal
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pyiceberg.catalog import load_catalog  # noqa: E402

MGMT = os.environ.get("POLARIS_MGMT_BASE", "http://127.0.0.1:8181/api/management/v1")
CATALOG_URI = os.environ.get("POLARIS_URI", "http://127.0.0.1:8181/api/catalog")
RBAC_PRINCIPAL = "atlas_analyst"
RBAC_PRINCIPAL_ROLE = "analyst_principal_role"
RBAC_CATALOG_ROLE = "analyst_catalog_role"
GRANTED_TABLES = ["dim_broker", "dim_customer"]  # dwd 下仅授这两张
SCOPE = "PRINCIPAL_ROLE:ALL"


def git_short_sha() -> str:
    import subprocess

    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _root_credentials() -> tuple[str, str]:
    return (
        os.environ.get("POLARIS_CLIENT_ID", "root"),
        os.environ.get("POLARIS_CLIENT_SECRET", "secret"),
    )


def _analyst_credentials() -> tuple[str, str]:
    cid = os.environ.get("POLARIS_RBAC_CLIENT_ID", "")
    secret = os.environ.get("POLARIS_RBAC_CLIENT_SECRET", "")
    if not cid or not secret:
        raise SystemExit(
            "[error] 请在 .env 设置 POLARIS_RBAC_CLIENT_ID / POLARIS_RBAC_CLIENT_SECRET"
            "（principal 首次创建时由 --ensure 输出）"
        )
    return cid, secret


def _request(path: str, token: str, method: str = "GET", payload: dict | None = None):
    url = f"{MGMT}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"{method} {path} -> {exc.code}: {exc.read().decode(errors='replace')[:200]}"
        ) from exc


def _mgmt_token() -> str:
    cid, secret = _root_credentials()
    auth = "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
    req = urllib.request.Request(f"{CATALOG_URI}/v1/oauth/tokens", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["access_token"]


def ensure() -> None:
    """幂等补齐 RBAC 对象链（重复运行无副作用）。

    principal 已存在时无法再次读取 clientSecret（一次性），故使用方需把
    首次创建输出的 secret 写入 .env 的 POLARIS_RBAC_CLIENT_SECRET。
    """
    token = _mgmt_token()
    try:
        _request(f"principals/{RBAC_PRINCIPAL}", token)
        print(f"[noop] principal {RBAC_PRINCIPAL} 已存在（secret 需来自首次创建输出）")
    except RuntimeError:
        created = _request("principals", token, "POST", {"name": RBAC_PRINCIPAL})
        cred = created.get("credentials", {})
        print(
            f"[create] principal {RBAC_PRINCIPAL} 创建成功，请把以下凭据写入 .env：\n"
            f"  POLARIS_RBAC_CLIENT_ID={cred.get('clientId')}\n"
            f"  POLARIS_RBAC_CLIENT_SECRET={cred.get('clientSecret')}"
        )
    # principal-role（不存在才建）
    try:
        _request(f"principal-roles/{RBAC_PRINCIPAL_ROLE}", token)
    except RuntimeError:
        _request("principal-roles", token, "POST", {"name": RBAC_PRINCIPAL_ROLE})
        print(f"[create] principal-role {RBAC_PRINCIPAL_ROLE}")
    # 绑定 principal → principal-role（201 幂等）
    _request(
        f"principals/{RBAC_PRINCIPAL}/principal-roles",
        token,
        "PUT",
        {"principalRole": {"name": RBAC_PRINCIPAL_ROLE}},
    )
    # catalog-role（atlas 域内）
    try:
        _request(f"catalogs/atlas/catalog-roles/{RBAC_CATALOG_ROLE}", token)
    except RuntimeError:
        _request("catalogs/atlas/catalog-roles", token, "POST", {"name": RBAC_CATALOG_ROLE})
        print(f"[create] catalog-role {RBAC_CATALOG_ROLE}（catalog atlas）")
    # catalog-role 绑到 principal-role
    _request(
        f"principal-roles/{RBAC_PRINCIPAL_ROLE}/catalog-roles/atlas",
        token,
        "PUT",
        {"catalogRole": {"name": RBAC_CATALOG_ROLE}},
    )
    # grants（已存在会 409 冲突，忽略）
    existing = _request(f"catalogs/atlas/catalog-roles/{RBAC_CATALOG_ROLE}/grants", token).get(
        "grants", []
    )
    have = {(g.get("tableName"), g.get("privilege")) for g in existing if g.get("type") == "table"}
    for table in GRANTED_TABLES:
        for privilege in (
            "TABLE_LIST",
            "TABLE_READ_DATA",
            "TABLE_FULL_METADATA",
            "TABLE_READ_PROPERTIES",
        ):
            if (table, privilege) in have:
                continue
            _request(
                f"catalogs/atlas/catalog-roles/{RBAC_CATALOG_ROLE}/grants",
                token,
                "PUT",
                {
                    "grant": {
                        "type": "table",
                        "namespace": ["dwd"],
                        "tableName": table,
                        "privilege": privilege,
                    }
                },
            )
            print(f"[grant] dwd.{table} {privilege}")
    print("[done] RBAC 对象就绪")


def _client(label: str, cid: str, secret: str):
    return load_catalog(
        label,
        type="rest",
        uri=CATALOG_URI,
        credential=f"{cid}:{secret}",
        scope=SCOPE,
        warehouse="atlas",
        # 覆盖 pyiceberg 默认 vended-credentials 头（Polaris 1.7 忽略值 → direct 模式），
        # 与 data/loader.py 同一对接要点
        **{
            "header.X-Iceberg-Access-Delegation": "vendor-credentials",
            "s3.endpoint": os.environ.get("MINIO_ENDPOINT", "http://127.0.0.1:9000"),
        },
    )


def _detail_text(detail: object) -> str:
    """探测详情规约：list（如 namespace 枚举）转 '[a, b]'，其余 str()。"""
    if isinstance(detail, list):
        return "[" + ", ".join(str(item) for item in detail) + "]"
    return str(detail)


def _probe(client, label: str) -> dict:
    """对单个 principal 探测 catalog 各操作，返回 {操作: (OK|权限错误, 摘要)}。"""
    out: dict = {}
    try:
        ns = [n for (n,) in client.list_namespaces()]
        out["list_namespaces"] = {"ok": True, "detail": ns}
    except Exception as exc:  # noqa: BLE001 - 探测输出错误类别即可
        out["list_namespaces"] = {"ok": False, "detail": type(exc).__name__}
    for table in ("dim_broker", "dim_customer", "fact_trades"):
        try:
            t = client.load_table(("dwd", table))
            out[f"load dwd.{table}"] = {
                "ok": True,
                "detail": f"snapshots={len(t.snapshots() or [])}",
            }
        except Exception as exc:  # noqa: BLE001
            out[f"load dwd.{table}"] = {"ok": False, "detail": type(exc).__name__}
    return {k: v for k, v in out.items() if k == "list_namespaces" or "load " in k}


def verify() -> int:
    root_cid, root_secret = _root_credentials()
    analyst_cid, analyst_secret = _analyst_credentials()
    root = _client("root", root_cid, root_secret)
    analyst = _client(RBAC_PRINCIPAL, analyst_cid, analyst_secret)
    report = {
        "sha": git_short_sha(),
        "catalog_uri": CATALOG_URI,
        "catalog": "atlas（tpcdi 17 + dwd 8，共 25 张 Iceberg 表）",
        "analyst_principal": RBAC_PRINCIPAL,
        "analyst_grants": {
            "namespace": "dwd",
            "tables": GRANTED_TABLES,
            "privileges": [
                "TABLE_LIST",
                "TABLE_READ_DATA",
                "TABLE_FULL_METADATA",
                "TABLE_READ_PROPERTIES",
            ],
        },
        "root": _probe(root, "root"),
        "analyst": _probe(analyst, "analyst"),
    }
    for name in ("root", "analyst"):
        print(f"== {name} principal（{RBAC_PRINCIPAL if name == 'analyst' else 'root'}）")
        for op, res in report[name].items():
            detail = _detail_text(res["detail"])
            status = "OK" if res["ok"] else detail
            line = f"  {op}: {status}" + (f"  {detail}" if res["ok"] else "")
            print(line)

    # 断言差异（实测计数）：root 全可见，受限 principal 只见授权表
    analyst_loads = [v for k, v in report["analyst"].items() if k.startswith("load ")]
    if not report["root"]["list_namespaces"]["ok"]:
        print("[error] root 列 namespace 失败——环境异常，非权限差异", file=sys.stderr)
        return 1
    granted_ok = all(v["ok"] for v in analyst_loads[:2])
    denied_ok = not analyst_loads[2]["ok"]
    if not (granted_ok and denied_ok):
        print(
            "[error] 受限 principal 可见性不符合预期：授权表应可读、未授权表应拒绝",
            file=sys.stderr,
        )
        return 1

    report_dir = REPO_ROOT / "eval" / "reports"
    report_dir.mkdir(exist_ok=True)
    path = report_dir / f"polaris-rbac-{report['sha']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"报告：{path.relative_to(REPO_ROOT)}")
    _write_html(report, REPO_ROOT / "docs" / "screenshots" / "polaris-rbac.html")
    return 0


def _write_html(report: dict, path: Path) -> None:
    """把报告渲染成自包含 HTML（真实数据，供浏览器截图留证）。"""
    rows: list[str] = []
    for name in ("root", "analyst"):
        who = "root（服务账号）" if name == "root" else f"{report['analyst_principal']}（受限只读）"
        for op, res in report[name].items():
            rows.append(
                f"<tr><td>{who}</td><td><code>{op}</code></td>"
                f"<td>{'<b>OK</b>' if res['ok'] else _detail_text(res['detail'])}</td>"
                f"<td>{_detail_text(res['detail'])}</td></tr>"
            )
    grants = ", ".join(f"dwd.{t}" for t in report["analyst_grants"]["tables"])
    chain = "Iceberg REST catalog（pyiceberg）→ Polaris（principal-role → catalog-role → grants）"
    html = (
        f"<h1>Polaris 层 RBAC：同一 catalog 不同 principal <code>{report['sha']}</code></h1>"
        f"<p>catalog：{report['catalog']}；受限 principal 仅授权 {grants} 的读权限</p>"
        f"<p>链路：{chain}</p>"
        f"<table border='1' cellspacing='0' cellpadding='6'>"
        f"<tr><th>principal</th><th>操作</th><th>结果</th><th>详情</th></tr>{''.join(rows)}</table>"
    )
    style = "body{font-family:Menlo,monospace;margin:24px;background:#fff}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><style>{style}</style></head>"
        f"<body>{html}</body></html>",
        encoding="utf-8",
    )
    print(f"HTML 快照：{path.relative_to(REPO_ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ensure", action="store_true", help="幂等补齐 RBAC 对象（建 principal/roles/grants）"
    )
    args = ap.parse_args()
    if args.ensure:
        ensure()
        return 0
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
