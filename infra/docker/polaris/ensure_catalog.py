"""Polaris catalog 'atlas' 幂等配置脚本（基础设施即代码）。

背景（实测根因，2026-09-02）：
    Doris 通过 REST catalog 协议读取 Polaris 下发的 storage config 中的
    endpoint 初始化 S3FileIO，该值会覆盖 Doris 侧 CREATE CATALOG 里写的
    s3.endpoint。若 Polaris catalog 的 storageConfigInfo.endpoint 是
    宿主机视角的 127.0.0.1:9000，Doris FE 容器内访问必然 Connection refused。
    因此对外公布的 endpoint 必须是引擎容器内可达的 host.docker.internal:9000；
    Polaris 自身写文件走 endpointInternal（minio:9000），不受影响。

用法：
    uv run python infra/docker/polaris/ensure_catalog.py

幂等：重复执行无副作用；仅当 endpoint 漂移时发出 PUT 修正。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CATALOG_NAME = "atlas"
# 对外公布的存储 endpoint：必须为容器网络内（Doris FE/BE）可达的地址
PUBLIC_ENDPOINT = os.environ.get("POLARIS_PUBLIC_S3_ENDPOINT", "http://host.docker.internal:9000")
# 宿主机 loader 视角的 endpoint（仅用于对照提示，不改动）
# Polaris 自身访问走 endpointInternal，见 docker-compose 内网说明

# Polaris 单端口同时暴露 REST Catalog 与管理 API（实测 8182 仅 Quarkus health）
BASE = os.environ.get("POLARIS_MGMT_BASE", "http://127.0.0.1:8181/api/management/v1")
TOKEN_URL = os.environ.get("POLARIS_TOKEN_URL", "http://127.0.0.1:8181/api/catalog/v1/oauth/tokens")
CLIENT_ID = os.environ.get("POLARIS_CLIENT_ID", "root")
CLIENT_SECRET = os.environ.get("POLARIS_CLIENT_SECRET", "secret")


def _get_token() -> str:
    """client_credentials 换取管理 API token。"""
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": "PRINCIPAL_ROLE:ALL"}
    ).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", _basic())
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)["access_token"]


def _basic() -> str:
    import base64

    raw = f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _request(
    token: str, path: str, method: str = "GET", payload: dict | None = None, etag: str | None = None
) -> dict | None:
    url = f"{BASE}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    if etag is not None:
        req.add_header("If-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"[error] {method} {path} -> {e.code}: {detail}", file=sys.stderr)
        raise


def main() -> int:
    token = _get_token()
    try:
        catalog = _request(token, f"catalogs/{CATALOG_NAME}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[error] catalog '{CATALOG_NAME}' 不存在，请先手工创建后再修正 endpoint。")
            return 2
        raise

    changes: list[str] = []

    # 1) 对外公布的存储 endpoint：必须为 Doris 容器网络内可达地址。
    #    实测：REST config 响应下发的 endpoint 会覆盖 Doris CREATE CATALOG 属性。
    storage = catalog.get("storageConfigInfo", {})
    current = storage.get("endpoint")
    if current != PUBLIC_ENDPOINT:
        changes.append(f"storage endpoint {current!r} -> {PUBLIC_ENDPOINT!r}")
        catalog["storageConfigInfo"]["endpoint"] = PUBLIC_ENDPOINT

    # 2) 静态凭证放 catalog 顶层属性：REST config 响应会下发 catalog properties，
    #    Doris FE 的 S3FileIO 初始化只认 config（表属性与 Doris 侧属性均不生效，实测）。
    props = catalog.setdefault("properties", {})
    wanted = {
        "s3.access-key-id": os.environ.get("MINIO_ACCESS_KEY", "admin"),
        "s3.secret-access-key": os.environ.get("MINIO_SECRET_KEY", "password"),
    }
    for key, value in wanted.items():
        if props.get(key) != value:
            changes.append(f"properties.{key} -> {value!r}")
            props[key] = value

    if not changes:
        print("[noop] endpoint 与凭证属性均已就绪，无需变更。")
        return 0

    print("[update] " + "; ".join(changes))
    version = int(catalog.get("entityVersion", 1))
    # 更新接口要求 UpdateCatalogRequest 包装（实测缺 currentEntityVersion 报 500），
    # 且必须带 If-Match 乐观锁头（实测缺失时 PUT 返回 200 但改动不持久化）
    payload = {"currentEntityVersion": version, "catalog": catalog}
    _request(token, f"catalogs/{CATALOG_NAME}", method="PUT", payload=payload, etag=str(version))
    # 自检：重新 GET 确认已持久化，防止静默失败
    after = _request(token, f"catalogs/{CATALOG_NAME}") or {}
    if after.get("storageConfigInfo", {}).get("endpoint") != PUBLIC_ENDPOINT:
        print("[error] 提交后自检失败：endpoint 未生效，请检查 Polaris 日志。", file=sys.stderr)
        return 3
    print("[done] catalog 已更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
