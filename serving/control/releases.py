"""T08b 发布导入与 CAS 激活（ADR-0031 D04/D13 管理面）。

设计口径
--------
- **发布源是显式 commit**：`source_git_sha` 必须是完整 40 hex 且对象库可达；
  只读 `git ls-tree`/`cat-file` 的对象库视图——工作树/索引/引用不是发布源，
  服务也不自动 commit/push/merge（D04）。
- **导入只收白名单配置**：路径必须命中制品白名单（agent.runtime.bundle，与草稿
  形状门共用同一事实源）；符号链接/子模块/超大文件/非白名单路径一律拒绝或不
  收集，制品从不执行（不从上传包运行代码）。
- **内容一致性**：制品目标文件必须与**已审核**草稿逐字一致（解析结构相等）；
  不一致即 409——审核后篡改不得变成发布（审核证据绑定摘要，D04）。
- **整体门禁**：结构（ossie_validate）+ 治理（governance_validate）+ supersedes
  链 + 策略一致性 + 值域绑定（ADR-0016）+ 全指标样例编译与 sqlglot 往返。
  模型/策略校验用「同目录 live 其他模型（剔除被制品替代的文件名）+ 制品模型」
  的联合引用面：只看制品内会把 live 已有引用误报为孤儿。
- **CAS 发布**：`Deployment` 是 `(deployment_id, active_release_id, source_id)`
  的原子指针；`expected_active_release_id` 不匹配一律 409，不静默覆盖（D04）。
- **证据门禁**：发布/回退都要求源最新修订有最近一次 `ok` 探测证据（只读已确认）；
  回退指针到历史制品与发布同受当前权限、源能力与安全门禁（D04"也需"）。

边界（诚实声明）
----------------
- 值域绑定校验的模型索引取**当前语义层**（`semantic.lint.check_value_profiles`
  的既有口径，无制品参数）：同一批新增「维度 + 值域快照」的首次发布会在值域门
  被拒（fail-closed），需先合并模型再发布值域——不静默放宽。
- 评测回归属 T13/T14；`eval_evidence_ids` 为空元组、不声称回归通过。索引摘要
  （`index_digest`）按制品内 `indexes/*` 实际内容计算，不读工作树索引。
- 草稿推进止于 `release_ready`（published/retired 的推进不在本批）；导入链与制品
  登记分属两次写事务（本地单写者）：并发编辑使推进 409 时，制品已登记但草稿未
  推进——不激活即无副作用，重试会命中"同内容已登记"409，须人工处置（如实暴露，
  不假装原子）。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import sqlglot
import yaml

from agent.compiler import Compiler, Plan, SemanticModel
from agent.runtime.bundle import (
    MAX_FILE_BYTES,
    REPO_CONFIG_PATTERNS,
    BundleError,
    build_manifest,
    load_bundle,
    persist_bundle,
    runtime_code_sha,
)
from data.identity import REPO_ROOT
from semantic import governance_validate, ossie_validate
from semantic.lint import check_value_profiles
from serving.control.auth import ControlForbidden, Principal, authorize
from serving.control.contracts import (
    ActivationRequest,
    ActivationView,
    Draft,
    Owner,
    ReleaseImportRequest,
    ReleaseImportView,
    ReleaseRecord,
)
from serving.control.store import ControlStore, ReleaseConflict, RevisionConflict

RELEASE_IMPORT_CAPABILITY = "release.import"
RELEASE_PUBLISH_CAPABILITY = "release.publish"
RELEASE_ROLLBACK_CAPABILITY = "release.rollback"
# 读取门：发布动作或部署管理（发布/回退先读当前指针做 CAS；部署管理只读不写）。
_RELEASE_READ_CAPABILITIES = frozenset(
    {"release.import", "release.publish", "release.rollback", "deployment.manage"}
)
# 当前只有语义模型具备确定性门禁链；其他 kind 不得静默导入。
_IMPORTABLE_KIND = "semantic"
_OSSIE_DIR = REPO_ROOT / "semantic" / "ossie"
_SAFE_SHA = re.compile(r"[0-9a-f]{40}")
ActivationAction = Literal["publish", "rollback"]


class ReleaseInvalid(ValueError):
    """提交/制品/门禁不合法（HTTP 层投影为 422）。"""


class SourceEvidenceMissing(RuntimeError):
    """发布/回退缺少当前源能力证据（HTTP 层投影为 409 evidence_required）。"""


class ReleaseTargetMissing(LookupError):
    """目标对象（草稿/源/部署/发布）不存在（HTTP 层投影为 404）。"""

    def __init__(self, kind: str, value: str) -> None:
        super().__init__(f"{kind}不存在：{value!r}")


class ReleaseService:
    """发布服务：Git 制品导入（门禁 → 登记）与部署指针 CAS 激活。"""

    def __init__(
        self,
        store: ControlStore,
        *,
        bundle_root: Path,
        repo_root: Path,
    ) -> None:
        self._store = store
        self._bundle_root = bundle_root
        self._repo_root = repo_root

    # ------------------------------------------------------------------
    # 导入：显式 commit → 门禁 → 内容寻址落盘 → 登记 → 草稿推进
    # ------------------------------------------------------------------

    def import_bundle(
        self, principal: Principal, request: ReleaseImportRequest
    ) -> ReleaseImportView:
        """把已审核草稿对应的 Git 制品导入为不可变发布（门禁通过才登记）。

        门禁顺序：能力/作用域（403）→ 草稿存在且 reviewed（404/409）→ 提交可达
        （422）→ 白名单收集（422）→ 目标存在（422）→ 内容 == 已审核草稿（409）→
        源已配置（404）→ 制品整体门禁（422）→ 内容寻址落盘 → 装载复检 → 登记 →
        草稿同事务推进 source_imported → release_ready。制品不激活（激活是显式 CAS）。

        Raises
        ------
        ControlForbidden
            缺 `release.import` 或草稿域 ∉ principal.scopes（403）。
        ReleaseTargetMissing
            草稿不存在或目标源未登记修订（404）。
        ReleaseInvalid
            提交不可达、路径/容量不合法或制品门禁未通过（422）。
        RevisionConflict
            草稿非 reviewed，或制品内容与已审核草稿不一致（409）。
        ReleaseConflict
            同内容制品已登记（不覆写；内容寻址制品不可变，409）。
        """
        draft = self._read_draft(request.draft_id)
        authorize(principal, RELEASE_IMPORT_CAPABILITY, draft.scope)
        if draft.kind != _IMPORTABLE_KIND or draft.status != "reviewed":
            raise RevisionConflict("导入只接受 reviewed 的语义草稿（先校验并审核）")
        commit = self._resolve_commit(request.source_git_sha)
        files = self._collect_whitelisted(commit)
        target = self._target_of(draft)
        if target not in files:
            raise ReleaseInvalid(f"制品缺少目标文件：{target!r}")
        try:
            committed = yaml.safe_load(files[target])
        except yaml.YAMLError as exc:
            raise ReleaseInvalid(f"目标文件不是合法 YAML：{exc}") from exc
        if committed != draft.content["document"]:
            raise RevisionConflict("制品内容与已审核草稿不一致（审核后篡改须重新审核）")
        revision = self._store.latest_source_revision(request.source_id)
        if revision is None:
            raise ReleaseTargetMissing("数据源", request.source_id)
        findings = self._gate_bundle(files)
        if findings:
            raise ReleaseInvalid("制品门禁未通过：" + "；".join(findings))
        manifest = build_manifest(
            files,
            source_git_sha=commit,
            runtime_code_sha=runtime_code_sha(),
            source_revision=revision.revision,
            eval_evidence_ids=(),
        )
        if self._store.get_release(manifest.release_id) is not None:
            raise ReleaseConflict("同内容制品已登记（内容寻址制品不覆写）")
        try:
            persist_bundle(self._bundle_root, files, manifest)
            bundle = load_bundle(self._bundle_root, manifest.release_id)
        except BundleError as exc:  # 落盘/复检失败：不登记（零指针副作用）
            raise ReleaseInvalid(f"制品落盘或装载复检失败：{exc}") from exc
        owner = Owner(issuer=principal.issuer, subject=principal.subject)
        self._store.register_release(
            bundle, owner=owner, scope=draft.scope, source_id=request.source_id
        )
        ready = self._store.advance_draft_chain(
            draft.draft_id,
            ("source_imported", "release_ready"),
            expected=draft.revision,
            actor=owner,
            evidence_id=manifest.release_id,
        )
        record = self._store.get_release(manifest.release_id)
        if record is None:  # 登记与回读之间的删除窗口（本地单写者，理论不可达）
            raise ReleaseTargetMissing("发布", manifest.release_id)
        return ReleaseImportView(
            release_id=manifest.release_id,
            content_digest=manifest.content_digest,
            draft_id=draft.draft_id,
            draft_revision=ready.revision,
            status="release_ready",
            target=target,
            created_at=record.created_at,
        )

    def _read_draft(self, draft_id: str) -> Draft:
        """读取草稿；不存在抛 ReleaseTargetMissing（路由层投影 404）。"""
        try:
            return self._store.get_draft(draft_id)
        except KeyError as exc:
            raise ReleaseTargetMissing("草稿", draft_id) from exc

    def _resolve_commit(self, source_git_sha: str) -> str:
        """校验显式提交：完整 40 hex 且对象库可达的 commit（不读工作树/索引/引用）。"""
        if not _SAFE_SHA.fullmatch(source_git_sha):
            raise ReleaseInvalid("源提交必须是完整 40 位十六进制 sha")
        probe = self._git("cat-file", "-e", f"{source_git_sha}^{{commit}}")
        if probe.returncode != 0:
            raise ReleaseInvalid(f"源提交不可达或不是 commit：{source_git_sha!r}")
        return source_git_sha

    def _git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        """在发布源仓库执行只读 git（对象库视图；结果由调用方判定，不静默继续）。"""
        return subprocess.run(
            ["git", "-C", str(self._repo_root), *args],
            capture_output=True,
            check=False,
        )

    def _collect_whitelisted(self, commit: str) -> dict[str, bytes]:
        """收集该 commit 中命中制品白名单的普通文件字节（其余路径零收集）。

        Raises
        ------
        ReleaseInvalid
            树不可读、白名单路径为符号链接/子模块、文件超过单文件上限（按
            `ls-tree` 尺寸在读取前拦截，防压缩炸弹）或制品为空。
        """
        result = self._git("ls-tree", "-r", "-z", "-l", commit)
        if result.returncode != 0:
            raise ReleaseInvalid("无法读取源提交树")
        files: dict[str, bytes] = {}
        for entry in result.stdout.split(b"\0"):
            if not entry:
                continue
            meta, separator, raw_path = entry.partition(b"\t")
            if not separator:
                raise ReleaseInvalid("源提交树条目缺少路径")
            path = raw_path.decode("utf-8", "surrogateescape")
            if not self._whitelisted(path):
                continue
            fields = meta.decode("ascii").split()
            if len(fields) != 4:
                raise ReleaseInvalid("源提交树条目格式异常")
            mode, kind, object_id, size = fields
            if mode == "120000":
                raise ReleaseInvalid(f"制品路径不得为符号链接：{path!r}")
            if mode == "160000":
                raise ReleaseInvalid(f"制品不得包含子模块：{path!r}")
            if kind != "blob":
                raise ReleaseInvalid(f"制品路径必须是普通文件：{path!r}")
            if size != "-" and int(size) > MAX_FILE_BYTES:
                raise ReleaseInvalid(f"制品文件超过容量上限：{path!r}")
            blob = self._git("cat-file", "blob", object_id)
            if blob.returncode != 0:
                raise ReleaseInvalid(f"无法读取制品内容：{path!r}")
            if len(blob.stdout) > MAX_FILE_BYTES:
                raise ReleaseInvalid(f"制品文件超过容量上限：{path!r}")
            files[path] = blob.stdout
        if not files:
            raise ReleaseInvalid("制品为空：源提交未命中任何白名单配置路径")
        return dict(sorted(files.items()))

    @staticmethod
    def _whitelisted(path: str) -> bool:
        """路径是否在制品白名单（与草稿形状门、装载校验同一事实源）。"""
        return any(re.fullmatch(pattern, path) for pattern in REPO_CONFIG_PATTERNS)

    @staticmethod
    def _target_of(draft: Draft) -> str:
        """草稿目标路径（形状门已在创建/编辑拦过，此处防御不变量）。"""
        target = draft.content.get("target")
        if not isinstance(target, str):
            raise ReleaseInvalid("草稿 content.target 不是字符串")
        return target

    @staticmethod
    def _gate_bundle(files: dict[str, bytes]) -> list[str]:
        """对制品整体跑确定性门禁，返回发现清单（空 = 通过）；零执行、零写盘。

        模型/策略一致性用「live 同目录其他模型（剔除被制品替代的文件名）+ 制品
        模型」的联合引用面——只在制品内比较会把 live 已有引用误报为孤儿。
        """
        candidates_names = [name for name in files if re.fullmatch(REPO_CONFIG_PATTERNS[0], name)]
        findings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="atlas-release-gate-") as scratch:
            root = Path(scratch)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            replaced = {Path(name).name for name in candidates_names}
            peers = [
                peer
                for peer in sorted(_OSSIE_DIR.glob("*.ossie.yaml"))
                if peer.name not in replaced
            ]
            candidates = [root / name for name in candidates_names]
            seen_names: dict[str, str] = {}
            for peer in peers:
                ossie_validate.validate_file(peer, seen_names)
            for candidate in candidates:
                findings.extend(ossie_validate.validate_file(candidate, seen_names))
            schema = json.loads(governance_validate.SCHEMA_PATH.read_text(encoding="utf-8"))
            for candidate in candidates:
                governance_validate.validate_file(candidate, schema, findings)
            governance_validate.check_supersedes_chains(
                governance_validate.collect_metric_governance([*peers, *candidates]), findings
            )
            policy: list[str] = []
            # 延迟导入：semantic 不设 serving 顶层依赖（与 DraftService 同纪律）
            from serving.auth import ROLE_DIRECTORY

            # 策略以制品内声明为准（制品缺该文件时退回 live 策略，fail-closed 由
            # check_policy_consistency 的孤儿报错体现）。
            bundle_policy = root / "semantic" / "policies" / "row_policy.yml"
            policy_path = (
                bundle_policy if bundle_policy.exists() else governance_validate.POLICY_PATH
            )
            governance_validate.check_policy_consistency(
                governance_validate.collect_referenced_policies([*peers, *candidates]),
                governance_validate.load_policies_by_name(policy_path),
                ROLE_DIRECTORY,
                policy,
            )
            findings.extend(policy)
            findings.extend(check_value_profiles(root / "semantic" / "values"))
            findings.extend(ReleaseService._compile_samples(candidates))
        return findings

    @staticmethod
    def _compile_samples(candidates: list[Path]) -> list[str]:
        """样例编译与方言往返：每个候选模型的每个指标必须可编译并通过 sqlglot 解析。

        编译失败不中止其余指标（汇总全部发现，便于一次修完）；模型装配失败由结构
        门先报，此处兜底不吞错（如实进 findings）。
        """
        findings: list[str] = []
        for candidate in candidates:
            try:
                model = SemanticModel(candidate)
            except Exception as exc:  # noqa: BLE001 - 门禁汇总：一条坏模型不带崩全部门禁
                findings.append(f"{candidate.name}：模型无法装配（{exc}）")
                continue
            compiler = Compiler(model)
            for metric in sorted(model.metrics):
                try:
                    sql, _ = compiler.compile(Plan(metric=metric))
                    sqlglot.parse_one(sql)
                except Exception as exc:  # noqa: BLE001 - 同上：逐指标汇总发现
                    findings.append(f"{candidate.name}.{metric}：样例编译失败（{exc}）")
        return findings

    # ------------------------------------------------------------------
    # 激活：发布/回退共用 CAS + 当前安全检查
    # ------------------------------------------------------------------

    def publish(
        self, principal: Principal, deployment_id: str, request: ActivationRequest
    ) -> ActivationView:
        """发布：把已登记制品设为部署活动指针（CAS）。

        Raises
        ------
        ControlForbidden
            缺 `release.publish` 或部署域 ∉ principal.scopes（403）。
        ReleaseTargetMissing
            部署或发布不存在（404）。
        ReleaseConflict
            发布与部署的域/源不匹配或 expected 指针已变化（409）。
        SourceEvidenceMissing
            源最新修订缺少 `ok` 探测证据（409 evidence_required）。
        """
        return self._activate(principal, deployment_id, request, action="publish")

    def rollback(
        self, principal: Principal, deployment_id: str, request: ActivationRequest
    ) -> ActivationView:
        """回退：指针切向历史制品；能力/域/登记/证据门禁与发布同等（D04"也需"）。"""
        return self._activate(principal, deployment_id, request, action="rollback")

    def _activate(
        self,
        principal: Principal,
        deployment_id: str,
        request: ActivationRequest,
        *,
        action: ActivationAction,
    ) -> ActivationView:
        """发布/回退共用的 CAS 激活：能力 → 部署 → 登记 → 域/源匹配 → 证据 → CAS。

        previous 取激活前读到的指针（响应如实给出切换来源）；activate 后重读部署，
        revision/updated_at 取提交后事实（不采信调用方或本地推算）。
        """
        deployment = self._store.get_deployment(deployment_id)
        if deployment is None:
            raise ReleaseTargetMissing("部署", deployment_id)
        capability = (
            RELEASE_PUBLISH_CAPABILITY if action == "publish" else RELEASE_ROLLBACK_CAPABILITY
        )
        authorize(principal, capability, deployment.scope)
        release = self._store.get_release(request.release_id)
        if release is None:
            raise ReleaseTargetMissing("发布", request.release_id)
        if (release.scope, release.source_id) != (deployment.scope, deployment.source_id):
            raise ReleaseConflict("发布与部署的域/源不匹配（不跨域发布）")
        self._require_current_evidence(deployment.source_id)
        self._store.activate(
            deployment_id, request.release_id, expected=request.expected_active_release_id
        )
        updated = self._store.get_deployment(deployment_id)
        if updated is None:  # 激活与回读之间的删除窗口（本地单写者，理论不可达）
            raise ReleaseTargetMissing("部署", deployment_id)
        return ActivationView(
            deployment_id=deployment_id,
            action=action,
            previous_release_id=deployment.active_release_id,
            active_release_id=request.release_id,
            revision=updated.revision,
            updated_at=updated.updated_at,
        )

    def _require_current_evidence(self, source_id: str) -> None:
        """证据门禁：源最新修订必须有最近一次 `ok` 探测（只读已确认）。

        证据是时间戳事实：重探后取最新观察（store 按 observed_at + 插入序取最近）
        ——上一次阻塞不因存在旧 `ok` 而放行，重探确认只读后自然恢复。
        """
        revision = self._store.latest_source_revision(source_id)
        if revision is None:
            raise SourceEvidenceMissing(f"数据源未登记修订：{source_id!r}")
        probe = self._store.latest_source_probe(source_id, revision.version)
        if probe is None or probe.status != "ok":
            raise SourceEvidenceMissing(
                f"数据源缺少当前只读能力证据（需先受限探测确认只读）：{source_id!r}"
            )

    # ------------------------------------------------------------------
    # 读取面：域裁剪 + 固定键 + 脱敏
    # ------------------------------------------------------------------

    def list_releases(self, principal: Principal) -> list[ReleaseRecord]:
        """已授权领域的发布列表（服务端裁剪；零授权 = 空表）。"""
        self._require_read(principal)
        return [row for row in self._store.list_releases() if row.scope in principal.scopes]

    def view(self, principal: Principal, release_id: str) -> ReleaseRecord:
        """发布详情（脱敏 Manifest）；未知抛 ReleaseTargetMissing，未授权域 403。"""
        self._require_read(principal)
        record = self._store.get_release(release_id)
        if record is None:
            raise ReleaseTargetMissing("发布", release_id)
        if record.scope not in principal.scopes:
            raise ControlForbidden(f"作用域未授权：{record.scope!r}")
        return record

    @staticmethod
    def _require_read(principal: Principal) -> None:
        if not _RELEASE_READ_CAPABILITIES & principal.capabilities:
            raise ControlForbidden(
                "控制能力不足：读取发布需要 release.import/publish/rollback 或 deployment.manage"
            )
