"""Publish Graph outputs through the content store and the existing Team board."""

from __future__ import annotations

import json
from typing import Any, Protocol

from operant.application.team import TeamRepository
from operant.domain.graph import GraphWorkflowRun, NodeRun, NodeSpec
from operant.domain.team import ArtifactBoardUpdate, TeamArtifact
from operant.domain.threads import Artifact, ArtifactSensitivity, ArtifactSourceType

MAX_GRAPH_ARTIFACT_BYTES = 1_048_576


class GraphArtifactError(ValueError):
    pass


class ArtifactService(Protocol):
    def create_artifact(self, **kwargs: Any) -> tuple[Artifact, bool]: ...

    def get_artifact(self, artifact_id: str, *, verify: bool = True) -> Artifact: ...


def _bind_content(value: Any, inputs: dict[str, Any], *, depth: int = 0) -> Any:
    if depth > 32:
        raise GraphArtifactError("Artifact content binding exceeds the nesting limit")
    if isinstance(value, dict):
        if set(value) == {"$input"}:
            port = value["$input"]
            if not isinstance(port, str) or port not in inputs:
                raise GraphArtifactError("Artifact content binding names a missing input")
            return inputs[port]
        if not all(isinstance(key, str) for key in value):
            raise GraphArtifactError("Artifact object keys must be strings")
        return {key: _bind_content(item, inputs, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_bind_content(item, inputs, depth=depth + 1) for item in value]
    return value


def publish_graph_artifact(
    *,
    service: ArtifactService,
    team_repository: TeamRepository,
    run: GraphWorkflowRun,
    node_run: NodeRun,
    node: NodeSpec,
    publisher_id: str,
    session_ids: set[str],
) -> dict[str, Any]:
    """Keep blob deduplication stable; the board and NodeRun bind it to this run.

    Caller must invoke this only for the current ready Artifact node. Raw content
    is not enveloped or changed, so exported text retains the author's bytes.
    Existing references need current-run provenance; known hashes do not grant it.
    """
    if not run.team_run_id:
        raise GraphArtifactError("Artifact publication requires a prepared Team")
    title = node.metadata.get("title", node.node_id)
    if not isinstance(title, str) or not title or len(title) > 500:
        raise GraphArtifactError("Artifact title must be bounded text")
    roster = team_repository.list_roster(run.team_run_id)
    if publisher_id not in {entry.agent_instance_id for entry in roster}:
        raise GraphArtifactError("Artifact publisher is not in the current Graph Team")
    existing_board = team_repository.list_artifacts(
        team_run_id=run.team_run_id, viewer_id=publisher_id, owner_audit=True
    )
    artifact_id = node_run.input_refs.get("artifact_id", node.metadata.get("artifact_id"))
    if artifact_id:
        if not isinstance(artifact_id, str):
            raise GraphArtifactError("Artifact reference must be a string")
        artifact = service.get_artifact(artifact_id, verify=True)
        source_bound = any(
            ref.source_type is ArtifactSourceType.SESSION and ref.source_id in session_ids
            for ref in artifact.source_refs
        )
        board_bound = any(item.artifact_id == artifact.id for item in existing_board)
        if not source_bound and not board_bound:
            raise GraphArtifactError("Artifact is not bound to this Graph Run")
    else:
        content = node.metadata.get("content", node_run.input_refs.get("content"))
        content = _bind_content(content, node_run.input_refs)
        if content is None:
            raise GraphArtifactError("Artifact node requires content or a current-run artifact_id")
        if isinstance(content, str):
            encoded = content.encode("utf-8")
            default_type = "text/plain; charset=utf-8"
        else:
            try:
                encoded = json.dumps(
                    content, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise GraphArtifactError("Artifact content must be finite JSON or text") from exc
            default_type = "application/json"
        if len(encoded) > MAX_GRAPH_ARTIFACT_BYTES:
            raise GraphArtifactError("Artifact content exceeds the bounded Graph output limit")
        media_type = node.metadata.get("media_type", default_type)
        if not isinstance(media_type, str) or not media_type or len(media_type) > 200:
            raise GraphArtifactError("Artifact media_type must be bounded text")
        sensitivity = ArtifactSensitivity(node.metadata.get("sensitivity", "normal"))
        artifact, _created = service.create_artifact(
            content=encoded,
            media_type=media_type,
            sensitivity=sensitivity,
            source_refs=(),
            retention_policy_ref="graph-node-output",
        )
        # create_artifact refuses content-hash collisions with incompatible
        # metadata, including a higher sensitivity or another retention policy.
        service.get_artifact(artifact.id, verify=True)
    if not any(item.artifact_id == artifact.id for item in existing_board):
        update = ArtifactBoardUpdate(
            idempotency_key=f"graph-artifact:{node_run.id}:{node_run.iteration}:{artifact.id}",
            expected_revision=0,
            artifact=TeamArtifact(
                artifact_id=artifact.id,
                team_run_id=run.team_run_id,
                title=title,
                media_type=artifact.media_type,
                publisher_id=publisher_id,
            ),
        )
        try:
            team_repository.apply_artifact_update(update)
        except ValueError:
            # Another ready node may publish the same content concurrently.
            # Accept only the now-visible immutable blob's current-Team binding.
            current = team_repository.list_artifacts(
                team_run_id=run.team_run_id, viewer_id=publisher_id, owner_audit=True
            )
            if not any(item.artifact_id == artifact.id for item in current):
                raise
    available: dict[str, Any] = {
        "artifact_id": artifact.id,
        "content_hash": artifact.content_hash,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
    }
    if len(node.output_ports) == 1:
        available.setdefault(node.output_ports[0].name, artifact.id)
    return {port.name: available[port.name] for port in node.output_ports if port.name in available}
