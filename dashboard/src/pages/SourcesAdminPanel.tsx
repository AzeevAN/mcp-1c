import {
  AlertCircle,
  CheckCircle2,
  CircleOff,
  FileUp,
  LoaderCircle,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { type DragEvent, type ChangeEvent, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  type AdminJob,
  SourceAdminApiError,
  uploadSource,
  useAdminSources,
  useClearJobs,
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
    if (suffix !== "zip" && suffix !== "hbk" && suffix !== "json") {
      setFile(null);
      setFeedback({ tone: "danger", text: "Выберите файл .zip, .hbk или .json. Общую справку загрузите через её блок в боковом меню." });
      return;
    }
    const limit = admin.data?.limits.upload_bytes;
    if (limit && next.size > limit) {
      setFile(null);
      setFeedback({ tone: "danger", text: `Файл больше лимита ${formatBytes(limit)}.` });
      return;
    }
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
      await uploadSource(file, allowTruncated, setUploadProgress);
      setFeedback({
        tone: "success",
        text: "Файл передан. Разбор продолжается в фоне и останется в журнале.",
      });
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
            <p>Schema v1, HBK или снимок активности — до {formatBytes(data.limits.upload_bytes)}.</p>
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
            <span><strong>Перетащите файл сюда</strong><small>.zip структуры · .hbk платформы · .json снимка</small></span>
          )}
          <button className="button-secondary" type="button" onClick={() => fileInput.current?.click()} disabled={uploading}>
            Выбрать файл
          </button>
          <input ref={fileInput} type="file" accept=".zip,.hbk,.json" onChange={handleInput} hidden />
        </div>

        <div className="upload-options">
          <label className="switch-field">
            <input type="checkbox" checked={allowTruncated} onChange={(event) => setAllowTruncated(event.target.checked)} />
            <span className="switch-control" aria-hidden="true"><i /></span>
            <span>
              <strong>Разрешить неполную тестовую выгрузку</strong>
              <small>Только для осознанной диагностики файла с <code>truncated=true</code>. Отсутствие объекта или связи в таком источнике ничего не доказывает.</small>
            </span>
          </label>
          <button className="button-primary" type="button" onClick={beginUpload} disabled={!file || uploading}>
            {uploading ? <LoaderCircle className="is-spinning" size={17} /> : <FileUp size={17} />}
            {uploading ? `Передаём ${uploadProgress}%` : "Загрузить и разобрать"}
          </button>
        </div>
        {uploading && <div className="upload-progress" aria-label={`Передано ${uploadProgress}%`}><i style={{ width: `${uploadProgress}%` }} /></div>}

        <details className="source-help">
          <summary>Какие файлы загружать</summary>
          <div>
            <p><strong>Структура конфигурации:</strong> архив <code>СтруктураКонфигурации_*.zip</code>, полученный обработкой проекта.</p>
            <p><strong>Активность расширений:</strong> файл <code>СнимокРасширений_*.json</code> из отдельной обработки снимка.</p>
            <p><strong>Справка платформы:</strong> точный файл <code>shcntx_ru.hbk</code>; другие похожие HBK его не заменяют.</p>
            <p><strong>Общая справка:</strong> загрузите подписанный <code>.mcp1cref</code> через блок «Общая справка» в боковом меню.</p>
            <p><strong>Полная файловая выгрузка:</strong> используйте отдельный двухфазный блок выше; ZIP больше 500 МиБ положите в <code>{data.incoming_dir}</code>.</p>
          </div>
        </details>
      </section>

      <JobList jobs={data.jobs} />

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
