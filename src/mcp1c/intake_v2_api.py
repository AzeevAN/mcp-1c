"""Синхронный application service для HTTP/UI единого intake.

Starlette остаётся тонкой границей транспорта: здесь принимаются только
``candidate_id`` и действие, строятся JSON-ready снимки, а server-side пути
остаются внутри ``IntakeLifecycle``.
"""

from __future__ import annotations

import functools
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import fsio
from .intake_v2 import (
    CandidateStoreError,
    CandidateTransport,
    CandidateJobStage,
    CandidateJobState,
    DurableCandidateStore,
    ExportIdentity,
    IntakeV2ContractError,
    LayerManifest,
    SourceKind,
    UnreadableJob,
)
from .intake_v2_composition import CompositionError
from .intake_v2_lifecycle import (
    CandidateCatalog,
    CandidateLocator,
    DiscoveredCandidate,
    IntakeLifecycle,
    LifecycleConflict,
    LifecycleError,
)
from .intake_v2_operations import (
    IntakeCommitResult,
    IntakeCoordinator,
    IntakePreview,
    OperationConflict,
    OperationError,
    OperationStalePreview,
)
from .intake_v2_planner import IntakeAction, LayerVersion
from .intake_v2_registry import GenerationPointer, GenerationView
from .intake_v2_transport import (
    BrowserStagingStore,
    TransportError,
)
from .intake_v2_probe import CandidateProbe, ProbeError, probe_export
from .config_sources import CONFIG_SOURCES_ROOT, ConfigSourceBindings, ConfigSourceError
from .editions import edition_conflict
from .registry import Registry, RegistryError, RegistrySnapshot
from .source_modes import ActivationMode, ActivationStatus


_MAX_EXTENSION_IMPACT_ITEMS = 200


class IntakeApiError(RuntimeError):
    """Запрос application service имеет неверный или недоступный контекст."""


class IntakeApiConflict(IntakeApiError):
    """Запрос конфликтует с durable job либо текущим Registry."""


class IntakeApiNotFound(IntakeApiError):
    """Candidate или job с указанным идентификатором отсутствует."""


class IntakeApiInvalid(IntakeApiError):
    """Идентификатор из запроса не может быть именем durable-записи."""


def _request_identifier(value: object, label: str) -> str:
    """Проверить id клиента до того, как он станет компонентом пути.

    Хранилища проверяют id и сами, но их отказ — исключение контракта, которое
    HTTP-слой видел как необработанное (500). Здесь отказ однозначно 422.
    """
    if not fsio.is_safe_identifier(value):
        raise IntakeApiInvalid(
            f"{label} должен содержать только латинские буквы, цифры, '.', '_' "
            "или '-', не начинаться с '.' и быть не длиннее 128 символов."
        )
    return value  # type: ignore[return-value]


def _store_conflicts(method):
    """Повреждённое durable-хранилище — конфликт запроса, а не авария.

    ``CandidateStoreError`` (битая или несовместимая запись job/candidate)
    иначе доходила до HTTP-слоя необработанной и превращалась в 500.
    """

    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except CandidateStoreError as error:
            raise IntakeApiConflict(str(error)) from error

    return wrapped


@dataclass(frozen=True, slots=True)
class IntakeWork:
    job_id: str
    candidate_id: str
    action: IntakeAction
    active: GenerationView | None
    generation_id: str


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
        # Изменение binding ждёт завершения confirm, чтение списка — только
        # короткой проверки. Порядок замков: mutation -> directory.
        self._directory_mutation_lock = threading.RLock()
        self._directory_lock = threading.RLock()
        self.bindings = ConfigSourceBindings(
            registry.data_dir / "config-source-bindings.json",
            lifecycle.config_sources_root or CONFIG_SOURCES_ROOT,
        )

    @classmethod
    def for_registry(
        cls,
        registry: Registry,
        *,
        config_sources_root: Path = CONFIG_SOURCES_ROOT,
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
            config_sources_root=Path(config_sources_root),
            directory_settle_seconds=directory_settle_seconds,
        )
        return cls(registry, lifecycle)

    def directory_sources(self) -> dict[str, object]:
        try:
            with self._directory_lock:
                names = self.registry.snapshot().configuration_names
                return {"roots": self.bindings.roots(),
                        "bindings": {k: v for k, v in self.bindings.load().items() if k in names},
                        "configuration_names": list(names)}
        except ConfigSourceError as error:
            raise IntakeApiConflict(str(error)) from error

    def _directory_probe(
        self, configuration: str, source_id: str,
    ) -> tuple[CandidateLocator, CandidateProbe]:
        if configuration not in self.registry.snapshot().configuration_names:
            raise IntakeApiConflict("Конфигурация не загружена.")
        try:
            locator = CandidateLocator(CandidateTransport.LOCAL_DIRECTORY, source_id)
            with self.lifecycle._open(locator) as tree:
                probe = probe_export(tree)
            if (probe.source_kind is not SourceKind.CONFIGURATION
                    or probe.internal_name != configuration or probe.wrapper):
                raise IntakeApiConflict(
                    "Каталог не соответствует выбранной конфигурации. "
                    "Проверьте внутреннее имя и Configuration.xml в корне."
                )
            return locator, probe
        except (LifecycleError, ProbeError, TransportError, OSError) as error:
            raise IntakeApiConflict("Не удалось проверить каталог конфигурации: " + (
                "нет доступа" if isinstance(error, OSError) else str(error)
            )) from error

    def bind_directory(self, configuration: str, source_id: str) -> dict[str, object]:
        with self._directory_mutation_lock, self._directory_lock:
            self._directory_probe(configuration, source_id)
            try:
                bindings = self.bindings.load()
                if any(k != configuration and v == source_id for k, v in bindings.items()):
                    raise IntakeApiConflict("Каталог уже привязан к другой конфигурации.")
                bindings[configuration] = source_id
                self.bindings.save(bindings)
            except ConfigSourceError as error:
                raise IntakeApiConflict(str(error)) from error
            return self.directory_sources()

    def unbind_directory(self, configuration: str) -> dict[str, object]:
        with self._directory_mutation_lock, self._directory_lock:
            try:
                bindings = self.bindings.load()
                if configuration in bindings:
                    del bindings[configuration]
                    self.bindings.save(bindings)
            except ConfigSourceError as error:
                raise IntakeApiConflict(str(error)) from error
            return self.directory_sources()

    def refresh_directory(self, configuration: str) -> dict[str, object]:
        with self._directory_mutation_lock, self._directory_lock:
            try:
                source_id = self.bindings.load().get(configuration)
            except ConfigSourceError as error:
                raise IntakeApiConflict(str(error)) from error
            if source_id is None:
                raise IntakeApiConflict("К конфигурации не подключён каталог.")
            locator, probe = self._directory_probe(configuration, source_id)
            candidate = DiscoveredCandidate(self.lifecycle._candidate_id(locator, probe), locator, probe)
            self.lifecycle.catalog.save(candidate)
            return self.candidate_payload(candidate)

    def _validate_directory_candidate(self, candidate: DiscoveredCandidate) -> None:
        if (candidate.locator.transport is not CandidateTransport.LOCAL_DIRECTORY
                or self.lifecycle.config_sources_root is None):
            return
        configuration = candidate.probe.internal_name
        try:
            if self.bindings.load().get(configuration) != candidate.locator.source_id:
                raise IntakeApiConflict("Привязка каталога конфигурации изменилась.")
        except ConfigSourceError as error:
            raise IntakeApiConflict(str(error)) from error
        _, current = self._directory_probe(configuration, candidate.locator.source_id)
        if current.raw_sha256 != candidate.probe.raw_sha256:
            raise IntakeApiConflict("Каталог изменился. Заново нажмите «Обновить из каталога».")

    def _action_policy(
        self,
        candidate: DiscoveredCandidate,
        registry_snapshot: RegistrySnapshot | None = None,
    ) -> tuple[list[str], str]:
        """Доступные действия и причина, если полное обновление запрещено."""
        if candidate.probe.source_kind is SourceKind.EXTENSION:
            # Родитель выбирается при start. До этого можно обещать только
            # полную первую загрузку/замену отдельного extension layer.
            # Редакцию расширения не сверяем: его поколение задаёт родитель.
            return [IntakeAction.UPDATE_FULL.value], ""
        snapshot = registry_snapshot or self.registry.snapshot()
        configuration = candidate.probe.internal_name
        loaded = snapshot.configurations.get(configuration)
        if loaded is None:
            if configuration in snapshot.registered_configuration_names:
                return [], (
                    "Конфигурация уже зарегистрирована, но недоступна. "
                    "Восстановите источник или явно удалите конфигурацию."
                )
            return [IntakeAction.CREATE.value], ""
        reason = edition_conflict(
            configuration,
            loaded.config.version,
            candidate.probe.configuration_version,
        )
        if reason:
            return [], reason
        return [IntakeAction.UPDATE_FULL.value], ""

    def candidate_payload(
        self,
        candidate: DiscoveredCandidate,
        *,
        registry_snapshot: RegistrySnapshot | None = None,
    ) -> dict[str, object]:
        actions, blocked_reason = self._action_policy(candidate, registry_snapshot)
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
            "actions": actions,
            "blocked_reason": blocked_reason,
        }

    def _listed_job_payload(self, job) -> dict[str, object]:
        """Payload job для списка: сбой одной job не скрывает остальные.

        Иначе одна DONE-job с утраченным preview (оборванный discard, ручная
        правка каталога) отвечала 409 на весь список, и в интерфейсе нельзя
        было даже увидеть её, чтобы отменить. Состояние job остаётся
        настоящим: готовая job не выдаётся за упавшую, а признак ``broken``
        и причина говорят, что её данные не читаются. ``discardable``
        отвечает, примет ли сервер отмену: только неопубликованная готовая
        job.
        """
        try:
            return self.job_payload(job.job_id)
        except (
            IntakeApiError,
            LifecycleError,
            OperationError,
            RegistryError,
            CandidateStoreError,
            IntakeV2ContractError,
        ) as error:
            discardable = False
            if job.state is CandidateJobState.DONE:
                try:
                    self.lifecycle.operations.load_commit(job.job_id)
                except KeyError:
                    discardable = True
                except Exception:
                    # Commit result есть, но не читается: публикация могла
                    # пройти, и отменять такую job сервер не станет.
                    discardable = False
            return {
                "transport": None,
                "job_id": job.job_id,
                "candidate_id": job.candidate_id,
                "state": job.state.value,
                "stage": job.stage.value,
                "error": str(error) or error.__class__.__name__,
                "preview": None,
                "commit": None,
                "broken": True,
                "discardable": discardable,
            }

    @staticmethod
    def _unreadable_job_payload(record: UnreadableJob) -> dict[str, object]:
        """Строка списка для файла job, который не читается как запись."""
        name = record.name.removesuffix(".json")
        return {
            "transport": None,
            "job_id": name,
            # Строка, а не null: интерфейс сопоставляет job с кандидатами по
            # этому полю, а пустой id не совпадёт ни с одним.
            "candidate_id": "",
            "state": CandidateJobState.FAILED.value,
            "stage": CandidateJobStage.FAILED.value,
            "error": f"Запись job не читается: {record.error}",
            "preview": None,
            "commit": None,
            "broken": True,
            "discardable": False,
        }

    def _listed_jobs(self) -> list[dict[str, object]]:
        with self.lifecycle.stable_jobs() as (jobs, unreadable):
            dated = []
            broken = list(unreadable)
            for job in jobs:
                try:
                    modified = self.lifecycle.operations.records.job_modified_at(job.job_id)
                except KeyError:
                    continue
                except (CandidateStoreError, OSError) as error:
                    broken.append(UnreadableJob(f"{job.job_id}.json", str(error)))
                    continue
                dated.append((modified, job))
            dated.sort(key=lambda item: (-item[0], item[1].job_id))
            # Лимит касается только завершённой истории. Незавершённая работа
            # и неподтверждённый preview сами не истекают: спрятанные за
            # лимитом, они остались бы на диске без кнопки.
            listed, history = [], 0
            for _modified, job in dated:
                if self._always_listed(job):
                    listed.append(job)
                elif history < 50:
                    listed.append(job)
                    history += 1
            return [self._listed_job_payload(job) for job in listed] + [
                self._unreadable_job_payload(record) for record in broken
            ]

    def _always_listed(self, job) -> bool:
        if job.state is CandidateJobState.FAILED:
            return False
        if job.state is not CandidateJobState.DONE:
            return True
        commit = self.lifecycle.operations.commits_dir / f"{job.job_id}.json"
        try:
            commit.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    @_store_conflicts
    def jobs_snapshot(self) -> dict[str, object]:
        """Читать сохранённые операции без обнаружения или хеширования входов."""
        return {"jobs": self._listed_jobs()}

    @_store_conflicts
    def snapshot(self) -> dict[str, object]:
        refreshed = self.lifecycle.refresh()
        registry_snapshot = self.registry.snapshot()
        # Refresh может читать большие server-side источники и завершается до
        # этой короткой секции. Здесь нужна атомарность только между списком job
        # и их payload: иначе конкурентный discard оставляет ложный HTTP 409.
        job_payloads = self._listed_jobs()
        return {
            "api_version": "v1",
            "directories": self.directory_sources(),
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
            "jobs": job_payloads,
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
            parent_activation = self.registry.active_activation(
                ExportIdentity.configuration(parent_configuration)
            )
            if (
                parent_activation is None
                or parent_activation.status is not ActivationStatus.ACTIVE
                or parent_activation.mode is not ActivationMode.B_FULL
            ):
                raise IntakeApiConflict(
                    "Расширение доступно только поверх active B_FULL; "
                    "родитель требует полной загрузки Source B."
                )
            identity = ExportIdentity.extension(
                candidate.probe.internal_name,
                parent_configuration=parent_configuration,
            )
            generation = snapshot.generation(identity)
            if generation is not None:
                return generation.view
            return None
        configuration = candidate.probe.internal_name
        snapshot = self.registry.snapshot()
        exists = configuration in snapshot.configuration_names
        if action is IntakeAction.CREATE:
            if exists or configuration in snapshot.registered_configuration_names:
                raise IntakeApiConflict("Конфигурация уже существует.")
            return None
        if not exists:
            raise IntakeApiConflict("Конфигурация для обновления не найдена.")
        # Проверка здесь, а не только в списке действий: прямой вызов API и
        # «Обновить из каталога» должны получить отказ до создания job.
        reason = edition_conflict(
            configuration,
            snapshot.configurations[configuration].config.version,
            candidate.probe.configuration_version,
        )
        if reason:
            raise IntakeApiConflict(reason)
        try:
            active = snapshot.generation_view(configuration)
        except RegistryError as error:
            raise IntakeApiConflict(str(error)) from error
        return active

    @_store_conflicts
    def start(
        self,
        candidate_id: str,
        action: str,
        *,
        job_id: str = "",
        parent_configuration: str = "",
    ) -> IntakeWork:
        if action == "update":
            raise IntakeApiConflict(
                "Частичное обновление Source B удалено; выберите полное обновление."
            )
        try:
            selected_action = IntakeAction(action)
        except (TypeError, ValueError) as error:
            raise IntakeApiError("Неизвестное действие intake.") from error
        _request_identifier(candidate_id, "candidate_id")
        if job_id:
            _request_identifier(job_id, "job_id")
        try:
            candidate = self.lifecycle.catalog.load(candidate_id)
        except KeyError:
            raise IntakeApiNotFound("Candidate не найден.") from None
        self._validate_directory_candidate(candidate)
        job_id = job_id or f"job-{secrets.token_hex(16)}"
        try:
            previous = self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            previous = None
        if previous is not None and previous.state is CandidateJobState.PARSING:
            # Прежняя ветка «resume» сюда не доходила: маршрут не пускает start
            # при живой задаче intake, а PARSING прежнего процесса закрывается
            # при старте lifecycle. PARSING без задачи остаётся, только если
            # не удалось записать даже отказ; запускать поверх неё второй
            # parse с тем же work нельзя.
            raise IntakeApiConflict(
                "Job ещё выполняется или её состояние не удалось сохранить; "
                "дождитесь завершения или перезапустите сервис."
            )
        if previous is not None and previous.state is CandidateJobState.DONE:
            try:
                preview = self.lifecycle.operations.load_preview(job_id)
            except OperationStalePreview as error:
                raise IntakeApiConflict(str(error)) from error
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

    def _preview_active_pointer(
        self, identity: ExportIdentity, previous: GenerationPointer | None,
    ) -> GenerationPointer:
        expected = self.registry.active_generation_pointer(identity)
        if (
            expected is None or previous is None
            or expected.identity != previous.identity
            or expected.generation_id != previous.generation_id
            or expected.root_path != previous.root_path
        ):
            raise RegistryError("active generation отсутствует или изменился")
        return expected

    def _preview_impacts(self, preview: IntakePreview) -> dict[str, object]:
        if not preview.materialized.payloads:
            expected = self._preview_active_pointer(
                preview.plan.identity, preview.expected_previous
            )
            relations = self.registry.preview_active_extension_relations(expected)
        else:
            relations = self.registry.preview_extension_relations(
                preview.materialized.root, preview.materialized.manifest,
            )
        impacts = tuple(item for item in relations if item.state.value == "target_missing")
        return {
            "total": len(impacts),
            "items": [
                {"extension": item.extension, "target": item.target, "state": item.state.value}
                for item in impacts[:_MAX_EXTENSION_IMPACT_ITEMS]
            ],
            "truncated": len(impacts) > _MAX_EXTENSION_IMPACT_ITEMS,
            "basis": [list(item) for item in self._extension_basis(preview.plan.identity)],
        }

    def _extension_basis(self, identity: ExportIdentity) -> tuple[tuple[str, str, str], ...]:
        """Состав active-поколений той же конфигурации, кроме самого кандидата.

        Снимок влияния расширений считается при prepare; дешёвая сверка этого
        состава при чтении статуса показывает, что снимок устарел (M7), без
        восстановления слоёв (M8). Публикация пересчитывает связи сама.
        """
        configuration = (
            identity.configuration_name
            if identity.source_kind is SourceKind.CONFIGURATION
            else identity.parent_configuration
        )
        basis = []
        for generation in self.registry.snapshot().generations.values():
            other = generation.manifest.identity
            context = (
                other.configuration_name
                if other.source_kind is SourceKind.CONFIGURATION
                else other.parent_configuration
            )
            if context != configuration or other == identity:
                continue
            basis.append((
                other.source_kind.value,
                other.extension_name or other.configuration_name,
                generation.manifest.generation_id,
            ))
        return tuple(sorted(basis))

    def prepare(self, work: IntakeWork) -> None:
        if not isinstance(work, IntakeWork):
            raise TypeError("work должен быть IntakeWork")
        try:
            self.lifecycle.prepare(
                work.job_id,
                action=work.action,
                active=work.active,
                generation_id=work.generation_id,
                preview_impacts=self._preview_impacts,
            )
        except Exception as error:
            self.lifecycle.operations.fail(work.job_id, error)
            raise

    @_store_conflicts
    def confirm(self, job_id: str) -> dict[str, object]:
        _request_identifier(job_id, "job_id")
        try:
            self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            raise IntakeApiNotFound("Job не найдена.") from None
        try:
            with self._directory_mutation_lock:
                # Повторный confirm готового commit не зависит от доступности входа.
                if self.job_payload(job_id).get("commit") is None:
                    self.lifecycle.operations.load_preview(job_id)
                    job = self.lifecycle.operations.records.load_job(job_id)
                    durable = self.lifecycle.operations.records.load_candidate(
                        job.candidate_id
                    )
                    # Только directory-preview зависит от изменяемого binding и
                    # повторного probe. Browser-preview после prepare автономен:
                    # его ZIP уже может быть штатно очищен другим commit.
                    if durable.transport is CandidateTransport.LOCAL_DIRECTORY:
                        with self._directory_lock:
                            self._validate_directory_candidate(
                                self.lifecycle.catalog.load(job.candidate_id)
                            )
                self.lifecycle.operations.confirm(job_id, self.registry)
                self.lifecycle.release_committed_candidate(job_id)
        except KeyError:
            raise IntakeApiConflict(
                "Durable preview или commit result job недоступен."
            ) from None
        except OperationStalePreview as error:
            raise IntakeApiConflict(str(error)) from error
        except CompositionError as error:
            # Preview, созданный прежней версией сервера, может пережить
            # restart. Он тоже должен завершаться контролируемым конфликтом.
            raise IntakeApiConflict(str(error)) from error
        return self.job_payload(job_id)

    @_store_conflicts
    def discard(self, job_id: str) -> dict[str, str]:
        _request_identifier(job_id, "job_id")
        try:
            self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            raise IntakeApiNotFound("Job не найдена.") from None
        try:
            self.lifecycle.discard(job_id)
        except OperationConflict as error:
            raise IntakeApiConflict(str(error)) from error
        return {"discarded": job_id}

    @_store_conflicts
    def remove_browser_candidate(self, candidate_id: str) -> dict[str, str]:
        try:
            self.lifecycle.remove_browser_candidate(candidate_id)
        except KeyError:
            raise IntakeApiNotFound("Кандидат не найден.") from None
        except LifecycleConflict as error:
            raise IntakeApiConflict(str(error)) from error
        return {"deleted": candidate_id}

    @_store_conflicts
    def ensure_configuration_purgeable(self, configuration: str) -> None:
        try:
            self.lifecycle.ensure_configuration_purgeable(configuration)
        except LifecycleError as error:
            raise IntakeApiConflict(str(error)) from error

    @_store_conflicts
    def has_configuration_jobs(self, configuration: str) -> bool:
        try:
            return bool(self.lifecycle.configuration_jobs(configuration))
        except LifecycleError as error:
            raise IntakeApiConflict(str(error)) from error

    @_store_conflicts
    def purge_configuration(self, configuration: str) -> None:
        try:
            self.lifecycle.purge_configuration(configuration)
            self.unbind_directory(configuration)
        except LifecycleError as error:
            raise IntakeApiConflict(str(error)) from error

    @_store_conflicts
    def job_payload(self, job_id: str) -> dict[str, object]:
        _request_identifier(job_id, "job_id")
        try:
            job = self.lifecycle.operations.records.load_job(job_id)
        except KeyError:
            raise IntakeApiNotFound("Job не найдена.") from None
        try:
            transport = self.lifecycle.catalog.load(job.candidate_id).locator.transport
        except KeyError:
            # Browser staging очищается после commit, durable candidate остаётся.
            try:
                transport = self.lifecycle.operations.records.load_candidate(job.candidate_id).transport
            except KeyError:
                transport = None
        payload: dict[str, object] = {
            "transport": transport.value if transport is not None else None,
            "job_id": job.job_id,
            "candidate_id": job.candidate_id,
            "state": job.state.value,
            "stage": job.stage.value,
            "error": job.error,
            "preview": None,
            "commit": None,
            "broken": False,
            # Сервер примет discard только у готовой неопубликованной job.
            "discardable": job.state is CandidateJobState.DONE,
        }
        if job.state is not CandidateJobState.DONE:
            return payload
        try:
            result = self.lifecycle.operations.load_commit(job_id)
        except KeyError:
            result = None
        if result is not None:
            payload["commit"] = _commit_payload(result)
            payload["discardable"] = False
            return payload
        try:
            preview = self.lifecycle.operations.load_preview_status(job_id)
        except OperationStalePreview as error:
            # Один preview прежней версии не должен блокировать список
            # кандидатов и остальные durable jobs.
            payload.update(
                state=CandidateJobState.FAILED.value,
                stage=CandidateJobStage.FAILED.value,
                error=str(error),
            )
            return payload
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
        if preview.reuse_active:
            try:
                self._preview_active_pointer(
                    preview.plan.identity, preview.expected_previous
                )
            except RegistryError as error:
                # Устаревший компактный preview не блокирует список других jobs.
                payload.update(
                    state=CandidateJobState.FAILED.value,
                    stage=CandidateJobStage.FAILED.value,
                    error=str(error),
                    preview=None,
                )
                return payload
        impacts = dict(preview.extension_impacts)
        basis = impacts.pop("basis", None)
        impacts["stale"] = basis is not None and [
            list(item) for item in self._extension_basis(preview.plan.identity)
        ] != basis
        payload["preview"]["extension_impacts"] = impacts
        return payload


__all__ = [
    "IntakeApiConflict",
    "IntakeApiError",
    "IntakeApiInvalid",
    "IntakeApiNotFound",
    "IntakeApiService",
    "IntakeWork",
]
