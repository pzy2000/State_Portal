from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


CheckpointValidity = Literal["replay-reachable", "save-restorable", "injection-only", "unknown"]
InstrumentationMode = Literal["existing_hook", "generated_patch", "in_place_test_gated", "injected_script", "none"]
RuntimeAccessMethod = Literal["test_hook", "exported_debug_bridge", "unsupported"]


@dataclass
class StoragePolicy:
    required_keys: list[str] = field(default_factory=list)
    safe_to_clear: list[str] = field(default_factory=list)
    safe_to_overwrite: list[str] = field(default_factory=list)
    preserve_keys: list[str] = field(default_factory=list)
    namespace_prefixes: list[str] = field(default_factory=list)

    def allows_write(self, key: str) -> bool:
        return key in self.safe_to_overwrite or any(key.startswith(prefix) for prefix in self.namespace_prefixes)

    def allows_clear(self, key: str) -> bool:
        return key in self.safe_to_clear or any(key.startswith(prefix) for prefix in self.namespace_prefixes)


@dataclass
class RepoMetadata:
    repo_path: str
    repo_name: str
    git_commit: str | None
    package_manager: str | None
    launch_command: str
    app_url: str | None
    build_command: str | None
    test_command: str | None
    scripts: dict[str, str] = field(default_factory=dict)


@dataclass
class CapabilityReport:
    browser_runtime: bool = False
    vite: bool = False
    static_html: bool = False
    react: bool = False
    vanilla_dom: bool = False
    three: bool = False
    react_three_fiber: bool = False
    webgl_or_canvas: bool = False
    local_storage: bool = False
    indexed_db: bool = False
    playwright: bool = False
    vitest: bool = False
    backend_or_external_api: bool = False
    deterministic_seed: bool = False
    hook_available: bool = False
    hook_name: str | None = None
    zustand_detected: bool = False
    zustand_source_paths: list[str] = field(default_factory=list)
    zustand_runtime_access_available: bool = False
    zustand_runtime_access_method: RuntimeAccessMethod = "unsupported"


@dataclass
class SemanticEvidence:
    source_path: str
    matched_symbol: str
    confidence: float
    detail: str = ""


@dataclass
class SemanticSurface:
    kind: str
    name: str
    access: list[str]
    source_path: str | None = None
    evidence: list[SemanticEvidence] = field(default_factory=list)


@dataclass
class SemanticField:
    name: str
    source: str
    readable_via: list[str]
    writable_via: list[str] = field(default_factory=list)
    evidence: list[SemanticEvidence] = field(default_factory=list)


@dataclass
class SemanticSchemaReport:
    discovery_mode: str = "source-guided semantic surface discovery"
    surfaces: list[SemanticSurface] = field(default_factory=list)
    fields: list[SemanticField] = field(default_factory=list)
    limitations: list[str] = field(
        default_factory=lambda: [
            "String and source-structure evidence, not full program synthesis.",
            "Field names are benchmark-specific semantic surfaces used by profiles.",
        ]
    )


@dataclass
class InspectResult:
    metadata: RepoMetadata
    profile: str
    adapter_layers: list[str]
    capabilities: CapabilityReport
    storage_policy: StoragePolicy
    instrumentation_mode: InstrumentationMode
    semantic_schema: SemanticSchemaReport = field(default_factory=SemanticSchemaReport)


@dataclass
class PhaseResult:
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: int | None = None


@dataclass
class RunReport:
    spec_path: str
    repo_path: str
    benchmark: str
    profile: str
    git_commit: str | None
    launch_command: str
    app_url: str | None
    adapter_layers_used: list[str]
    detected_capabilities: dict[str, Any]
    runtime_access_methods: dict[str, Any]
    storage_keys_touched: list[str]
    hook_availability: dict[str, Any]
    instrumentation_mode: InstrumentationMode
    checkpoint_strategy: str
    checkpoint_validity_class: CheckpointValidity
    target_validated_before_smoke: bool
    checkpoint_synthesis: PhaseResult
    checkpoint_restoration: PhaseResult
    validation: PhaseResult
    smoke_execution: PhaseResult
    oracle_result: PhaseResult
    visual_oracle_result: PhaseResult
    overall_status: str
    semantic_status: str = "pending"
    visual_audit_status: str = "pending"
    oracle_policy: str = "semantic"
    visual_audit_reason: str = "pending"
    validity_explanation: str = ""
    memory_persistence_diff: dict[str, Any] = field(default_factory=dict)
    soft_lock_signal: dict[str, Any] = field(default_factory=dict)
    render_readiness_signal: dict[str, Any] = field(default_factory=dict)
    post_checkpoint_interaction: dict[str, Any] = field(default_factory=dict)
    console_errors: list[str] = field(default_factory=list)
    network_api_calls: dict[str, Any] = field(default_factory=dict)
    unsupported_baseline_reasons: dict[str, str] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def path_str(path: Path) -> str:
    return str(path.expanduser().resolve())
