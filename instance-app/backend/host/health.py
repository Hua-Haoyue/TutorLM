"""Readiness assembly. Policy lives in ``domain``; this module only assembles.

``/health`` proves the process is alive. ``/health/ready`` is the handshake the
control plane uses before admitting a visitor, so a persistent instance must
declare its project binding (``contracts/instance-runtime-v2.md`` / ``v3``).

No readiness rule is re-implemented here: the payload shape comes from
``backend.domain.readiness`` and the ``content_only`` proof comes from
``backend.domain.storage_layout.evaluate_storage_layout`` (fed with facts from
``host/storage_probe.py``). The Host only decides *which* report to build.
"""
from __future__ import annotations

from pathlib import Path

from backend.domain.readiness import ReadinessReport, ephemeral_report, persistent_report
from backend.domain.volume import is_legacy_layout
from backend.domain.storage_layout import (
    StorageProofFacts,
    evaluate_storage_layout,
    marker_is_missing_but_volume_initialized,
)

from .config import HostConfig
from .storage_probe import collect_facts, ensure_data_root


class HealthService:
    """Builds liveness and readiness payloads for the current Host process."""

    def __init__(self, config: HostConfig) -> None:
        self.config = config

    @property
    def provider_summary(self) -> dict[str, str]:
        return {"name": self.config.provider.name, "model": self.config.provider.model}

    def live(self) -> dict[str, str]:
        return {"status": "ok"}

    def ready(self) -> ReadinessReport:
        """Persistent instances declare the v2/v3 binding; others declare mode."""
        writable, reason = ensure_data_root(Path(self.config.data_dir))

        if not self.config.requires_workspace_binding:
            if not writable:
                return ephemeral_report(
                    mode=self.config.run_mode,
                    ok=False,
                    provider=self.provider_summary,
                    reasons=(reason or "data root is not writable",),
                )
            return ephemeral_report(mode=self.config.run_mode, provider=self.provider_summary)

        workspace_id = self.config.workspace_id
        owner_id = self.config.owner_id
        if not workspace_id or not owner_id:
            return persistent_report(
                workspace_id=workspace_id or "",
                ok=False,
                storage_layout=None,
                reasons=("TUTORLM_WORKSPACE_ID / TUTORLM_OWNER_ID 缺失：实例未绑定项目",),
            )

        facts = collect_facts(
            workspace_id=workspace_id,
            owner_id=owner_id,
            subdirectory=self.config.workspace_subdirectory,
            data_dir=Path(self.config.data_dir),
            session_dir=Path(self.config.session_data_dir) if self.config.session_data_dir else None,
        )
        decision = evaluate_storage_layout(facts)
        reasons = list(decision.reasons)

        if not writable:
            reasons.append(reason or "数据根不可写")

        # `legacy_root` is a *supported* layout, not a failure: contracts
        # instance-runtime-v3.md keeps old mount-root projects running under v2
        # behaviour ("旧卷无需搬迁"). The only consequence is that this instance
        # must not declare `content_only` (so it cannot be a share/clone source).
        legacy = is_legacy_layout(self.config.workspace_subdirectory)

        # A volume that is already initialised but whose proof is missing must not
        # be served as a freshly-created empty project. That guard applies to the
        # v3 subdirectory layout; a legacy root has no marker by construction.
        empty_volume_risk = (not legacy) and marker_is_missing_but_volume_initialized(facts)
        if empty_volume_risk:
            reasons.append("布局标记缺失但业务库已存在：拒绝静默生成空卷冒充原项目")

        ok = writable and not empty_volume_risk and (decision.declarable or legacy)

        return persistent_report(
            workspace_id=workspace_id,
            ok=ok,
            storage_layout=decision.layout if ok else None,
            provider=self.provider_summary,
            reasons=tuple(reasons),
            details={"initialized": decision.initialized} if not ok else None,
        )

    def facts(self) -> StorageProofFacts | None:
        """Exposed for diagnostics/tests; ``None`` when unbound."""
        if not self.config.requires_workspace_binding:
            return None
        if not self.config.workspace_id or not self.config.owner_id:
            return None
        return collect_facts(
            workspace_id=self.config.workspace_id,
            owner_id=self.config.owner_id,
            subdirectory=self.config.workspace_subdirectory,
            data_dir=Path(self.config.data_dir),
            session_dir=Path(self.config.session_data_dir) if self.config.session_data_dir else None,
        )


__all__ = ["HealthService"]