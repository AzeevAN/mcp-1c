import { AlertTriangle, CheckCircle2, Puzzle, RotateCw, Save, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  CapabilitiesApiError,
  useCapabilities,
  useSaveCapabilities,
} from "../shared/api/capabilities";
import { requestServerRestart, waitForServerRestart } from "../shared/api/sourceAdmin";

// Значения baseline живут в backend manifest; эти числа оставлены в комментарии
// для contract-проверки опубликованной формы и metadata authoring.
// forms: 6 141 (20.09.2026); metadata_authoring: 5 762 (20.09.2026);
// tokenizer: o200k_base.

function errorMessage(error: unknown): string {
  if (error instanceof CapabilitiesApiError || error instanceof Error) return error.message;
  return "Неизвестная ошибка управления модулями.";
}

function formatTokens(value: number | null): string {
  return value == null ? "Стоимость не измерена" : `≈ ${value.toLocaleString("ru-RU")} токенов`;
}

function activeContextCost(status: { active: string[]; modules: { id: string; approx_tokens: number | null }[] }) {
  const active = status.modules.filter((module) => status.active.includes(module.id));
  const measured = active.reduce((total, module) => total + (module.approx_tokens ?? 0), 0);
  const unmeasured = active.filter((module) => module.approx_tokens == null).length;
  return { measured, unmeasured };
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
  const contextCost = activeContextCost(status);
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
          <span className="eyebrow">Настройки сервера</span>
          <h1>Дополнительные модули</h1>
          <p>Управляйте составом MCP-инструментов и заранее видьте, сколько контекста займёт каждый модуль.</p>
        </div>
        <Puzzle size={30} aria-hidden="true" />
      </header>

      <div className="capabilities-status-grid" aria-label="Состояние модулей">
        <article>
          <span>Активно сейчас</span>
          <strong>{status.active.length ? `${status.active.length} модул${status.active.length === 1 ? "ь" : "я"}` : "Модули выключены"}</strong>
          <small>Загружено текущим процессом MCP.</small>
        </article>
        <article>
          <span>Контекст старта</span>
          <strong>{formatTokens(contextCost.measured)}</strong>
          <small>{contextCost.unmeasured ? `${contextCost.unmeasured} модул${contextCost.unmeasured === 1 ? "ь" : "я"} без замера` : "Сумма активных модулей"}</small>
        </article>
        <article className={status.pending_restart ? "is-pending" : "is-ready"}>
          <span>Сохранённый выбор</span>
          <strong>{status.desired.length ? `${status.desired.length} модул${status.desired.length === 1 ? "ь" : "я"}` : "Модули выключены"}</strong>
          <small>{status.pending_restart ? "Ожидает полного перезапуска" : "Совпадает с текущим процессом"}</small>
        </article>
      </div>

      <section className="capabilities-picker" aria-labelledby="capability-picker-title">
        <div className="capabilities-picker-copy">
          <span className="eyebrow">Закрытый каталог</span>
          <h2 id="capability-picker-title">Доступные модули</h2>
          <p>Встроенные возможности сервера. Каждый модуль показывает собственную стоимость контекста.</p>
        </div>
        <div className="capabilities-list">
          {status.modules.map((module) => {
            const name = module.id;
            const checked = draft.includes(name);
            const changed = checked !== module.active;
            const stateLabel = changed ? "Изменён" : checked ? "Включен" : "Выключен";
            return (
              <label className={`switch-field capability-switch ${checked ? "is-enabled" : "is-disabled"} ${changed ? "is-dirty" : ""}`} key={module.id}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(name)}
                  disabled={save.isPending || restarting}
                  aria-label={`${module.display_name} (${name})`}
                />
                <span className="switch-control" aria-hidden="true"><i /></span>
                <span className="capability-copy">
                  <span className="capability-card-head">
                    <strong>{module.display_name}</strong>
                    <b className={`capability-state is-${changed ? "pending" : checked ? "enabled" : "disabled"}`}>{stateLabel}</b>
                  </span>
                  <code>{name}</code>
                  <small className="capability-description">{module.description}</small>
                  <span className="capability-metrics">
                    <span><strong>{module.tool_count ?? "—"}</strong><small>инструментов</small></span>
                    <span><strong>{formatTokens(module.approx_tokens)}</strong><small>стоимость контекста</small></span>
                    <span><strong>{module.tokenizer ?? "—"}</strong><small>tokenizer</small></span>
                  </span>
                  <small className="capability-technical">
                    {module.approx_tokens == null ? "Стоимость не измерена" : `≈ ${module.approx_tokens.toLocaleString("ru-RU")} токенов · ${module.tool_count ?? "?"} инструментов · ${module.tokenizer ?? "unmeasured"} · ${module.measured_at ?? "дата не указана"}`}
                  </small>
                  <small className="capability-runtime">{`В текущем процессе: ${status.active.includes(name) ? "включён" : "выключен"}`}</small>
                  {module.measurement_command && <small className="capability-command"><code>{module.measurement_command}</code></small>}
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
