import { AlertTriangle, CheckCircle2, Puzzle, RotateCw, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  CapabilitiesApiError,
  useCapabilities,
  useSaveCapabilities,
} from "../shared/api/capabilities";
import { requestServerRestart, waitForServerRestart } from "../shared/api/sourceAdmin";

const moduleCopy: Record<string, { title: string; description: string; details?: string }> = {
  diagnostics: {
    title: "Диагностика сервера",
    description: "Добавляет административный инструмент проверки состояния capability-контура.",
  },
  forms: {
    title: "Управляемые формы",
    description: "Добавляет 4 инструмента: правила, компиляцию, декомпиляцию и статическую проверку. Файлы и конфигурацию 1С не изменяет.",
    details: "≈ 3 503 токена стартового контекста · o200k_base · замер 13.09.2026",
  },
};

function errorMessage(error: unknown): string {
  if (error instanceof CapabilitiesApiError || error instanceof Error) return error.message;
  return "Неизвестная ошибка управления модулями.";
}

export function CapabilitiesPage() {
  const query = useCapabilities();
  const save = useSaveCapabilities();
  const [draft, setDraft] = useState<string[]>([]);
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; text: string } | null>(null);
  const [confirmRestart, setConfirmRestart] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const draftInitialized = useRef(false);
  const restartTrigger = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLElement>(null);

  useEffect(() => {
    if (query.data && !draftInitialized.current) {
      setDraft(query.data.desired);
      draftInitialized.current = true;
    }
  }, [query.data]);

  useEffect(() => {
    if (confirmRestart) dialog.current?.focus();
  }, [confirmRestart]);

  if (query.isPending) {
    return (
      <section className="capabilities-page" aria-live="polite">
        <div className="section-card capabilities-loading">
          <span className="loading-dot" />Читаем настройки дополнительных модулей…
        </div>
      </section>
    );
  }

  if (query.isError || !query.data) {
    return (
      <section className="capabilities-page">
        <header className="capabilities-heading">
          <div><span>Настройки сервера</span><h1>Дополнительные модули</h1></div>
        </header>
        <div className="capabilities-error" role="alert">
          <AlertTriangle size={22} aria-hidden="true" />
          <div>
            <strong>Настройки недоступны</strong>
            <span>{errorMessage(query.error)}</span>
          </div>
          <button className="button-secondary" type="button" onClick={() => void query.refetch()}>
            Повторить
          </button>
        </div>
      </section>
    );
  }

  const status = query.data;
  const normalizedDraft = status.available.filter((name) => draft.includes(name));
  const dirty = JSON.stringify(normalizedDraft) !== JSON.stringify(status.desired);

  const toggle = (name: string) => {
    setFeedback(null);
    setDraft((current) => current.includes(name)
      ? current.filter((item) => item !== name)
      : status.available.filter((item) => item === name || current.includes(item)));
  };

  const saveDraft = async () => {
    setFeedback(null);
    try {
      const result = await save.mutateAsync(normalizedDraft);
      setDraft(result.desired);
      setFeedback({
        tone: "success",
        text: result.pending_restart
          ? "Изменение сохранено и ожидает перезапуска."
          : "Выбор сохранён; перезапуск не требуется.",
      });
    } catch (error) {
      setFeedback({ tone: "danger", text: errorMessage(error) });
    }
  };

  const closeRestart = () => {
    if (restarting) return;
    setConfirmRestart(false);
    restartTrigger.current?.focus();
  };

  const restart = async () => {
    setFeedback(null);
    setRestarting(true);
    try {
      const response = await requestServerRestart();
      await waitForServerRestart(response.runtime_id);
      window.location.assign("/login?next=%2Fcapabilities");
    } catch (error) {
      setRestarting(false);
      setConfirmRestart(false);
      setFeedback({ tone: "danger", text: errorMessage(error) });
      restartTrigger.current?.focus();
    }
  };

  return (
    <section className="capabilities-page">
      <header className="capabilities-heading">
        <div>
          <span>Настройки сервера</span>
          <h1>Дополнительные модули</h1>
          <p>Выберите внутренние модули, сохраните полный набор и примените его отдельным перезапуском.</p>
        </div>
        <Puzzle size={30} aria-hidden="true" />
      </header>

      <div className="capabilities-status-grid" aria-label="Состояние модулей">
        <article>
          <span>Активно сейчас</span>
          <strong>{status.active.length ? status.active.join(", ") : "Модули выключены"}</strong>
          <small>Каталог MCP tools текущего процесса не меняется после сохранения.</small>
        </article>
        <article className={status.pending_restart ? "is-pending" : "is-ready"}>
          <span>Сохранённый выбор</span>
          <strong>{status.desired.length ? status.desired.join(", ") : "Модули выключены"}</strong>
          <small>{status.pending_restart ? "Ожидает полного перезапуска" : "Совпадает с текущим процессом"}</small>
        </article>
      </div>

      <section className="capabilities-picker" aria-labelledby="capability-picker-title">
        <div className="capabilities-picker-copy">
          <span>Закрытый каталог</span>
          <h2 id="capability-picker-title">Доступные модули</h2>
          <p>Интерфейс принимает только модули, заранее объявленные сервером.</p>
        </div>
        <div className="capabilities-list">
          {status.available.map((name) => {
            const copy = moduleCopy[name] ?? { title: name, description: "Внутренний capability-модуль." };
            return (
              <label className="switch-field capability-switch" key={name}>
                <input
                  type="checkbox"
                  checked={draft.includes(name)}
                  onChange={() => toggle(name)}
                  disabled={save.isPending || restarting}
                  aria-label={`${copy.title} (${name})`}
                />
                <span className="switch-control" aria-hidden="true"><i /></span>
                <span>
                  <strong>{copy.title}</strong>
                  <small>{copy.description}</small>
                  {copy.details && <small>{copy.details}</small>}
                  <code>{name}</code>
                  <em>В текущем процессе: {status.active.includes(name) ? "включён" : "выключен"}</em>
                </span>
              </label>
            );
          })}
        </div>
        <div className="capabilities-actions">
          <span>{dirty ? "Выбор ещё не сохранён." : "Выбор сохранён."}</span>
          <button
            className="button-primary"
            type="button"
            disabled={!dirty || save.isPending || restarting}
            onClick={() => void saveDraft()}
          >
            <Save size={16} aria-hidden="true" />
            {save.isPending ? "Сохраняем…" : "Сохранить выбор"}
          </button>
        </div>
      </section>

      {feedback && (
        <div className={`admin-feedback is-${feedback.tone}`} role="status">
          {feedback.tone === "success" && <CheckCircle2 size={17} aria-hidden="true" />}
          {feedback.text}
        </div>
      )}

      {status.pending_restart && (
        <section className="capabilities-restart">
          <div>
            <span>Есть сохранённые изменения</span>
            <strong>Для применения нужен полный перезапуск сервера</strong>
            <small>Клиент MCP потребуется подключить заново после старта нового процесса.</small>
          </div>
          {status.runtime.self_restart ? (
            <button
              ref={restartTrigger}
              className="button-danger"
              type="button"
              disabled={dirty || save.isPending || restarting}
              onClick={() => setConfirmRestart(true)}
            >
              <RotateCw size={16} aria-hidden="true" />Перезапустить и применить
            </button>
          ) : (
            <div className="inline-warning">
              <AlertTriangle size={18} aria-hidden="true" />
              Перезапуск из дашборда выключен; изменение должен применить оператор сервера.
            </div>
          )}
        </section>
      )}

      {confirmRestart && (
        <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeRestart(); }}>
          <section
            ref={dialog}
            className="capabilities-restart-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="capabilities-restart-title"
            tabIndex={-1}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.stopPropagation();
                closeRestart();
              }
              if (event.key !== "Tab") return;
              const elements = [...(dialog.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
              const first = elements[0];
              const last = elements[elements.length - 1];
              if (!first) {
                event.preventDefault();
                return;
              }
              if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {
                event.preventDefault();
                last.focus();
              } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) {
                event.preventDefault();
                first.focus();
              }
            }}
          >
            <button className="modal-close" type="button" aria-label="Закрыть" disabled={restarting} onClick={closeRestart}>
              <X size={17} aria-hidden="true" />
            </button>
            <RotateCw size={26} aria-hidden="true" />
            <h2 id="capabilities-restart-title">Перезапустить сервер?</h2>
            <p>Текущий процесс завершится после ответа, а сохранённый набор модулей будет прочитан только при новом старте.</p>
            <div className="capabilities-dialog-actions">
              <button className="button-secondary" type="button" disabled={restarting} onClick={closeRestart}>Отмена</button>
              <button className="button-danger" type="button" disabled={restarting} onClick={() => void restart()}>
                {restarting ? "Ожидаем новый процесс…" : "Да, перезапустить"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
