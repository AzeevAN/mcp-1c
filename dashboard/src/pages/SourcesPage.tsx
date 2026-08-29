import {
  AlertTriangle,
  Boxes,
  Braces,
  Check,
  ChevronDown,
  Copy,
  FileJson,
  Layers3,
  PackageCheck,
  Puzzle,
  ServerCrash,
  Trash2,
  X,
} from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useForgetSource, useRemoveSource } from "../shared/api/sourceAdmin";
import {
  type CodeCorpus,
  type ConfigurationSource,
  type Coverage,
  type SourceItem,
  useSources,
} from "../shared/api/sources";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";
import { SourcesAdminPanel } from "./SourcesAdminPanel";

const sourceKindLabel: Record<SourceItem["kind"], string> = {
  configuration: "Конфигурация",
  modules: "Основной код",
  extension: "Расширение",
  "extension-runtime": "Снимок активности расширений",
  syntax: "Справка платформы",
  query: "Язык запросов",
};

const phaseView: Record<CodeCorpus["phase"], { label: string; tone: StatusTone }> = {
  ready: { label: "Готово", tone: "success" },
  limited: { label: "С ограничениями", tone: "warning" },
  building: { label: "Строится", tone: "info" },
  error: { label: "Ошибка", tone: "danger" },
  missing: { label: "Не загружен", tone: "info" },
};

function formatNumber(value: number) {
  return new Intl.NumberFormat("ru-RU").format(value);
}

function formatDate(value: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function percent(value: number, total: number) {
  return total === 0 ? "—" : `${((value * 100) / total).toFixed(1).replace(".", ",")}%`;
}

type CoverageRow = { label: string; value: number; total: number };

function CoverageTable({ title, rows }: { title: string; rows: CoverageRow[] }) {
  return (
    <section className="coverage-table" aria-label={title}>
      <h4>{title}</h4>
      <div className="coverage-table-head" aria-hidden="true">
        <span>Состояние</span>
        <span>Количество</span>
        <span>Доля</span>
      </div>
      {rows.map((row) => (
        <div className="coverage-row" key={row.label}>
          <span>{row.label}</span>
          <strong>{formatNumber(row.value)} из {formatNumber(row.total)}</strong>
          <span>{percent(row.value, row.total)}</span>
        </div>
      ))}
    </section>
  );
}

function CoverageTables({ coverage }: { coverage: Coverage }) {
  return (
    <div className="coverage-stack">
      <CoverageTable
        title="Модули и процедуры"
        rows={[
          { label: "Исходник доступен", value: coverage.modules.source_available, total: coverage.modules.total },
          { label: "Модуль прочитан частично", value: coverage.modules.partial, total: coverage.modules.total },
          { label: "Модуль не прочитан", value: coverage.modules.unreadable, total: coverage.modules.total },
          { label: "Процедуры разобраны", value: coverage.procedures.full, total: coverage.procedures.total },
          { label: "Процедуры частично", value: coverage.procedures.partial, total: coverage.procedures.total },
        ]}
      />
      <CoverageTable
        title="Структуры форм"
        rows={[
          { label: "Полностью разобраны", value: coverage.form_structures.full, total: coverage.form_structures.total },
          { label: "Частично разобраны", value: coverage.form_structures.partial, total: coverage.form_structures.total },
          { label: "Недоступны", value: coverage.form_structures.unreadable, total: coverage.form_structures.total },
        ]}
      />
      <CoverageTable
        title="Модули форм"
        rows={[
          { label: "Модуль прочитан", value: coverage.form_modules.read, total: coverage.form_modules.total },
          { label: "Модуль пуст", value: coverage.form_modules.empty, total: coverage.form_modules.total },
          { label: "Модуль отсутствует", value: coverage.form_modules.missing, total: coverage.form_modules.total },
          { label: "Модуль не прочитан", value: coverage.form_modules.unreadable, total: coverage.form_modules.total },
        ]}
      />
    </div>
  );
}

function ConfigurationNavItem({
  configuration,
  selected,
  onSelect,
}: {
  configuration: ConfigurationSource;
  selected: boolean;
  onSelect: () => void;
}) {
  const limited = configuration.corpora.some((corpus) => corpus.phase === "limited");
  const failed = configuration.corpora.some((corpus) => corpus.phase === "error");
  return (
    <button
      type="button"
      className={selected ? "configuration-nav-item is-selected" : "configuration-nav-item"}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="configuration-nav-icon" aria-hidden="true"><Boxes size={18} /></span>
      <span>
        <strong>{configuration.id}</strong>
        <small>{formatNumber(configuration.objects)} объектов · {configuration.corpora.length} корп.</small>
      </span>
      <i className={failed ? "is-danger" : limited ? "is-warning" : "is-success"} aria-hidden="true" />
    </button>
  );
}

type RemovalTarget = {
  operation: "source" | "orphan";
  id: string;
  title: string;
  impact: string;
};

function ReferenceRow({
  source,
  onRemove,
}: {
  source: SourceItem;
  onRemove?: (target: RemovalTarget) => void;
}) {
  return (
    <div className="reference-row">
      <Braces size={17} aria-hidden="true" />
      <span>
        <strong>{sourceKindLabel[source.kind]}</strong>
        <small>{source.platform || formatNumber(source.items_total) + " элементов"}</small>
      </span>
      <i className="is-success" aria-hidden="true" />
      {onRemove && (
        <button
          className="icon-danger-button"
          type="button"
          aria-label={`Удалить ${sourceKindLabel[source.kind]}`}
          onClick={() => onRemove({
            operation: "source",
            id: source.id,
            title: sourceKindLabel[source.kind],
            impact: "Источник и его разобранный индекс будут сняты с учёта. Остальные конфигурации и корпуса не изменятся.",
          })}
        >
          <Trash2 size={15} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

function CorpusCard({
  corpus,
  onRemove,
}: {
  corpus: CodeCorpus;
  onRemove?: (target: RemovalTarget) => void;
}) {
  const phase = phaseView[corpus.phase];
  const Icon = corpus.kind === "extension" ? Puzzle : Layers3;
  const [expanded, setExpanded] = useState(corpus.kind === "modules");
  return (
    <article className={`corpus-card is-${corpus.phase}`}>
      <header>
        <div className="corpus-title">
          <span className="corpus-icon" aria-hidden="true"><Icon size={20} /></span>
          <div>
            <span className="eyebrow">{sourceKindLabel[corpus.kind]}</span>
            <h3>{corpus.label}</h3>
          </div>
        </div>
        <div className="corpus-actions">
          <StatusBadge tone={phase.tone}>{phase.label}</StatusBadge>
          {onRemove && corpus.source && (
            <button
              className="is-danger"
              type="button"
              onClick={() => onRemove({
                operation: "source",
                id: corpus.source!.id,
                title: corpus.label,
                impact: corpus.kind === "extension"
                  ? "Будут удалены индекс этого расширения, его каталог кода и журнал покрытия. Родительская конфигурация останется."
                  : "Будут удалены индекс основного кода, каталог модулей и журнал покрытия. Структура конфигурации останется.",
              })}
              aria-label={`Удалить ${corpus.label}`}
            >
              <Trash2 size={16} aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            aria-label={`${expanded ? "Скрыть" : "Показать"} подробности ${corpus.label}`}
          >
            <ChevronDown size={18} aria-hidden="true" />
          </button>
        </div>
      </header>

      <p className="corpus-state">{corpus.state}</p>

      {expanded && (
        <div className="corpus-details">
          {corpus.source && (
            <dl className="source-facts">
              <div><dt>Элементов выгрузки</dt><dd>{formatNumber(corpus.source.items_total)}</dd></div>
              <div><dt>Версия кода</dt><dd>{corpus.source.code_version || "—"}</dd></div>
              <div><dt>Загружено</dt><dd>{formatDate(corpus.source.loaded_at)}</dd></div>
            </dl>
          )}

          {(corpus.phase === "limited" || corpus.source?.incomplete) && (
            <div className="inline-warning">
              <AlertTriangle size={18} aria-hidden="true" />
              <span>Нулевой счётчик не доказывает отсутствие данных: корпус прочитан с ограничениями.</span>
            </div>
          )}

          {corpus.source?.warnings.map((warning) => (
            <div className="inline-warning" key={warning}>
              <AlertTriangle size={18} aria-hidden="true" /><span>{warning}</span>
            </div>
          ))}

          {corpus.coverage ? (
            <CoverageTables coverage={corpus.coverage} />
          ) : (
            <div className="coverage-empty">Таблицы появятся после завершения разбора корпуса.</div>
          )}

          <footer className="corpus-footer">
            {corpus.journal_url ? (
              <a href={corpus.journal_url} target="_blank" rel="noreferrer">
                <FileJson size={17} aria-hidden="true" />Открыть JSON-журнал
              </a>
            ) : (
              <span><ServerCrash size={17} aria-hidden="true" />Журнал пока недоступен</span>
            )}
            {corpus.journal && <code>data/{corpus.journal}</code>}
          </footer>
        </div>
      )}
    </article>
  );
}

function RuntimeSourceCard({
  source,
  onRemove,
}: {
  source: SourceItem;
  onRemove?: (target: RemovalTarget) => void;
}) {
  const hasWarnings = source.warnings.length > 0;
  return (
    <article className={`corpus-card is-${hasWarnings ? "limited" : "ready"}`}>
      <header>
        <div className="corpus-title">
          <span className="corpus-icon" aria-hidden="true"><FileJson size={20} /></span>
          <div>
            <span className="eyebrow">Runtime-состояние</span>
            <h3>Активность расширений</h3>
          </div>
        </div>
        <div className="corpus-actions">
          <StatusBadge tone={hasWarnings ? "warning" : "success"}>
            {hasWarnings ? "Устарел" : "Снимок загружен"}
          </StatusBadge>
          {onRemove && (
            <button
              className="is-danger"
              type="button"
              aria-label="Удалить снимок активности расширений"
              onClick={() => onRemove({
                operation: "source",
                id: source.id,
                title: "Снимок активности расширений",
                impact: "Будет снят только сеансовый снимок активности. Структура и код расширений останутся.",
              })}
            >
              <Trash2 size={16} aria-hidden="true" />
            </button>
          )}
        </div>
      </header>
      <p className="corpus-state">
        Расширений в снимке: {formatNumber(source.items_total)} · загружено {formatDate(source.loaded_at)}
      </p>
      {source.warnings.map((warning) => (
        <div className="inline-warning" key={warning}>
          <AlertTriangle size={18} aria-hidden="true" /><span>{warning}</span>
        </div>
      ))}
    </article>
  );
}

function ConfigurationDetail({
  configuration,
  onRemove,
}: {
  configuration: ConfigurationSource;
  onRemove?: (target: RemovalTarget) => void;
}) {
  return (
    <div className="configuration-detail">
      <section className="configuration-hero">
        <div className="configuration-hero-copy">
          <span className="eyebrow">Выбранная конфигурация</span>
          <h1 title={configuration.id}>{configuration.id}</h1>
          <p>Структура, основной код и расширения собраны в один связанный контур.</p>
        </div>
        <div className="configuration-hero-actions">
          <StatusBadge tone="success">Источник активен</StatusBadge>
          {onRemove && configuration.source && (
            <button
              className="button-danger-quiet"
              type="button"
              onClick={() => onRemove({
                operation: "source",
                id: configuration.source!.id,
                title: configuration.id,
                impact: "Будут каскадно удалены структура конфигурации, основной код, все привязанные расширения и их журналы покрытия.",
              })}
            >
              <Trash2 size={15} aria-hidden="true" />Удалить
            </button>
          )}
        </div>
      </section>

      <section className="configuration-facts" aria-label="Сводка конфигурации">
        <div><span>Версия</span><strong>{configuration.version || "—"}</strong></div>
        <div><span>Платформа</span><strong>{configuration.platform || "—"}</strong></div>
        <div><span>Объекты</span><strong>{formatNumber(configuration.objects)}</strong></div>
        <div><span>Связи</span><strong>{formatNumber(configuration.edges)}</strong></div>
      </section>

      {configuration.notes.map((note) => (
        <div className="configuration-note" key={note}>
          <AlertTriangle size={18} aria-hidden="true" /><span>{note}</span>
        </div>
      ))}

      <section className="relationship-strip" aria-label="Состав конфигурации">
        <div><PackageCheck size={20} aria-hidden="true" /><span><strong>Структура</strong><small>{formatNumber(configuration.objects)} объектов</small></span></div>
        <div><Layers3 size={20} aria-hidden="true" /><span><strong>Основной код</strong><small>{configuration.corpora.some((item) => item.kind === "modules") ? "подключён" : "не загружен"}</small></span></div>
        <div><Puzzle size={20} aria-hidden="true" /><span><strong>Расширения</strong><small>{configuration.corpora.filter((item) => item.kind === "extension").length}</small></span></div>
        <div><FileJson size={20} aria-hidden="true" /><span><strong>Активность</strong><small>{configuration.extension_runtime ? "снимок" : "unknown"}</small></span></div>
      </section>

      <div className="corpus-stack">
        {configuration.extension_runtime && (
          <RuntimeSourceCard source={configuration.extension_runtime} onRemove={onRemove} />
        )}
        {configuration.corpora.map((corpus) => (
          <CorpusCard corpus={corpus} key={corpus.id} onRemove={onRemove} />
        ))}
      </div>
    </div>
  );
}

function RemovalDialog({
  target,
  onClose,
}: {
  target: RemovalTarget;
  onClose: () => void;
}) {
  const removeSource = useRemoveSource();
  const forgetSource = useForgetSource();
  const [confirmation, setConfirmation] = useState("");
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const mutation = target.operation === "source" ? removeSource : forgetSource;
  const requiresExactName = target.operation === "source";
  const confirmed = !requiresExactName || confirmation === target.id;

  const copyExactName = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API недоступен");
      await navigator.clipboard.writeText(target.id);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  const remove = async () => {
    if (!confirmed) return;
    try {
      await mutation.mutateAsync(target.id);
      onClose();
    } catch {
      // Сообщение остаётся в модальном окне через mutation.error.
    }
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !mutation.isPending) onClose();
    }}>
      <section className="removal-dialog" role="dialog" aria-modal="true" aria-labelledby="removal-title">
        <button className="modal-close" type="button" onClick={onClose} aria-label="Закрыть" disabled={mutation.isPending}>
          <X size={18} />
        </button>
        <span className="removal-icon"><Trash2 size={22} /></span>
        <span className="eyebrow">Необратимое действие</span>
        <h2 id="removal-title">
          {target.operation === "orphan" ? "Удалить исходный файл?" : `Удалить «${target.title}»?`}
        </h2>
        <p>{target.impact}</p>
        {requiresExactName && (
          <div className="confirmation-field">
            <label htmlFor="removal-confirmation">Для подтверждения введите точное имя:</label>
            <div className="confirmation-name">
              <code title={target.id}>{target.id}</code>
              <button
                className="button-secondary confirmation-copy-button"
                type="button"
                onClick={copyExactName}
                aria-label={copyState === "copied" ? "Точное имя скопировано" : "Скопировать точное имя"}
              >
                {copyState === "copied" ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
                {copyState === "copied" ? "Скопировано" : "Копировать"}
              </button>
            </div>
            <input
              id="removal-confirmation"
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
        )}
        {mutation.isError && <div className="admin-feedback is-danger">{mutation.error instanceof Error ? mutation.error.message : "Не удалось удалить."}</div>}
        <footer>
          <button className="button-secondary" type="button" onClick={onClose} disabled={mutation.isPending}>Отмена</button>
          <button className="button-danger" type="button" onClick={remove} disabled={!confirmed || mutation.isPending}>
            {mutation.isPending
              ? "Удаляем…"
              : target.operation === "orphan" ? "Удалить файл" : "Удалить без возможности отмены"}
          </button>
        </footer>
      </section>
    </div>
  );
}

export function SourcesPage() {
  const query = useSources();
  const [searchParams, setSearchParams] = useSearchParams();
  const [removalTarget, setRemovalTarget] = useState<RemovalTarget | null>(null);
  const configurations = query.data?.configurations ?? [];
  const requested = searchParams.get("config");
  const selected = configurations.find((item) => item.id === requested) ?? configurations[0];

  const select = (id: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("config", id);
    setSearchParams(next, { replace: true, preventScrollReset: true });
  };

  if (query.isLoading) {
    return <section className="sources-message"><span className="loading-dot" />Загружаем единый снимок источников…</section>;
  }
  if (query.isError) {
    return <section className="sources-message is-error"><ServerCrash />Не удалось получить снимок источников.</section>;
  }

  return (
    <div className="sources-page">
      <header className="sources-page-heading">
        <div>
          <span className="eyebrow">Управление данными</span>
          <h1>Источники</h1>
          <p>Выберите конфигурацию — справа останутся только её структура, основной код и расширения.</p>
        </div>
        <StatusBadge tone={query.data?.permissions.admin ? "info" : "success"}>
          {query.data?.permissions.admin ? "Права администратора" : "Только чтение"}
        </StatusBadge>
      </header>

      <div className="sources-layout">
        <aside className="sources-master" aria-label="Выбор источника">
          <div className="sources-master-title"><span>Конфигурации</span><strong>{configurations.length}</strong></div>
          <div className="configuration-nav-list">
            {configurations.map((configuration) => (
              <ConfigurationNavItem
                key={configuration.id}
                configuration={configuration}
                selected={selected?.id === configuration.id}
                onSelect={() => select(configuration.id)}
              />
            ))}
          </div>

          <div className="references-title">Общие источники</div>
          <div className="reference-list">
            {query.data?.references.length
              ? query.data.references.map((source) => (
                <ReferenceRow
                  source={source}
                  key={source.id}
                  onRemove={query.data.permissions.admin ? setRemovalTarget : undefined}
                />
              ))
              : <span className="reference-empty">Справочники не загружены</span>}
          </div>
        </aside>

        <main className="sources-detail">
          {selected ? (
            <ConfigurationDetail
              configuration={selected}
              onRemove={query.data?.permissions.admin ? setRemovalTarget : undefined}
            />
          ) : (
            <section className="sources-empty">
              <Boxes size={34} aria-hidden="true" />
              <h2>Конфигурации не загружены</h2>
              <p>После загрузки здесь появятся структура, основной код, расширения и покрытие.</p>
            </section>
          )}
        </main>
      </div>

      {query.data?.permissions.admin && (
        <SourcesAdminPanel
          onRequestForget={(path) => setRemovalTarget({
            operation: "orphan",
            id: path,
            title: "исходный файл",
            impact: "Будет удалён исходный файл, который сейчас не заявлен ни одним источником. Если индекс понадобится построить заново, файл придётся получить повторно.",
          })}
        />
      )}

      {removalTarget && <RemovalDialog target={removalTarget} onClose={() => setRemovalTarget(null)} />}
    </div>
  );
}
