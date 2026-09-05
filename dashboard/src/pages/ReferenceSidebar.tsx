import { AlertCircle, BookOpen, Check, ChevronRight, Copy, FileUp, LoaderCircle, RotateCw, Trash2, UploadCloud, X } from "lucide-react";
import { type DragEvent, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryClient } from "@tanstack/react-query";

import { useReferenceStatus } from "../shared/api/reference";
import { type AdminSourcesResponse, type ReferenceAdminState, requestServerRestart, uploadReference, useAdminSources, useRemoveReference, waitForServerRestart } from "../shared/api/sourceAdmin";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";

const stateLabels: Record<string, string> = {
  disabled: "выключена", missing: "не загружена", untrusted: "нет доверия",
  incompatible: "несовместима", corrupt: "повреждена", ready: "подключена",
  pending_restart: "ожидает перезапуска",
};

function tone(state: string): StatusTone {
  if (state === "ready") return "success";
  if (state === "pending_restart") return "warning";
  if (state === "missing" || state === "disabled") return "info";
  return "danger";
}

function formatBytes(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} КБ` : `${Math.round(bytes / 1024 / 1024)} МБ`;
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Операция завершилась ошибкой.";
}

export function ReferenceSidebar({ admin }: { admin: boolean }) {
  const administrative = useAdminSources(admin);
  const publicStatus = useReferenceStatus(!admin);
  const reference = admin ? administrative.data?.reference : publicStatus.data;
  const query = admin ? administrative : publicStatus;
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const shown = reference?.pending ?? reference?.active;
  const close = () => { setOpen(false); trigger.current?.focus(); };
  const empty = shown?.state === "missing" || shown?.state === "disabled";
  const label = query.isError ? "Статус недоступен" : !shown ? "Загружаем статус…" : stateLabels[shown.state] ?? shown.state;

  return (
    <section className="reference-sidebar" aria-label="Общая справка">
      <h2>Общая справка</h2>
      <button ref={trigger} className={`reference-sidebar-button is-${shown ? tone(shown.state) : "info"}`} type="button"
        aria-label={`Общая справка: ${label}`} aria-haspopup="dialog" onClick={() => setOpen(true)} disabled={!shown}>
        <BookOpen size={21} aria-hidden="true" />
        <span className="reference-sidebar-copy">
          <strong>{empty && admin && reference?.managed_upload ? "Загрузить справку" : shown?.items != null ? `${new Intl.NumberFormat("ru-RU").format(shown.items)} материалов` : "Для всех конфигураций"}</strong>
          <span className="reference-sidebar-state"><i aria-hidden="true" />{label}</span>
        </span>
        <ChevronRight size={17} aria-hidden="true" />
      </button>
      {query.isError && <button className="reference-retry" type="button" onClick={() => void query.refetch()}>Повторить</button>}
      {open && reference && <ReferenceDialog reference={reference} admin={admin} restartAvailable={administrative.data?.runtime?.self_restart ?? false} onClose={close} />}
    </section>
  );
}

function ReferenceDialog({ reference, admin, restartAvailable, onClose }: {
  reference: ReferenceAdminState; admin: boolean; restartAvailable: boolean; onClose: () => void;
}) {
  const shown = reference.pending ?? reference.active;
  const removeReference = useRemoveReference();
  const queryClient = useQueryClient();
  const dialog = useRef<HTMLElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [action, setAction] = useState<"remove" | "restart" | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [restarting, setRestarting] = useState(false);
  const [showUpload, setShowUpload] = useState(!reference.managed_file_present && !reference.pending);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const busy = uploading || restarting || removeReference.isPending;
  const canManage = admin && reference.managed_upload;

  useEffect(() => {
    const previousFocus = document.activeElement;
    const previous = document.body.style.overflow;
    const application = document.getElementById("root");
    const previousInert = application?.inert ?? false;
    document.body.style.overflow = "hidden";
    if (application) application.inert = true;
    return () => {
      document.body.style.overflow = previous;
      if (application) application.inert = previousInert;
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  // Подтверждение заменяет содержимое того же окна: фокус не уходит в фоновую страницу.
  useEffect(() => { dialog.current?.focus(); }, [action, showUpload]);

  const closeDialog = () => {
    if (busy) return;
    if (!action) { onClose(); return; }
    setAction(null);
    setConfirmation("");
    setCopyState("idle");
  };

  const copyExactName = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API недоступен");
      await navigator.clipboard.writeText("reference.mcp1cref");
      setCopyState("copied");
    } catch { setCopyState("failed"); }
  };

  const remove = async () => {
    setFeedback(null);
    try {
      const result = await removeReference.mutateAsync(confirmation);
      setAction(null);
      setConfirmation("");
      setShowUpload(false);
      setFile(null);
      setFeedback({ tone: "success", text: result.pending
        ? "Файл и расходный индекс удалены. Справочные инструменты исчезнут после перезапуска."
        : "Неактивированная база удалена; перезапуск не требуется." });
    } catch (error) { setFeedback({ tone: "danger", text: errorMessage(error) }); }
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

  const chooseFile = (next: File | null) => {
    if (busy) return;
    setFeedback(null);
    setFile(null);
    if (!next) return;
    if (!next.name.toLowerCase().endsWith(".mcp1cref")) {
      setFeedback({ tone: "danger", text: "Выберите подписанный файл .mcp1cref." });
    } else if (next.size > reference.limits.upload_bytes) {
      setFeedback({ tone: "danger", text: `Файл больше лимита ${formatBytes(reference.limits.upload_bytes)}.` });
    } else { setFile(next); }
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files.item(0));
  };

  const beginUpload = async () => {
    if (!file || busy || !canManage) return;
    setUploading(true);
    setUploadProgress(0);
    setFeedback(null);
    try {
      const result = await uploadReference(file, setUploadProgress);
      // Строка и окно получают один pending-снимок сразу после успешной проверки.
      queryClient.setQueryData(["reference", "status"], result.reference);
      queryClient.setQueryData<AdminSourcesResponse>(["sources", "admin"], (previous) =>
        previous ? { ...previous, reference: result.reference } : previous,
      );
      await queryClient.invalidateQueries({ queryKey: ["sources", "admin"] });
      setFile(null);
      setShowUpload(false);
      setFeedback({ tone: "success", text: "Справка проверена и сохранена. Изменение применится после перезапуска сервера." });
    } catch (error) { setFeedback({ tone: "danger", text: errorMessage(error) }); }
    finally { setUploading(false); }
  };

  return createPortal(
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDialog(); }}>
      <section ref={dialog} className="reference-dialog" role="dialog" aria-modal="true" aria-labelledby="reference-dialog-title" tabIndex={-1}
        onKeyDown={(event) => {
          if (event.key === "Escape") { event.stopPropagation(); closeDialog(); }
          if (event.key !== "Tab") return;
          const elements = [...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), summary, a[href]') ?? [])]
            .filter((element) => !element.hidden && !element.closest('details:not([open]) > :not(summary)'));
          const first = elements[0], last = elements[elements.length - 1];
          if (!first) { event.preventDefault(); return; }
          if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus(); }
          else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first.focus(); }
        }}>
        <button className="modal-close" type="button" onClick={closeDialog} aria-label="Закрыть" disabled={busy}><X size={17} aria-hidden="true" /></button>
        <span className="reference-dialog-icon"><BookOpen size={24} aria-hidden="true" /></span>
        <h2 id="reference-dialog-title">{action === "remove" ? "Удалить локальную общую базу?" : action === "restart" ? "Перезапустить сервер?" : "Общая справка"}</h2>
        {feedback && <div className={`admin-feedback is-${feedback.tone}`} role="status">{feedback.text}</div>}
        {action ? (
          <>
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
                  <button className="button-secondary" type="button" onClick={closeDialog} disabled={busy}>Отмена</button>
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
                  <button className="button-secondary" type="button" onClick={closeDialog} disabled={busy}>Отмена</button>
                  <button className="button-primary" type="button" onClick={() => void restart()}>
                    <RotateCw size={16} aria-hidden="true" />Перезапустить сервер
                  </button>
                </footer>
              </>
            )}
          </>
        ) : (
          <>
            <p className="reference-dialog-subtitle">Единая справка для всех конфигураций.</p>
            <StatusBadge tone={tone(shown.state)}>{stateLabels[shown.state] ?? shown.state}</StatusBadge>
            {reference.pending ? (
              <div className="inline-warning"><AlertCircle size={18} aria-hidden="true" /><span>{reference.pending.message}{reference.active.ready ? " До перезапуска работает предыдущий снимок справки." : " Справка пока недоступна для поиска."}</span></div>
            ) : !shown.ready && <p>{shown.message}</p>}
            <dl className="reference-dialog-facts">
              {shown.items != null && <div><dt>Материалы</dt><dd>{new Intl.NumberFormat("ru-RU").format(shown.items)}</dd></div>}
              {shown.signature && <div><dt>Подпись</dt><dd>{shown.signature === "ed25519" ? "Проверена" : shown.signature === "unsigned-experimental" ? "Без подписи · экспериментально" : shown.signature === "not-checked" ? "Не проверена" : shown.signature}</dd></div>}
            </dl>
            {canManage && (showUpload ? (
              <div className="reference-upload">
                <div className={`upload-dropzone${dragging ? " is-dragging" : file ? " has-file" : ""}`}
                  onDragEnter={(event) => { event.preventDefault(); if (!busy) setDragging(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragging(false)} onDrop={handleDrop}>
                  <UploadCloud size={26} aria-hidden="true" />
                  <span><strong>{file?.name ?? "Перетащите файл справки"}</strong><small>{file ? formatBytes(file.size) : `.mcp1cref · до ${formatBytes(reference.limits.upload_bytes)}`}</small></span>
                  <button className="button-secondary" type="button" onClick={() => fileInput.current?.click()} disabled={busy}>Выбрать файл</button>
                  <input ref={fileInput} type="file" accept=".mcp1cref" aria-label="Файл общей справки" hidden disabled={busy}
                    onChange={(event) => { chooseFile(event.target.files?.[0] ?? null); event.target.value = ""; }} />
                </div>
                <p>Подпись и содержимое проверяются до сохранения. Для применения понадобится перезапуск сервера.</p>
                <button className="button-primary" type="button" onClick={() => void beginUpload()} disabled={!file || busy}>
                  {uploading ? <LoaderCircle className="is-spinning" size={17} aria-hidden="true" /> : <FileUp size={17} aria-hidden="true" />}
                  {uploading ? `Передаём ${uploadProgress}%` : "Проверить и сохранить"}
                </button>
                {uploading && <div className="upload-progress" role="progressbar" aria-label="Загрузка справки" aria-valuemin={0} aria-valuemax={100} aria-valuenow={uploadProgress}><i style={{ width: `${uploadProgress}%` }} /></div>}
              </div>
            ) : (
              <button className={`${reference.pending ? "button-secondary" : "button-primary"} reference-upload-open`} type="button" disabled={busy} onClick={() => { setFeedback(null); setShowUpload(true); }}>
                <FileUp size={17} aria-hidden="true" />{reference.managed_file_present ? "Загрузить обновление" : "Загрузить справку"}
              </button>
            ))}
            {restarting && <div className="inline-warning" role="status"><LoaderCircle className="is-spinning" size={18} aria-hidden="true" />Сервер перезапускается. После запуска откроется страница входа.</div>}
            {canManage && reference.pending && restartAvailable && <div className="reference-actions"><button className="button-primary" type="button" disabled={busy} onClick={() => { setFeedback(null); setAction("restart"); }}><RotateCw size={16} aria-hidden="true" />Перезапустить и применить</button></div>}
            {admin && reference.pending && !restartAvailable && <div className="inline-warning"><AlertCircle size={18} aria-hidden="true" />Перезапуск из дашборда выключен; изменение должен применить оператор сервера.</div>}
            {admin && !reference.managed_upload && <p className="reference-dialog-subtitle">Загрузкой и удалением справки управляет оператор сервера. Изменения из дашборда выключены.</p>}
            {!admin && <p className="reference-dialog-subtitle">Только чтение. Загрузкой и удалением справки управляет администратор.</p>}
            {(shown.index_cache || shown.schema_version || (canManage && reference.managed_file_present)) && <details className="source-help reference-details"><summary>Дополнительные действия</summary><div>
              {shown.index_cache && <p>Индекс: {shown.index_cache}</p>}
              {shown.schema_version && <p>Версия формата: {shown.schema_version}</p>}
              {canManage && reference.managed_file_present && <button className="button-danger-quiet" type="button" disabled={busy} onClick={() => { setFeedback(null); setConfirmation(""); setCopyState("idle"); setAction("remove"); }}><Trash2 size={16} aria-hidden="true" />Удалить базу</button>}
            </div></details>}
          </>
        )}
      </section>
    </div>, document.body,
  );
}
