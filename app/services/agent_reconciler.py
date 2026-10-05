from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from app.services.agent_profiles import AgentProfileStore
from app.services.agent_mcp_adapters import project_mcp_spec
from app.services.state_store import PanelStateStore


ResourceAction = Literal["install", "update", "uninstall"]
ResourceType = Literal["provider", "mcp", "skill", "prompt", "router"]
_SYNC_TYPES = {"mcp", "skill", "prompt"}


@dataclass(frozen=True)
class AgentResourceOperation:
    action: ResourceAction
    resource_type: ResourceType
    resource_id: str
    client_id: str
    node_id: str
    reason: str = ""
    config: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentResourcePlan:
    operations: list[AgentResourceOperation]
    already_consistent: list[str]
    warnings: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.operations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operations": [operation.as_dict() for operation in self.operations],
            "already_consistent": list(self.already_consistent),
            "warnings": list(self.warnings),
            "changed": self.changed,
        }


class AgentReconciler:
    """Build minimal, explainable plans from database intent and observations."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.state = PanelStateStore(self.data_root)
        self.profiles = AgentProfileStore(self.data_root)

    def plan_profile(
        self,
        profile_id: str,
        *,
        node_id: str = "__local__",
        client_id: str | None = None,
        observations: Mapping[str, Any] | None = None,
        platform: str = "linux",
    ) -> AgentResourcePlan:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise FileNotFoundError(f"Profile 不存在: {profile_id}")
        selected_items = [
            dict(item)
            for item in profile.get("items", [])
            if client_id is None or item.get("client_id") == client_id
        ]
        clients = {
            str(item["client_id"])
            for item in selected_items
        }
        if client_id is not None:
            clients.add(client_id)

        operations: list[AgentResourceOperation] = []
        consistent: list[str] = []
        warnings: list[str] = []
        desired: set[tuple[str, str, str]] = set()
        for item in selected_items:
            item_client = str(item["client_id"])
            resource_type = str(item["resource_type"])
            resource_id = str(item["resource_id"])
            desired.add((item_client, resource_type, resource_id))
            if not self._resource_exists(item_client, resource_type, resource_id):
                warnings.append(
                    f"Profile 引用了不存在的资源: {item_client}/{resource_type}/{resource_id}"
                )
                continue
            observation = self._observation(
                node_id,
                item_client,
                resource_type,
                resource_id,
                observations,
            )
            if observation and observation.get('status') == 'error':
                warnings.append(f'回读失败，已跳过操作: {item_client}/{resource_type}/{resource_id}')
                continue
            action, reason, warning = self._desired_action(
                item_client,
                resource_type,
                resource_id,
                observation,
                platform=platform,
                config=dict(item.get("config") or {}),
            )
            if warning:
                warnings.append(warning)
            key = _resource_key(item_client, resource_type, resource_id)
            if action is None:
                consistent.append(key)
                continue
            operations.append(
                AgentResourceOperation(
                    action=action,
                    resource_type=_resource_type(resource_type),
                    resource_id=resource_id,
                    client_id=item_client,
                    node_id=node_id,
                    reason=reason,
                    config=dict(item.get("config") or {}),
                )
            )

        for assignment in self._assignments(node_id, client_id):
            assignment_client = str(assignment["client_id"])
            resource_type = str(assignment["resource_type"])
            resource_id = str(assignment["resource_id"])
            key_tuple = (assignment_client, resource_type, resource_id)
            if assignment_client not in clients or key_tuple in desired:
                continue
            observation = self._observation(
                node_id,
                assignment_client,
                resource_type,
                resource_id,
                observations,
            )
            if observation and observation.get('status') == 'error':
                warnings.append(f'回读失败，已跳过操作: {assignment_client}/{resource_type}/{resource_id}')
                continue
            if _is_present(observation):
                operations.append(
                    AgentResourceOperation(
                        action="uninstall",
                        resource_type=_resource_type(resource_type),
                        resource_id=resource_id,
                        client_id=assignment_client,
                        node_id=node_id,
                        reason="assigned_but_not_in_profile",
                    )
                )
            else:
                consistent.append(
                    _resource_key(assignment_client, resource_type, resource_id)
                )

        return _sorted_plan(operations, consistent, warnings)

    def plan_resources(
        self,
        resource_type: str,
        resource_ids: list[str],
        client_id: str,
        *,
        action: Literal["install", "update", "uninstall"],
        node_id: str = "__local__",
        observations: Mapping[str, Any] | None = None,
        platform: str = "linux",
    ) -> AgentResourcePlan:
        clean_type = _resource_type(resource_type)
        operations: list[AgentResourceOperation] = []
        consistent: list[str] = []
        warnings: list[str] = []
        for resource_id in dict.fromkeys(str(value).strip() for value in resource_ids):
            if not resource_id:
                continue
            key = _resource_key(client_id, resource_type, resource_id)
            if not self._resource_exists(client_id, resource_type, resource_id):
                warnings.append(
                    f"资源不存在: {client_id}/{resource_type}/{resource_id}"
                )
                continue
            observation = self._observation(
                node_id,
                client_id,
                resource_type,
                resource_id,
                observations,
            )
            if observation and observation.get('status') == 'error':
                warnings.append(f'回读失败，已跳过操作: {client_id}/{resource_type}/{resource_id}')
                continue
            if action == "uninstall":
                if _is_present(observation):
                    operations.append(
                        AgentResourceOperation(
                            action="uninstall",
                            resource_type=clean_type,
                            resource_id=resource_id,
                            client_id=client_id,
                            node_id=node_id,
                            reason="explicit_uninstall",
                        )
                    )
                else:
                    consistent.append(key)
                continue
            planned_action, reason, warning = self._desired_action(
                client_id,
                resource_type,
                resource_id,
                observation,
                platform=platform,
                config={},
            )
            if warning:
                warnings.append(warning)
            if planned_action is None:
                consistent.append(key)
            else:
                operations.append(
                    AgentResourceOperation(
                        action=planned_action,
                        resource_type=clean_type,
                        resource_id=resource_id,
                        client_id=client_id,
                        node_id=node_id,
                        reason=reason,
                    )
                )
        return _sorted_plan(operations, consistent, warnings)

    def plan_assignments(
        self,
        *,
        node_id: str = "__local__",
        client_id: str | None = None,
        resource_types: set[str] | None = None,
        observations: Mapping[str, Any] | None = None,
        platform: str = "linux",
    ) -> AgentResourcePlan:
        selected_types = resource_types or _SYNC_TYPES
        operations: list[AgentResourceOperation] = []
        consistent: list[str] = []
        warnings: list[str] = []
        for assignment in self._assignments(node_id, client_id):
            resource_type = str(assignment["resource_type"])
            if resource_type not in selected_types:
                continue
            item_client = str(assignment["client_id"])
            resource_id = str(assignment["resource_id"])
            observation = self._observation(
                node_id,
                item_client,
                resource_type,
                resource_id,
                observations,
            )
            if observation and observation.get('status') == 'error':
                warnings.append(f'回读失败，已跳过操作: {item_client}/{resource_type}/{resource_id}')
                continue
            enabled = bool(assignment.get("desired_enabled", True))
            if not enabled:
                if _is_present(observation):
                    operations.append(
                        AgentResourceOperation(
                            action="uninstall",
                            resource_type=_resource_type(resource_type),
                            resource_id=resource_id,
                            client_id=item_client,
                            node_id=node_id,
                            reason="desired_disabled",
                        )
                    )
                else:
                    consistent.append(
                        _resource_key(item_client, resource_type, resource_id)
                    )
                continue
            if not self._resource_exists(item_client, resource_type, resource_id):
                warnings.append(
                    f"分配引用了不存在的资源: {item_client}/{resource_type}/{resource_id}"
                )
                continue
            action, reason, warning = self._desired_action(
                item_client,
                resource_type,
                resource_id,
                observation,
                platform=platform,
                config={},
            )
            if warning:
                warnings.append(warning)
            if action is None:
                consistent.append(_resource_key(item_client, resource_type, resource_id))
            else:
                operations.append(
                    AgentResourceOperation(
                        action=action,
                        resource_type=_resource_type(resource_type),
                        resource_id=resource_id,
                        client_id=item_client,
                        node_id=node_id,
                        reason=reason,
                    )
                )
        return _sorted_plan(operations, consistent, warnings)

    def _assignments(
        self, node_id: str, client_id: str | None
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.state.list_mcp_assignments(node_id, client_id):
            rows.append(
                {
                    **item,
                    "resource_type": "mcp",
                    "resource_id": item["mcp_id"],
                }
            )
        for item in self.state.list_skill_assignments(node_id, client_id):
            rows.append(
                {
                    **item,
                    "resource_type": "skill",
                    "resource_id": item["skill_id"],
                }
            )
        for item in self.state.list_prompt_assignments(node_id, client_id):
            rows.append(
                {
                    **item,
                    "resource_type": "prompt",
                    "resource_id": item["prompt_id"],
                }
            )
        return rows

    def _resource_exists(
        self, client_id: str, resource_type: str, resource_id: str
    ) -> bool:
        if resource_type == "mcp":
            return resource_id in self.state.list_mcp_servers()
        if resource_type == "skill":
            return resource_id in self.state.list_skills()
        if resource_type == "prompt":
            return resource_id in self.state.list_prompts()
        if resource_type == "provider":
            return self.state.get_agent_provider(client_id, resource_id) is not None
        if resource_type == "router":
            return True
        return False

    def _observation(
        self,
        node_id: str,
        client_id: str,
        resource_type: str,
        resource_id: str,
        override: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if override is not None:
            value = _override_value(override, client_id, resource_type, resource_id)
            return _normalize_observation(value)
        if resource_type == "mcp":
            scan_state = self.state.get_mcp_scan_state(node_id, client_id)
            if scan_state.get('status') == 'error':
                return {'status': 'error', 'present': False, 'error': scan_state.get('error')}
            rows = self.state.list_mcp_observations(node_id, client_id)
            return next((row for row in rows if row["mcp_id"] == resource_id), None)
        if resource_type == "skill":
            rows = self.state.list_skill_observations(node_id, client_id)
            return next((row for row in rows if row["skill_id"] == resource_id), None)
        if resource_type == "prompt":
            rows = self.state.list_prompt_observations(node_id, client_id)
            return next((row for row in rows if row["prompt_id"] == resource_id), None)
        if resource_type == "provider":
            provider = self.state.get_agent_provider(client_id, resource_id)
            return (
                {"status": "installed", "present": True}
                if provider and provider.get("is_current")
                else None
            )
        if resource_type == "router":
            return {"status": "unknown", "present": True}
        return None

    def _desired_action(
        self,
        client_id: str,
        resource_type: str,
        resource_id: str,
        observation: dict[str, Any] | None,
        *,
        platform: str,
        config: dict[str, Any],
    ) -> tuple[ResourceAction | None, str, str | None]:
        if resource_type == "router":
            snapshot = self.state.get_router_snapshot("__local__")
            if _router_matches(snapshot, client_id, config):
                return None, "already_consistent", None
            return "update", "router_configuration_differs", None
        if observation is None or not _is_present(observation):
            action: ResourceAction = (
                "update" if resource_type == "provider" else "install"
            )
            return action, "missing", None
        status = str(observation.get("status") or "unknown").lower()
        warning = None
        if status == "error":
            warning = f"回读错误，将尝试修复: {client_id}/{resource_type}/{resource_id}"
        if status in {"drifted", "drift", "outdated", "error"}:
            return "update", status, warning
        expected_hash = self._expected_hash(
            client_id, resource_type, resource_id, platform
        )
        observed_hash = observation.get("spec_hash") or observation.get("content_hash")
        if expected_hash and observed_hash and str(expected_hash) != str(observed_hash):
            return "update", "hash_mismatch", warning
        if status in {"installed", "present", "ok", "verified"}:
            return None, "already_consistent", warning
        return "install", status or "missing", warning

    def _expected_hash(
        self, client_id: str, resource_type: str, resource_id: str, platform: str
    ) -> str | None:
        if resource_type == "mcp":
            server = self.state.list_mcp_servers().get(resource_id)
            if server is None:
                return None
            variants = self.state.list_mcp_variants(resource_id, client_id, platform)
            if not variants and platform != "any":
                variants = self.state.list_mcp_variants(resource_id, client_id, "any")
            spec = variants[0]["spec"] if variants else server.get("spec") or {}
            return _json_hash(project_mcp_spec(client_id, dict(spec)))
        if resource_type == "skill":
            skill = self.state.list_skills().get(resource_id)
            return str(skill.get("content_hash") or "") if skill else None
        if resource_type == "prompt":
            prompt = self.state.list_prompts().get(resource_id)
            if prompt is None:
                return None
            variants = self.state.list_prompt_variants(
                resource_id, client_id, platform
            )
            if not variants and platform != "any":
                variants = self.state.list_prompt_variants(
                    resource_id, client_id, "any"
                )
            content = variants[0]["content"] if variants else prompt.get("content") or ""
            return hashlib.sha256(str(content).encode("utf-8")).hexdigest()
        return None


def _override_value(
    observations: Mapping[str, Any],
    client_id: str,
    resource_type: str,
    resource_id: str,
) -> Any:
    client_scope = observations.get(client_id)
    if isinstance(client_scope, Mapping):
        type_scope = client_scope.get(resource_type)
        if isinstance(type_scope, Mapping) and resource_id in type_scope:
            return type_scope[resource_id]
    type_scope = observations.get(resource_type)
    if isinstance(type_scope, Mapping):
        return type_scope.get(resource_id)
    return None


def _normalize_observation(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"status": value, "present": value.lower() not in {"missing", "absent"}}
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _is_present(observation: Mapping[str, Any] | None) -> bool:
    if observation is None:
        return False
    if "present" in observation:
        return bool(observation.get("present"))
    return str(observation.get("status") or "").lower() not in {
        "",
        "missing",
        "absent",
        "uninstalled",
    }


def _router_matches(
    snapshot: Mapping[str, Any], client_id: str, config: Mapping[str, Any]
) -> bool:
    if not config:
        return bool(snapshot)
    expected_takeover = config.get("takeover")
    if expected_takeover is not None:
        actual = bool((snapshot.get("takeover") or {}).get(client_id))
        if actual != bool(expected_takeover):
            return False
    expected_provider = config.get("provider_id")
    if expected_provider is not None:
        actual_provider = str((snapshot.get("provider_ids") or {}).get(client_id) or "")
        if actual_provider != str(expected_provider):
            return False
    expected_queue = config.get("failover_queue")
    if expected_queue is not None:
        actual_queue = (snapshot.get("failover_queues") or {}).get(client_id, [])
        if list(actual_queue) != list(expected_queue):
            return False
    policy = config.get("policy")
    if isinstance(policy, Mapping):
        actual_policy = (snapshot.get("providers") or {}).get(client_id, {})
        if any(actual_policy.get(key) != value for key, value in policy.items()):
            return False
    return True


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _resource_type(value: str) -> ResourceType:
    if value not in {"provider", "mcp", "skill", "prompt", "router"}:
        raise ValueError(f"unsupported resource type: {value}")
    return value  # type: ignore[return-value]


def _resource_key(client_id: str, resource_type: str, resource_id: str) -> str:
    return f"{client_id}:{resource_type}:{resource_id}"


def _sorted_plan(
    operations: list[AgentResourceOperation],
    consistent: list[str],
    warnings: list[str],
) -> AgentResourcePlan:
    action_order = {"uninstall": 0, "install": 1, "update": 2}
    return AgentResourcePlan(
        operations=sorted(
            operations,
            key=lambda item: (
                item.client_id,
                item.resource_type,
                action_order[item.action],
                item.resource_id,
            ),
        ),
        already_consistent=sorted(set(consistent)),
        warnings=list(dict.fromkeys(warnings)),
    )
