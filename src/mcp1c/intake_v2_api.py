"""Синхронный application service для HTTP/UI единого intake.

Starlette остаётся тонкой границей транспорта: здесь принимаются только
``candidate_id`` и действие, строятся JSON-ready снимки, а server-side пути
остаются внутри ``IntakeLifecycle``.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .intake_v2 import (
    CandidateJobState,
    DurableCandidateStore,
    ExportIdentity,
    LayerManifest,
    SourceKind,
)
from .intake_v2_composition import CompositionError
from .intake_v2_lifecycle import (
    CandidateCatalog,
    DiscoveredCandidate,
    IntakeLifecycle,
    LifecycleError,
)
from .intake_v2_operations import IntakeCommitResult, IntakeCoordinator
from .intake_v2_planner import IntakeAction, LayerVersion
from .intake_v2_registry import GenerationOrigin, GenerationView
from .intake_v2_transport import BrowserStagingStore
from .registry import Registry, RegistryError, RegistrySnapshot


_MAX_EXTENSION_IMPACT_ITEMS = 200


class IntakeApiError(RuntimeError):
    """Запрос application service имеет неверный или недоступный контекст."""


class IntakeApiConflict(IntakeApiError):
    """Запрос конфликтует с durable job либо текущим Registry."""


class IntakeApiNotFound(IntakeApiError):
    """Candidate или job с указанным идентификатором отсутствует."""


@dataclass(frozen=True, slots=True)
class IntakeWork:
    job_id: str
    candidate_id: str
    action: IntakeAction
    active: GenerationView | None
    generation_id: str
    resume: bool = False


def _layer_version_payload(
    layer: LayerManifest | LayerVersion | None,
) -> dict[str, object] | None:
    if layer is None:
        return None
    return {
        "state": layer.state.value,
        "content_sha256": layer.content_sha256,
        "items_total": layer.items_total,
        "error": layer.error,
    }


def _commit_payload(result: IntakeCommitResult) -> dict[str, object]:
    return {
        "no_op": result.no_op,
        "generation_id": result.pointer.generation_id,
        "manifest_sha256": result.pointer.manifest_sha256,
        "applied_layers": sorted(layer.value for layer in result.applied_layers),
    }


class IntakeApiService:
    """Один вход для candidate list, preview, progress и confirm."""

    def __init__(self, registry: Registry, lifecycle: IntakeLifecycle):
        if not isinstance(registry, Registry):
            raise TypeError("registry должен быть Registry")
        if not isinstance(lifecycle, IntakeLifecycle):
            raise TypeError("lifecycle должен быть IntakeLifecycle")
        self.registry = registry
        self.lifecycle = lifecycle

    @classmethod
    def for_registry(
        cls,
        registry: Registry,
        *,
        local_source: Path | None = None,
        directory_settle_seconds: float = 5.0,
    ) -> IntakeApiService:
        root = registry.data_dir / "intake-v2"
        browser = BrowserStagingStore(root / "uploads")
        records = DurableCandidateStore(root / "records")
        lifecycle = IntakeLifecycle(
            CandidateCatalog(root / "catalog"),
            browser,
            IntakeCoordinator(root / "operations", records),
            incoming_root=registry.incoming_dir,
            local_sources=(
                {"local": Path(local_source)} if local_source is not None else None
            ),
            directory_settle_seconds=directory_settle_seconds,
        )
        return cls(registry, lifecycle)

    def _actions(
        self,
        candidate: DiscoveredCandidate,
        registry_snapshot: RegistrySnapshot | None = None,
    ) -> list[str]:
        if candidate.probe.source_kind is SourceKind.EXTENSION:
            # Родитель выбирается при start. До этого можно обещать только
            # полную первую загрузку/замену отдельного extension layer.
            return [IntakeAction.UPDATE_FULL.value]
        snapshot = registry_snapshot or self.registry.snapshot()
        configuration = candidate.probe.internal_name
        if configuration not in snapshot.configuration_names:
            return [IntakeAction.CREATE.value]
        identity = ExportIdentity.configuration(configuration)
        if snapshot.generation(identity) is None:
            return [IntakeAction.UPDATE_FULL.value]
        return [IntakeAction.UPDATE_CONTENT.value, IntakeAction.UPDATE_FULL.value]

    def candidate_payload(
        self,
        candidate: DiscoveredCandidate,
        *,
        registry_snapshot: RegistrySnapshot | None = None,
    ) -> dict[str, object]:
        return {
            "id": candidate.candidate_id,
            "transport": candidate.probe.transport.value,
            "source_kind": candidate.probe.source_kind.value,
            "internal_name": candidate.probe.internal_name,
            "configuration_version": candidate.probe.configuration_version,
            "layout": candidate.probe.layout.value,
            "origin_name": candidate.probe.origin_name,
            "raw_sha256": candidate.probe.raw_sha256,
            "requires_parent": candidate.probe.source_kind is SourceKind.EXTENSION,
            "actions": self._actions(candidate, registry_snapshot),
        }

    def snapshot(self) -> dict[str, object]:
        refreshed = self.lifecycle.refresh()
        registry_snapshot = self.registry.snapshot()
        return {
            "api_version": "v1",
            "configuration_names": list(registry_snapshot.configuration_names),
            "candidates": [
                self.candidate_payload(
                    candidate,
                    registry_snapshot=registry_snapshot,
                )
                for candidate in refreshed.candidates
            ],
            "groups": [
                {
                    "source_kind": key[0],
                    "internal_name": key[1],
                    "candidate_ids": list(candidate_ids),
                }
                for key, candidate_ids in refreshed.groups.items()
            ],
            "issues": [
                {
                    "source_id": issue.source_id,
                    "origin_name": issue.origin_name,
                    "message": issue.message,
                }
                for issue in refreshed.issues
            ],
            "jobs": [
                self.job_payload(job.job_id)
                for job in self.lifecycle.operations.records.list_jobs()
            ],
        }

    def accept_upload(
        self,
        origin_name: str,
        stream: BinaryIO,
        *,
        expected_size: int | None,
    ) -> dict[str, object]:
        candidate_id = f"candidate-{secrets.token_hex(16)}"
        accepted = False
        try:
            self.lifecycle.browser.accept(
                candidate_id,
                origin_name,
                stream,
                expected_size=expected_size,
            )
            accepted = True
            candidate = self.lifecycle.discover_browser(candidate_id)
            return self.candidate_payload(candidate)
        except Exception:
            if accepted:
                try:
                    self.lifecycle.browser.discard(candidate_id)
                except Exception:
                    pass
            raise

    def _active(
        self,
        candidate: DiscoveredCandidate,
        action: IntakeAction,
        *,
        parent_configuration: str = "",
    ) -> GenerationView | None:
        if candidate.probe.source_kind is SourceKind.EXTENSION:
            if action is IntakeAction.CREATE:
                raise IntakeApiConflict(
                    "Расширение загружается действием update_full, а не create."
                )
            if not parent_configuration:
                raise IntakeApiConflict("Для расширения обязателен родитель.")
            snapshot = self.registry.snapshot()
            if parent_configuration not in snapshot.configuration_names:
                raise IntakeApiConflict(
                    f"Родитель расширения не загружен: {parent_configuration}."
                )
            identity = ExportIdentity.extension(
                candidate.probe.internal_name,
                parent_configuration=parent_configuration,
            )
            generation = snapshot.generation(identity)
            if generation is not None:
                return generation.view
            if action is IntakeAction.UPDATE_CONTENT:
                raise IntakeApiConflict(
                    "Для update сначала нужен существующий extension generation."
                )
            return None
        configuration = candidate.probe.internal_name
        snapshot = self.registry.snapshot()
        exists = configuration in snapshot.configuration_names
        if action is IntakeAction.CREATE:
            if exists:
                raise IntakeApiConflict("Конфигурация уже существует.")
            return None
        if not exists:
            raise IntakeApiConflict("Конфигурация для обновления не найдена.")
        try:
            active = snapshot.generation_view(configuration)
        except RegistryError as error:
            raise IntakeApiConflict(str(error)) from error
        if (
            action is IntakeAction.UPDATE_CONTENT
            and active.origin is GenerationOrigin.LEGACY
        ):
            raise IntakeApiConflict(
                "Для legacy-конфигурации сначала требуется полное обновление."
            )
        return active

    def start(
        self,
        candidate_id: str,
        action: str,
        *,
        job_id: str = "",
        parent_configuration: str = "",
    ) -> IntakeWork:
        try:
            selected_action = IntakeAction(action)
        except (TypeError, ValueError) as error:
            raise IntakeApiError("Неизвестное действие intake.") from error
        try:
            candidate = self.lifecycle.catalog.load(candidate_id)
        except KeyError:
            raise IntakeApiNotFound("Candidate не найден.") from None
        job_id = job_id or f"job-{secrets.token_hex(16)}"
        try:
            previous = self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            previous = None
        if previous is not None and previous.state is CandidateJobState.PARSING:
            self.lifecycle.start(
                job_id,
                candidate_id,
                parent_configuration=parent_configuration,
            )
            return IntakeWork(
                job_id,
                candidate_id,
                selected_action,
                None,
                "",
                resume=True,
            )
        if previous is not None and previous.state is CandidateJobState.DONE:
            try:
                preview = self.lifecycle.operations.load_preview(job_id)
            except KeyError:
                raise IntakeApiConflict(
                    "Durable preview готовой job недоступен."
                ) from None
            if preview.plan.action is not selected_action:
                raise IntakeApiConflict("Action не совпадает с готовым preview.")
            raise IntakeApiConflict("Job уже содержит готовый preview.")
        active = self._active(
            candidate,
            selected_action,
            parent_configuration=parent_configuration,
        )
        try:
            self.lifecycle.start(
                job_id,
                candidate_id,
                parent_configuration=parent_configuration,
            )
        except LifecycleError as error:
            raise IntakeApiConflict(str(error)) from error
        return IntakeWork(
            job_id,
            candidate_id,
            selected_action,
            active,
            f"generation-{secrets.token_hex(16)}",
        )

    def prepare(self, work: IntakeWork) -> None:
        if not isinstance(work, IntakeWork):
            raise TypeError("work должен быть IntakeWork")
        try:
            if work.resume:
                self.lifecycle.resume(
                    work.job_id,
                    expected_action=work.action,
                )
                return
            self.lifecycle.prepare(
                work.job_id,
                action=work.action,
                active=work.active,
                generation_id=work.generation_id,
            )
        except Exception as error:
            self.lifecycle.operations.fail(work.job_id, error)
            raise

    def confirm(self, job_id: str) -> dict[str, object]:
        try:
            self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            raise IntakeApiNotFound("Job не найдена.") from None
        try:
            self.lifecycle.operations.confirm(job_id, self.registry)
        except KeyError:
            raise IntakeApiConflict(
                "Durable preview или commit result job недоступен."
            ) from None
        except CompositionError as error:
            # Preview, созданный прежней версией сервера, может пережить
            # restart. Он тоже должен завершаться контролируемым конфликтом.
            raise IntakeApiConflict(str(error)) from error
        return self.job_payload(job_id)

    def job_payload(self, job_id: str) -> dict[str, object]:
        try:
            job = self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            raise IntakeApiNotFound("Job не найдена.") from None
        payload: dict[str, object] = {
            "job_id": job.job_id,
            "candidate_id": job.candidate_id,
            "state": job.state.value,
            "stage": job.stage.value,
            "error": job.error,
            "preview": None,
            "commit": None,
        }
        if job.state is not CandidateJobState.DONE:
            return payload
        try:
            preview = self.lifecycle.operations.load_preview(job_id)
        except KeyError:
            raise IntakeApiConflict(
                "Durable preview готовой job недоступен."
            ) from None
        payload["preview"] = {
            "action": preview.plan.action.value,
            "no_op": preview.plan.no_op,
            "identity": preview.plan.identity.to_dict(),
            "base_generation_id": preview.plan.base_generation_id,
            "candidate_generation_id": preview.plan.candidate_generation_id,
            "layers": [
                {
                    "kind": layer.kind.value,
                    "decision": layer.decision.value,
                    "reason": layer.reason.value,
                    "current": _layer_version_payload(layer.current),
                    "candidate": _layer_version_payload(layer.candidate),
                }
                for layer in preview.plan.layers
            ],
        }
        impacts = tuple(
            item
            for item in self.registry.preview_extension_relations(
                preview.materialized.root,
                preview.materialized.manifest,
            )
            if item.state.value == "target_missing"
        )
        payload["preview"]["extension_impacts"] = {
            "total": len(impacts),
            "items": [
                {
                    "extension": item.extension,
                    "target": item.target,
                    "state": item.state.value,
                }
                for item in impacts[:_MAX_EXTENSION_IMPACT_ITEMS]
            ],
            "truncated": len(impacts) > _MAX_EXTENSION_IMPACT_ITEMS,
        }
        try:
            result = self.lifecycle.operations.load_commit(job_id)
        except KeyError:
            result = None
        if result is not None:
            payload["commit"] = _commit_payload(result)
        return payload


__all__ = [
    "IntakeApiConflict",
    "IntakeApiError",
    "IntakeApiNotFound",
    "IntakeApiService",
    "IntakeWork",
]
