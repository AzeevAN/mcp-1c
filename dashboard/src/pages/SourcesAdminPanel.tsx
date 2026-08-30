import {
  AlertCircle,
  Archive,
  CheckCircle2,
  CircleOff,
  FileArchive,
  FileUp,
  FolderInput,
  LoaderCircle,
  RotateCw,
  Trash2,
  UploadCloud,
} from "lucide-react";
import { type DragEvent, type ChangeEvent, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  type AdminJob,
  SourceAdminApiError,
  uploadSource,
  useAdminSources,
  useClearJobs,
  useParseIncoming,
} from "../shared/api/sourceAdmin";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";

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

function incomingTone(state: string): StatusTone {
  if (state === "разобрано") return "success";
  if (state === "разбор не удался") return "danger";
  if (state === "обновлённая выгрузка" || state === "отбор устарел") return "warning";
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
  const parseIncoming = useParseIncoming();
  const fileInput = useRef<HTMLInputElement>(null);
  const refreshedTerminalJobs = useRef("");
  const [file, setFile] = useState<File | null>(null);
  const [allowTruncated, setAllowTruncated] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [configurationByFile, setConfigurationByFile] = useState<Record<string, string>>({});
  const [activeIncoming, setActiveIncoming] = useState("");
  const terminalJobsSignature = (admin.data?.jobs ?? [])
    .filter((job) => job.state === "готово" || job.state === "ошибка")
    .map((job, index) => `${index}\u0000${job.name}\u0000${job.size}\u0000${job.state}\u0000${job.error}`)
    .join("\u0001");

  useEffect(() => {
    if (!terminalJobsSignature) {
      refreshedTerminalJobs.current = "";
      return;
    }
    if (refreshedTerminalJobs.current === terminalJobsSignature) return;
    refreshedTerminalJobs.current = terminalJobsSignature;
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["sources"], exact: true }),
      queryClient.invalidateQueries({ queryKey: ["dashboard", "bootstrap"] }),
    ]);
  }, [queryClient, terminalJobsSignature]);

  const chooseFile = (next: File | null) => {
    setFeedback(null);
    if (!next) {
      setFile(null);
      return;
    }
    const suffix = next.name.toLowerCase().split(".").pop();
    if (suffix !== "zip" && suffix !== "hbk" && suffix !== "json") {
      setFile(null);
      setFeedback({ tone: "danger", text: "Выберите файл .zip, .hbk или .json." });
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
      ]);
    } catch (error) {
      setFeedback({ tone: "danger", text: errorMessage(error) });
    } finally {
      setUploading(false);
    }
  };

  const parse = async (name: string) => {
    const names = admin.data?.configuration_names ?? [];
    const configuration = configurationByFile[name] || (names.length === 1 ? names[0] : "");
    setActiveIncoming(name);
    setFeedback(null);
    try {
      await parseIncoming.mutateAsync({ name, configuration });
      setFeedback({ tone: "success", text: `Разбор «${name}» запущен.` });
    } catch (error) {
      setFeedback({ tone: "danger", text: errorMessage(error) });
    } finally {
      setActiveIncoming("");
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
  const configurationNames = data.configuration_names;

  return (
    <section className="source-admin" aria-label="Администрирование источников">
      <header className="source-admin-heading">
        <div>
          <span className="eyebrow">Доступ администратора</span>
          <h2>Добавление и обслуживание данных</h2>
          <p>Операции выполняет тот же сервер и тот же Registry, которым пользуются MCP-инструменты.</p>
        </div>
        <StatusBadge tone="info">Запись разрешена</StatusBadge>
      </header>

      {feedback && <div className={`admin-feedback is-${feedback.tone}`} role="status">{feedback.text}</div>}
      {data.snapshot_error && <div className="admin-feedback is-danger">{data.snapshot_error}</div>}

      <section className="admin-card upload-card">
        <header className="admin-card-heading">
          <span className="admin-card-icon"><FileUp size={21} aria-hidden="true" /></span>
          <div>
            <h3>Загрузить источник</h3>
            <p>Один файл до {formatBytes(data.limits.upload_bytes)}. Тип определяется по содержимому.</p>
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
            <span><strong>Перетащите файл сюда</strong><small>.zip структуры · .hbk справки · .json снимка расширений</small></span>
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
            <p><strong>Большая выгрузка модулей и расширений:</strong> положите ZIP в <code>{data.incoming_dir}</code> и запустите разбор в следующем блоке.</p>
          </div>
        </details>
      </section>

      <JobList jobs={data.jobs} />

      <section className="admin-card incoming-card">
        <header className="admin-card-heading is-spread">
          <span className="admin-card-icon"><FolderInput size={21} aria-hidden="true" /></span>
          <div>
            <h3>Входящие выгрузки</h3>
            <p>Большие ZIP из <code>{data.incoming_dir}</code>. Сканируется только сам каталог, без вложенных папок.</p>
          </div>
          <span className="count-pill">{data.incoming.length} {data.incoming.length === 1 ? "файл" : "файлов"}</span>
        </header>

        {!data.incoming_exists ? (
          <div className="admin-empty"><Archive size={24} /><span><strong>Каталог ещё не создан</strong><small>Создайте или смонтируйте <code>{data.incoming_dir}</code>.</small></span></div>
        ) : data.incoming.length === 0 ? (
          <div className="admin-empty"><Archive size={24} /><span><strong>Входящих файлов нет</strong><small>ZIP появится здесь после копирования в <code>{data.incoming_dir}</code>.</small></span></div>
        ) : (
          <div className="incoming-list">
            {data.incoming.map((item) => {
              const selectedConfiguration = configurationByFile[item.name] || (configurationNames.length === 1 ? configurationNames[0] : "");
              const needsChoice = configurationNames.length > 1 && !selectedConfiguration;
              return (
                <article className="incoming-row" key={item.name}>
                  <span className="incoming-file-icon"><FileArchive size={20} aria-hidden="true" /></span>
                  <span className="incoming-file-copy">
                    <strong>{item.name}</strong>
                    <small>{formatBytes(item.size)}{item.detail ? ` · ${item.detail}` : ""}</small>
                  </span>
                  <StatusBadge tone={incomingTone(item.state)}>{item.state}</StatusBadge>
                  <div className="incoming-action">
                    {configurationNames.length > 1 && item.can_parse && (
                      <label>
                        <span>Родительская конфигурация</span>
                        <select
                          value={selectedConfiguration}
                          onChange={(event) => setConfigurationByFile((current) => ({ ...current, [item.name]: event.target.value }))}
                        >
                          <option value="">Выберите конфигурацию</option>
                          {configurationNames.map((name) => <option key={name}>{name}</option>)}
                        </select>
                      </label>
                    )}
                    {configurationNames.length === 1 && item.can_parse && <small>Будет привязано к «{configurationNames[0]}»</small>}
                    <button
                      className="button-secondary"
                      type="button"
                      disabled={!item.can_parse || needsChoice || activeIncoming === item.name}
                      onClick={() => parse(item.name)}
                    >
                      {activeIncoming === item.name ? <LoaderCircle className="is-spinning" size={16} /> : item.action === "reparse" ? <RotateCw size={16} /> : <Archive size={16} />}
                      {item.action === "reparse" ? "Переразобрать" : "Разобрать"}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
        {!configurationNames.length && data.incoming.length > 0 && (
          <div className="admin-callout"><AlertCircle size={18} /><span>Сначала загрузите структуру конфигурации: без неё серверу не к чему привязать код, модули форм и расширения.</span></div>
        )}
        <div className="admin-callout is-info"><AlertCircle size={18} /><span>Человек выбирает только родительскую конфигурацию. Основной код или расширение сервер определяет из содержимого выгрузки, а не из имени ZIP.</span></div>
      </section>

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
