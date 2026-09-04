import {
  AlertCircle,
  BookOpen,
  Check,
  CheckCircle2,
  CircleOff,
  Copy,
  FileUp,
  LoaderCircle,
  RotateCw,
  Trash2,
  UploadCloud,
  X,
} from "lucide-react";
import { type DragEvent, type ChangeEvent, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  type AdminJob,
  type ReferenceAdminState,
  SourceAdminApiError,
  uploadSource,
  uploadReference,
  requestServerRestart,
  waitForServerRestart,
  useAdminSources,
  useClearJobs,
  useRemoveReference,
} from "../shared/api/sourceAdmin";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";
import { ConfigIntakePanel } from "./ConfigIntakePanel";

function formatBytes(value: number) {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} КБ`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1).replace(".0", "")} МБ`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1).replace(".0", "")} ГБ`;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Операция завершилась ошибкой.";
}

function jobTone(job: AdminJob): StatusTone {
  if (job.state === "готово") return "success";
  if (job.state === "ошибка") return "danger";
  return "info";
}

function JobList({ jobs }: { jobs: AdminJob[] }) {
  const clear = useClearJobs();
  const hasCompleted = jobs.some((job) => job.state === "готово" || job.state === "ошибка");
  if (!jobs.length) return null;
  return (
    <section className="admin-subsection job-section" aria-label="Журнал загрузок">
      <header className="admin-subsection-heading">
        <div>
          <span className="eyebrow">Фоновые операции</span>
          <h3>Журнал загрузок</h3>
        </div>
        {hasCompleted && (
          <button
            className="button-secondary"
            type="button"
            onClick={() => clear.mutate()}
            disabled={clear.isPending}
          >
            <CircleOff size={16} aria-hidden="true" />Очистить завершённые
          </button>
        )}
      </header>
      <div className="job-list">
        {jobs.map((job, index) => {
          const running = job.state === "принимается" || job.state === "разбирается";
          return (
            <article className={`job-row is-${jobTone(job)}`} key={`${job.name}-${index}`}>
              <span className="job-icon" aria-hidden="true">
                {running ? <LoaderCircle size={18} /> : job.state === "готово" ? <CheckCircle2 size={18} /> : <AlertCircle size={18} />}
              </span>
              <span className="job-copy">
                <strong>{job.name}</strong>
                <small>{formatBytes(job.size)}{job.error ? ` · ${job.error}` : ""}</small>
                <span className={running ? "operation-progress is-running" : `operation-progress is-${jobTone(job)}`}>
                  <i />
                </span>
              </span>
              <StatusBadge tone={jobTone(job)}>{job.state}</StatusBadge>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function referenceTone(state: string): StatusTone {
  if (state === "ready") return "success";
  if (state === "pending_restart") return "warning";
  if (state === "missing" || state === "disabled") return "info";
  return "danger";
}

const referenceStateLabels: Record<string, string> = {
  disabled: "выключена",
  missing: "не загружена",
  untrusted: "нет доверия",
  incompatible: "несовместима",
  corrupt: "повреждена",
  ready: "подключена",
  pending_restart: "ожидает перезапуска",
};

function ReferenceAdminCard({
  reference,
  restartAvailable,
}: {
  reference: ReferenceAdminState;
  restartAvailable: boolean;
}) {
  const shown = reference.pending ?? reference.active;
  const removeReference = useRemoveReference();
  const [action, setAction] = useState<"remove" | "restart" | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [restarting, setRestarting] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; text: string } | null>(null);

  const closeDialog = () => {
    if (removeReference.isPending || restarting) return;
    setAction(null);
    setConfirmation("");
    setCopyState("idle");
  };

  const copyExactName = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API недоступен");
      await navigator.clipboard.writeText("reference.mcp1cref");
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  const remove = async () => {
    setFeedback(null);
    try {
      const result = await removeReference.mutateAsync(confirmation);
      setAction(null);
      setConfirmation("");
      setFeedback({
        tone: "success",
        text: result.pending
          ? "Файл и расходный индекс удалены. Справочные инструменты исчезнут после перезапуска."
          : "Неактивированная база удалена; перезапуск не требуется.",
      });
    } catch (error) {
      setFeedback({ tone: "danger", text: errorMessage(error) });
    }
  };

  const restart = async () => {
    setFeedback(null);
    setRestarting(true);
    setAction(null);
    try {
      const response = await requestServerRestart();
      await waitForServerRestart(response.runtime_id);
      window.location.assign("/login?next=%2Fsources");
    } catch (error) {
      setRestarting(false);
      setFeedback({ tone: "danger", text: errorMessage(error) });
    }
  };

  return (
    <section className="admin-card reference-admin-card" aria-label="Локальная общая справка">
      <header className="admin-card-heading is-spread">
        <span className="admin-card-icon"><BookOpen size={21} aria-hidden="true" /></span>
        <div>
          <h3>Локальная общая справка</h3>
          <p>Опциональный подписанный артефакт добавляет <code>search_reference</code> и <code>get_reference</code> только после проверки и перезапуска.</p>
        </div>
        <StatusBadge tone={referenceTone(shown.state)}>{referenceStateLabels[shown.state] ?? shown.state}</StatusBadge>
      </header>

      <div className="reference-status-copy">
        <strong>{shown.message}</strong>
        <small>
          {shown.items ? `Лимит ${formatBytes(reference.limits.upload_bytes)} · ${shown.items} элементов` : `Лимит ${formatBytes(reference.limits.upload_bytes)}`}
          {shown.index_cache ? ` · индекс ${shown.index_cache}` : ""}
          {shown.signature ? ` · ${shown.signature}` : ""}
          {reference.managed_upload ? " · загрузка через общую форму выше" : ""}
        </small>
      </div>
      {feedback && <div className={`admin-feedback is-${feedback.tone}`} role="status">{feedback.text}</div>}
      {restarting && (
        <div className="inline-warning" role="status">
          <LoaderCircle className="is-spinning" size={18} aria-hidden="true" />
          <span>Сервер перезапускается. Страница входа откроется после нового <code>runtime_id</code>.</span>
        </div>
      )}
      {reference.managed_upload && (
        <div className="reference-actions">
          {reference.managed_file_present && (
            <button
              className="button-danger-quiet"
              type="button"
              onClick={() => { setFeedback(null); setCopyState("idle"); setAction("remove"); }}
              disabled={restarting || removeReference.isPending}
            >
              <Trash2 size={16} aria-hidden="true" />Удалить базу
            </button>
          )}
          {reference.pending && restartAvailable && (
            <button
              className="button-primary"
              type="button"
              onClick={() => { setFeedback(null); setAction("restart"); }}
              disabled={restarting || removeReference.isPending}
            >
              <RotateCw size={16} aria-hidden="true" />Перезапустить и применить
            </button>
          )}
        </div>
      )}
      {reference.pending && !restartAvailable && (
        <div className="inline-warning">
          <AlertCircle size={18} aria-hidden="true" />
          <span>Перезапуск из дашборда выключен; изменение должен применить оператор сервера.</span>
        </div>
      )}
      {!reference.managed_upload && (
        <div className="inline-warning">
          <AlertCircle size={18} aria-hidden="true" />
          <span>Артефакт подключён через внешний <code>MCP1C_REFERENCE_ARTIFACT</code>; загрузка из дашборда выключена.</span>
        </div>
      )}

      {action && (
        <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeDialog();
        }}>
          <section
            className={action === "remove" ? "removal-dialog" : "removal-dialog restart-dialog"}
            role="dialog"
            aria-modal="true"
            aria-labelledby="reference-action-title"
          >
            <button className="modal-close" type="button" onClick={closeDialog} aria-label="Закрыть">
              <X size={17} aria-hidden="true" />
            </button>
            <span className="removal-icon" aria-hidden="true">
              {action === "remove" ? <Trash2 size={22} /> : <RotateCw size={22} />}
            </span>
            <h2 id="reference-action-title">
              {action === "remove" ? "Удалить локальную общую базу?" : "Перезапустить сервер?"}
            </h2>
            {action === "remove" ? (
              <>
                <p>Файл и расходный индекс будут удалены. Если инструменты уже активны, текущий снимок продолжит отвечать только до перезапуска, после которого <code>search_reference</code> и <code>get_reference</code> исчезнут.</p>
                <div className="confirmation-field">
                  <label htmlFor="reference-remove-confirmation">Для подтверждения введите точное имя:</label>
                  <div className="confirmation-name">
                    <code title="reference.mcp1cref">reference.mcp1cref</code>
                    <button
                      className="button-secondary confirmation-copy-button"
                      type="button"
                      onClick={() => void copyExactName()}
                      aria-label={copyState === "copied" ? "Точное имя скопировано" : "Скопировать точное имя"}
                    >
                      {copyState === "copied" ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
                      {copyState === "copied" ? "Скопировано" : "Копировать"}
                    </button>
                  </div>
                  <input
                    id="reference-remove-confirmation"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    autoComplete="off"
                  />
                  {copyState === "failed" && (
                    <span className="confirmation-copy-error" role="status">
                      Не удалось скопировать автоматически — имя можно выделить вручную.
                    </span>
                  )}
                </div>
                <footer>
                  <button className="button-secondary" type="button" onClick={closeDialog}>Отмена</button>
                  <button
                    className="button-danger"
                    type="button"
                    onClick={() => void remove()}
                    disabled={confirmation !== "reference.mcp1cref" || removeReference.isPending}
                  >
                    {removeReference.isPending ? <LoaderCircle className="is-spinning" size={16} /> : <Trash2 size={16} />}
                    Удалить базу
                  </button>
                </footer>
              </>
            ) : (
              <>
                <p>Текущие MCP-сеансы будут разорваны, а сессия дашборда исчезнет. После восстановления сервера потребуется повторный вход.</p>
                <footer>
                  <button className="button-secondary" type="button" onClick={closeDialog}>Отмена</button>
                  <button className="button-primary" type="button" onClick={() => void restart()}>
                    <RotateCw size={16} aria-hidden="true" />Перезапустить сервер
                  </button>
                </footer>
              </>
            )}
          </section>
        </div>
      )}
    </section>
  );
}

export function SourcesAdminPanel({
  onRequestForget,
}: {
  onRequestForget: (path: string) => void;
}) {
  const admin = useAdminSources(true);
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [allowTruncated, setAllowTruncated] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; text: string } | null>(null);

  const chooseFile = (next: File | null) => {
    setFeedback(null);
    if (!next) {
      setFile(null);
      return;
    }
    const suffix = next.name.toLowerCase().split(".").pop();
    if (suffix !== "zip" && suffix !== "hbk" && suffix !== "json" && suffix !== "mcp1cref") {
      setFile(null);
      setFeedback({ tone: "danger", text: "Выберите файл .zip, .hbk, .json или .mcp1cref." });
      return;
    }
    const isReference = suffix === "mcp1cref";
    const reference = admin.data?.reference;
    if (isReference && !reference?.managed_upload) {
      setFile(null);
      setFeedback({
        tone: "danger",
        text: "Артефакт общей справки подключён через внешний путь; его загрузкой управляет оператор сервера.",
      });
      return;
    }
    const limit = isReference
      ? reference?.limits.upload_bytes
      : admin.data?.limits.upload_bytes;
    if (limit && next.size > limit) {
      setFile(null);
      setFeedback({ tone: "danger", text: `Файл больше лимита ${formatBytes(limit)}.` });
      return;
    }
    if (isReference) setAllowTruncated(false);
    setFile(next);
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files.item(0));
  };

  const handleInput = (event: ChangeEvent<HTMLInputElement>) => {
    chooseFile(event.target.files?.[0] ?? null);
  };

  const beginUpload = async () => {
    if (!file) return;
    setUploading(true);
    setUploadProgress(0);
    setFeedback(null);
    try {
      const isReference = file.name.toLowerCase().endsWith(".mcp1cref");
      if (isReference) {
        await uploadReference(file, setUploadProgress);
        setFeedback({
          tone: "success",
          text: "База проверена и сохранена. Для появления двух MCP-инструментов перезапустите сервер.",
        });
      } else {
        await uploadSource(file, allowTruncated, setUploadProgress);
        setFeedback({
          tone: "success",
          text: "Файл передан. Разбор продолжается в фоне и останется в журнале.",
        });
      }
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["sources"] }),
        queryClient.invalidateQueries({ queryKey: ["dashboard", "bootstrap"] }),
        queryClient.invalidateQueries({ queryKey: ["sources", "admin"] }),
      ]);
    } catch (error) {
      setFeedback({ tone: "danger", text: errorMessage(error) });
    } finally {
      setUploading(false);
    }
  };

  if (admin.isPending) {
    return <section className="admin-loading"><span className="loading-dot" />Загружаем административные действия…</section>;
  }
  if (admin.isError) {
    const message = admin.error instanceof SourceAdminApiError ? admin.error.message : "Административный API недоступен.";
    return <section className="admin-loading is-error"><AlertCircle size={20} />{message}</section>;
  }

  const data = admin.data;
  const referenceFile = file?.name.toLowerCase().endsWith(".mcp1cref") ?? false;

  return (
    <section className="source-admin" aria-label="Администрирование источников">
      {feedback && <div className={`admin-feedback is-${feedback.tone}`} role="status">{feedback.text}</div>}
      {data.snapshot_error && <div className="admin-feedback is-danger">{data.snapshot_error}</div>}

      <ConfigIntakePanel />

      <section className="admin-card upload-card">
        <header className="admin-card-heading">
          <span className="admin-card-icon"><FileUp size={21} aria-hidden="true" /></span>
          <div>
            <h3>Базовая структура и справки</h3>
            <p>Schema v1, HBK, снимок активности или общая справка — до {formatBytes(data.limits.upload_bytes)}.</p>
          </div>
        </header>

        <div
          className={dragging ? "upload-dropzone is-dragging" : file ? "upload-dropzone has-file" : "upload-dropzone"}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
        >
          <UploadCloud size={28} aria-hidden="true" />
          {file ? (
            <span><strong>{file.name}</strong><small>{formatBytes(file.size)}</small></span>
          ) : (
            <span><strong>Перетащите файл сюда</strong><small>.zip структуры · .hbk платформы · .json снимка · .mcp1cref общей справки</small></span>
          )}
          <button className="button-secondary" type="button" onClick={() => fileInput.current?.click()} disabled={uploading}>
            Выбрать файл
          </button>
          <input ref={fileInput} type="file" accept=".zip,.hbk,.json,.mcp1cref" onChange={handleInput} hidden />
        </div>

        <div className="upload-options">
          {referenceFile ? (
            <div className="inline-warning">
              <BookOpen size={18} aria-hidden="true" />
              <span>Подпись, manifest и SQLite будут полностью проверены до сохранения для активации после перезапуска.</span>
            </div>
          ) : (
            <label className="switch-field">
              <input type="checkbox" checked={allowTruncated} onChange={(event) => setAllowTruncated(event.target.checked)} />
              <span className="switch-control" aria-hidden="true"><i /></span>
              <span>
                <strong>Разрешить неполную тестовую выгрузку</strong>
                <small>Только для осознанной диагностики файла с <code>truncated=true</code>. Отсутствие объекта или связи в таком источнике ничего не доказывает.</small>
              </span>
            </label>
          )}
          <button className="button-primary" type="button" onClick={beginUpload} disabled={!file || uploading}>
            {uploading ? <LoaderCircle className="is-spinning" size={17} /> : <FileUp size={17} />}
            {uploading ? `Передаём ${uploadProgress}%` : referenceFile ? "Проверить и сохранить" : "Загрузить и разобрать"}
          </button>
        </div>
        {uploading && <div className="upload-progress" aria-label={`Передано ${uploadProgress}%`}><i style={{ width: `${uploadProgress}%` }} /></div>}

        <details className="source-help">
          <summary>Какие файлы загружать</summary>
          <div>
            <p><strong>Структура конфигурации:</strong> архив <code>СтруктураКонфигурации_*.zip</code>, полученный обработкой проекта.</p>
            <p><strong>Активность расширений:</strong> файл <code>СнимокРасширений_*.json</code> из отдельной обработки снимка.</p>
            <p><strong>Справка платформы:</strong> точный файл <code>shcntx_ru.hbk</code>; другие похожие HBK его не заменяют.</p>
            <p><strong>Общая справка:</strong> подписанный <code>.mcp1cref</code> с канонической SQLite schema v1; после проверки потребуется перезапуск сервера.</p>
            <p><strong>Полная файловая выгрузка:</strong> используйте отдельный двухфазный блок выше; ZIP больше 500 МиБ положите в <code>{data.incoming_dir}</code>.</p>
          </div>
        </details>
      </section>

      <JobList jobs={data.jobs} />

      {data.reference && (
        <ReferenceAdminCard
          reference={data.reference}
          restartAvailable={data.runtime?.self_restart ?? false}
        />
      )}

      {data.orphans.length > 0 && (
        <section className="admin-card orphan-card">
          <header className="admin-card-heading">
            <span className="admin-card-icon is-warning"><Trash2 size={21} aria-hidden="true" /></span>
            <div>
              <h3>Исходные файлы вне реестра</h3>
              <p>Индексы уже построены, но исходник может понадобиться для повторного разбора.</p>
            </div>
          </header>
          <div className="orphan-list">
            {data.orphans.map((orphan) => (
              <div className="orphan-row" key={orphan.path}>
                <span><strong>{orphan.path}</strong><small>{formatBytes(orphan.size)}</small></span>
                <button className="button-danger-quiet" type="button" onClick={() => onRequestForget(orphan.path)}>
                  <Trash2 size={15} />Удалить файл
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </section>
  );
}
