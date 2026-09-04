import {
  AlertCircle,
  CheckCircle2,
  FileArchive,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";
import { type ChangeEvent, type DragEvent, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  ConfigIntakeApiError,
  type IntakeAction,
  type IntakeCandidate,
  type IntakeJob,
  type IntakeLayerVersion,
  confirmConfigIntake,
  discardConfigIntake,
  startConfigIntake,
  uploadConfigCandidate,
  useConfigIntake,
  useIntakeJob,
} from "../shared/api/configIntake";
import { refreshSourceDependents } from "../shared/api/sourceFreshness";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";

const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;

const actionLabels: Record<IntakeAction, string> = {
  create: "Создать конфигурацию",
  update: "Обновить код, формы и роли",
  update_full: "Обновить полностью",
};

const layerLabels = {
  base_structure: "Базовая структура",
  extended_structure: "Расширенная структура",
  code: "Код",
  forms: "Формы",
  roles: "Роли",
};

const stageLabels: Record<string, string> = {
  accepted: "Кандидат принят",
  probing: "Проверяем источник",
  ready: "Ожидает разбора",
  collecting: "Собираем файлы",
  converting: "Строим каноническую модель",
  materializing: "Готовим поколение",
  planning: "Считаем semantic diff",
  done: "Preview готов",
  failed: "Разбор завершился ошибкой",
};

const transportLabels: Record<IntakeCandidate["transport"], string> = {
  browser: "Загружен из браузера",
  incoming: "Найден в incoming",
  "local-file": "Read-only ZIP",
  "local-directory": "Read-only каталог",
};

const layoutLabels: Record<IntakeCandidate["layout"], string> = {
  unknown: "раскладка не определена",
  flat: "плоская раскладка",
  tree: "дерево файлов",
  mixed: "смешанная раскладка",
};

const extensionImpactLabels = {
  resolved: "цель найдена",
  target_missing: "цель отсутствует в новой базе",
};

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} КБ`;
  return `${(value / 1024 / 1024).toFixed(1).replace(".0", "")} МБ`;
}

function message(error: unknown) {
  return error instanceof Error ? error.message : "Операция intake завершилась ошибкой.";
}

function stateTone(state: IntakeLayerVersion["state"]): StatusTone {
  if (state === "ready") return "success";
  if (state === "error") return "danger";
  return "info";
}

function CandidateRow({
  candidate,
  configurationNames,
  busy,
  onStart,
}: {
  candidate: IntakeCandidate;
  configurationNames: string[];
  busy: boolean;
  onStart: (candidate: IntakeCandidate, action: IntakeAction, parent: string) => void;
}) {
  const [parent, setParent] = useState("");
  const requiresFirstFullUpdate = candidate.source_kind === "configuration"
    && candidate.actions.length === 1
    && candidate.actions[0] === "update_full";
  return (
    <article className="intake-candidate">
      <span className="incoming-file-icon"><FileArchive size={20} aria-hidden="true" /></span>
      <span className="intake-candidate-copy">
        <strong>{candidate.origin_name}</strong>
        <small>
          {transportLabels[candidate.transport]}
          {candidate.configuration_version ? ` · версия ${candidate.configuration_version}` : ""}
          {` · ${layoutLabels[candidate.layout]}`}
        </small>
      </span>
      <div className="intake-candidate-actions">
        {candidate.requires_parent && (
          <label className="intake-parent-select">
            <span>Родительская конфигурация</span>
            <select
              aria-label="Родительская конфигурация"
              value={parent}
              disabled={busy || configurationNames.length === 0}
              onChange={(event) => setParent(event.target.value)}
            >
              <option value="">Выберите конфигурацию</option>
              {configurationNames.map((name) => (
                <option value={name} key={name}>{name}</option>
              ))}
            </select>
          </label>
        )}
        {candidate.actions.map((action) => (
          <button
            className={action === "update_full" || action === "create" ? "button-primary" : "button-secondary"}
            type="button"
            key={action}
            disabled={busy || (candidate.requires_parent && !parent)}
            onClick={() => onStart(candidate, action, parent)}
          >
            {actionLabels[action]}
          </button>
        ))}
        {candidate.requires_parent && configurationNames.length === 0 && (
          <small>Сначала загрузите родительскую конфигурацию.</small>
        )}
        {requiresFirstFullUpdate && (
          <small>
            Сначала выполните полное обновление: оно создаст единое поколение структуры, кода, форм и ролей.
          </small>
        )}
      </div>
    </article>
  );
}

function PreviewDialog({
  job,
  error,
  pending,
  discarding,
  onClose,
  onConfirm,
  onDiscard,
}: {
  job: IntakeJob | undefined;
  error: string;
  pending: boolean;
  discarding: boolean;
  onClose: () => void;
  onConfirm: () => void;
  onDiscard: () => void;
}) {
  const preview = job?.preview;
  const title = preview ? "Проверка изменений" : "Подготовка изменений";
  return (
    <div className="modal-backdrop" role="presentation">
      <section
        className="intake-preview-dialog"
        role="dialog"
        aria-modal="true"
        aria-busy={pending}
        aria-labelledby="intake-preview-title"
      >
        <button
          className="modal-close"
          type="button"
          onClick={onClose}
          aria-label="Закрыть"
          disabled={pending}
        >
          <X size={17} aria-hidden="true" />
        </button>
        <span className="removal-icon" aria-hidden="true">
          {preview ? <ShieldCheck size={22} /> : <LoaderCircle className="is-spinning" size={22} />}
        </span>
        <h2 id="intake-preview-title">{title}</h2>
        {error ? (
          <div className="admin-feedback is-danger" role="alert">{error}</div>
        ) : !job || (job.state !== "done" && job.state !== "failed") ? (
          <div className="intake-progress" role="status">
            <LoaderCircle className="is-spinning" size={18} aria-hidden="true" />
            <span>{stageLabels[job?.stage ?? "ready"] ?? job?.stage ?? "Запускаем разбор"}</span>
          </div>
        ) : job.state === "failed" ? (
          <div className="admin-feedback is-danger" role="alert">{job.error}</div>
        ) : preview ? (
          <>
            {pending ? (
              <div className="intake-progress" role="status">
                <LoaderCircle className="is-spinning" size={18} aria-hidden="true" />
                <span>
                  <strong>{discarding ? "Удаление preview уже запущено." : "Публикация уже запущена."}</strong>{" "}
                  Дождитесь ответа сервера: операция не отменяется закрытием или обновлением страницы.
                </span>
              </div>
            ) : (
              <>
                <p>
                  {preview.no_op
                    ? "Содержимое совпадает с активным поколением: повторная запись не нужна."
                    : "Публикация применит только перечисленные решения одним поколением."}
                </p>
                <p>
                  Preview уже сохранён на сервере. Закройте окно крестиком, чтобы вернуться к нему позже.
                </p>
              </>
            )}
            <div className="intake-diff" aria-label="Послойные изменения">
              {preview.layers.map((layer) => (
                <article className="intake-layer" key={layer.kind}>
                  <div>
                    <strong>{layerLabels[layer.kind]}</strong>
                    <small>{layer.decision === "apply" ? "применить" : "сохранить активный слой"}</small>
                  </div>
                  <span>
                    <StatusBadge tone={stateTone(layer.candidate.state)}>{layer.candidate.state}</StatusBadge>
                    <small>{layer.candidate.items_total} элементов</small>
                  </span>
                  {layer.candidate.error && <p>{layer.candidate.error}</p>}
                </article>
              ))}
            </div>
            {preview.extension_impacts && preview.extension_impacts.total > 0 && (
              <section className="intake-extension-impacts" aria-label="Влияние на расширения">
                <strong>Перепроверка расширений</strong>
                {preview.extension_impacts.items.map((item) => (
                  <p key={`${item.extension}:${item.target}`}>
                    <code>{item.extension}</code> · <code>{item.target}</code> · {extensionImpactLabels[item.state]}
                  </p>
                ))}
                {preview.extension_impacts.truncated && (
                  <small>Показаны первые {preview.extension_impacts.items.length} из {preview.extension_impacts.total} связей.</small>
                )}
              </section>
            )}
            <footer>
              <button className="button-danger-quiet" type="button" onClick={onDiscard} disabled={pending}>
                Отменить и удалить preview
              </button>
              <button className="button-primary" type="button" onClick={onConfirm} disabled={pending}>
                {pending && <LoaderCircle className="is-spinning" size={16} aria-hidden="true" />}
                {pending
                  ? preview.no_op ? "Завершаем без изменений…" : "Публикуем изменения…"
                  : preview.no_op ? "Завершить без изменений" : "Опубликовать изменения"}
              </button>
            </footer>
          </>
        ) : null}
      </section>
    </div>
  );
}

export function ConfigIntakePanel() {
  const intake = useConfigIntake();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [activeJobId, setActiveJobId] = useState("");
  const [starting, setStarting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const job = useIntakeJob(activeJobId);

  useEffect(() => {
    const state = job.data?.job.state;
    if (!activeJobId || (state !== "done" && state !== "failed")) return;
    void queryClient.invalidateQueries({
      queryKey: ["sources", "intake"],
      exact: true,
    });
  }, [activeJobId, job.data?.job.state, queryClient]);

  const refresh = async () => {
    setFeedback(null);
    await intake.refetch();
  };

  const chooseFile = (next: File | null) => {
    setFeedback(null);
    if (!next) {
      setFile(null);
      return;
    }
    if (!next.name.toLowerCase().endsWith(".zip")) {
      setFile(null);
      setFeedback({ tone: "danger", text: "Для полной файловой выгрузки нужен ZIP." });
      return;
    }
    if (next.size > MAX_UPLOAD_BYTES) {
      setFile(null);
      setFeedback({
        tone: "danger",
        text: "ZIP больше 500 МиБ: используйте data/incoming или read-only mount.",
      });
      return;
    }
    setFile(next);
  };

  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    chooseFile(event.target.files?.[0] ?? null);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files.item(0));
  };

  const upload = async () => {
    if (!file) return;
    setUploading(true);
    setUploadProgress(0);
    setFeedback(null);
    try {
      await uploadConfigCandidate(file, setUploadProgress);
      setFile(null);
      if (input.current) input.current.value = "";
      await queryClient.invalidateQueries({ queryKey: ["sources", "intake"] });
      setFeedback({
        tone: "success",
        text: "ZIP принят и сохранён. Теперь выберите найденный candidate и действие.",
      });
    } catch (error) {
      setFeedback({ tone: "danger", text: message(error) });
    } finally {
      setUploading(false);
    }
  };

  const start = async (
    candidate: IntakeCandidate,
    action: IntakeAction,
    parent: string,
  ) => {
    setStarting(true);
    setDialogError("");
    setFeedback(null);
    try {
      const result = await startConfigIntake(candidate.id, action, parent);
      setActiveJobId(result.job.job_id);
      queryClient.setQueryData(
        ["sources", "intake", "job", result.job.job_id],
        result,
      );
    } catch (error) {
      setFeedback({ tone: "danger", text: message(error) });
    } finally {
      setStarting(false);
    }
  };

  const confirm = async () => {
    if (!activeJobId) return;
    setConfirming(true);
    setDialogError("");
    try {
      const result = await confirmConfigIntake(activeJobId);
      queryClient.setQueryData(
        ["sources", "intake", "job", activeJobId],
        result,
      );
      await Promise.all([
        ...(result.job.commit?.no_op ? [] : [refreshSourceDependents(queryClient)]),
        queryClient.invalidateQueries({ queryKey: ["sources", "intake"], exact: true }),
      ]);
      setActiveJobId("");
      setFeedback({
        tone: "success",
        text: result.job.commit?.no_op
          ? "Изменений нет; активное поколение сохранено."
          : "Поколение опубликовано. MCP использует новый согласованный снимок.",
      });
    } catch (error) {
      setDialogError(message(error));
    } finally {
      setConfirming(false);
    }
  };

  const discard = async () => {
    if (!activeJobId) return;
    const discardedJobId = activeJobId;
    setDiscarding(true);
    setDialogError("");
    try {
      await discardConfigIntake(discardedJobId);
      setActiveJobId("");
      setDialogError("");
      await queryClient.invalidateQueries({
        queryKey: ["sources", "intake"],
        exact: true,
      });
      queryClient.removeQueries({
        queryKey: ["sources", "intake", "job", discardedJobId],
        exact: true,
      });
      setFeedback({
        tone: "success",
        text: "Preview отменён; его рабочие данные удалены.",
      });
    } catch (error) {
      setDialogError(message(error));
    } finally {
      setDiscarding(false);
    }
  };

  if (intake.isPending) {
    return <section className="admin-loading"><span className="loading-dot" />Проверяем кандидатов полной выгрузки…</section>;
  }
  if (intake.isError) {
    const text = intake.error instanceof ConfigIntakeApiError
      ? intake.error.message
      : "Intake API недоступен.";
    return <section className="admin-loading is-error"><AlertCircle size={20} />{text}</section>;
  }

  const candidates = new Map(intake.data.candidates.map((item) => [item.id, item]));
  const intakeBusy = starting || confirming || discarding;

  return (
    <section className="admin-card config-intake-card" aria-label="Полная файловая выгрузка">
      <header className="admin-card-heading is-spread">
        <span className="admin-card-icon"><ShieldCheck size={21} aria-hidden="true" /></span>
        <div>
          <h3>Полная файловая выгрузка</h3>
          <p>Один источник для структуры, кода, форм и ролей. Сначала preview, затем отдельная публикация.</p>
        </div>
        <button className="button-secondary" type="button" onClick={() => void refresh()} disabled={intake.isFetching || confirming || discarding}>
          <RefreshCw className={intake.isFetching ? "is-spinning" : ""} size={16} aria-hidden="true" />
          Проверить обновления
        </button>
      </header>

      {feedback && <div className={`admin-feedback is-${feedback.tone}`} role="status">{feedback.text}</div>}

      <div
        className={dragging ? "intake-upload is-dragging" : file ? "intake-upload has-file" : "intake-upload"}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <UploadCloud size={23} aria-hidden="true" />
        <span>
          <strong>{file ? file.name : "ZIP до 500 МиБ"}</strong>
          <small>{file ? formatBytes(file.size) : "Большие файлы оставьте в data/incoming/"}</small>
        </span>
        <button className="button-secondary" type="button" onClick={() => input.current?.click()} disabled={uploading || confirming || discarding}>
          Выбрать ZIP полной выгрузки
        </button>
        <input ref={input} type="file" accept=".zip" onChange={handleInput} hidden />
        <button className="button-primary" type="button" onClick={() => void upload()} disabled={!file || uploading || confirming || discarding}>
          {uploading && <LoaderCircle className="is-spinning" size={16} aria-hidden="true" />}
          {uploading ? `Принимаем ${uploadProgress}%` : "Принять ZIP"}
        </button>
      </div>

      {intake.data.issues.length > 0 && (
        <div className="intake-issues" aria-label="Ошибки кандидатов">
          {intake.data.issues.map((issue) => (
            <div className="admin-feedback is-danger" key={`${issue.source_id}:${issue.origin_name}`}>
              <strong>{issue.origin_name}</strong> · {issue.message}
            </div>
          ))}
        </div>
      )}

      {!intake.data.groups.length ? (
        <div className="admin-empty intake-empty">
          <FileArchive size={24} aria-hidden="true" />
          <span><strong>Кандидатов пока нет</strong><small>Загрузите ZIP, положите его в data/incoming/ или настройте read-only mount.</small></span>
        </div>
      ) : (
        <div className="intake-groups">
          {intake.data.groups.map((group) => (
            <section className="intake-group" key={`${group.source_kind}:${group.internal_name}`}>
              <header>
                <span>
                  <strong>{group.internal_name}</strong>
                  <small>{group.source_kind === "extension" ? "Расширение" : "Конфигурация"}</small>
                </span>
                <span className="count-pill">{group.candidate_ids.length} {group.candidate_ids.length === 1 ? "вариант" : "варианта"}</span>
              </header>
              <div>
                {group.candidate_ids.map((candidateId) => {
                  const candidate = candidates.get(candidateId);
                  return candidate ? (
                    <CandidateRow
                      candidate={candidate}
                      configurationNames={intake.data.configuration_names}
                      busy={intakeBusy}
                      key={candidate.id}
                      onStart={(item, action, parent) => void start(item, action, parent)}
                    />
                  ) : null;
                })}
              </div>
            </section>
          ))}
        </div>
      )}

      {intake.data.jobs.some((item) => item.state === "done" && !item.commit) && (
        <div className="intake-resumable">
          <strong>Готовые preview</strong>
          {intake.data.jobs
            .filter((item) => item.state === "done" && !item.commit)
            .map((item) => {
              const candidate = candidates.get(item.candidate_id);
              const identity = item.preview?.identity;
              const identityName = identity?.source_kind === "extension"
                ? `${identity.parent_configuration} / ${identity.extension_name}`
                : identity?.configuration_name;
              const label = candidate
                ? `${identityName || candidate.internal_name} · ${candidate.origin_name}`
                : identityName || item.candidate_id;
              return (
                <button className="button-secondary" type="button" key={item.job_id} disabled={confirming || discarding} onClick={() => { setDialogError(""); setActiveJobId(item.job_id); }}>
                  Открыть preview: {label}
                </button>
              );
            })}
        </div>
      )}

      {activeJobId && (
        <PreviewDialog
          job={job.data?.job}
          error={dialogError || (job.isError ? message(job.error) : "")}
          pending={confirming || discarding}
          discarding={discarding}
          onClose={() => {
            if (confirming) return;
            setActiveJobId("");
            setDialogError("");
          }}
          onConfirm={() => void confirm()}
          onDiscard={() => void discard()}
        />
      )}
    </section>
  );
}
