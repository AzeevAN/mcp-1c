import {
  AlertTriangle,
  Boxes,
  Braces,
  ChevronDown,
  FileJson,
  GitFork,
  Layers3,
  PackageCheck,
  Puzzle,
  ServerCrash,
} from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  type CodeCorpus,
  type ConfigurationSource,
  type Coverage,
  type SourceItem,
  useSources,
} from "../shared/api/sources";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";

const sourceKindLabel: Record<SourceItem["kind"], string> = {
  configuration: "Конфигурация",
  modules: "Основной код",
  extension: "Расширение",
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

function ReferenceRow({ source }: { source: SourceItem }) {
  return (
    <div className="reference-row">
      <Braces size={17} aria-hidden="true" />
      <span>
        <strong>{sourceKindLabel[source.kind]}</strong>
        <small>{source.platform || formatNumber(source.items_total) + " элементов"}</small>
      </span>
      <i className="is-success" aria-hidden="true" />
    </div>
  );
}

function CorpusCard({ corpus }: { corpus: CodeCorpus }) {
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

function ConfigurationDetail({ configuration }: { configuration: ConfigurationSource }) {
  return (
    <div className="configuration-detail">
      <section className="configuration-hero">
        <div>
          <span className="eyebrow">Выбранная конфигурация</span>
          <h1>{configuration.id}</h1>
          <p>Структура, основной код и расширения собраны в один связанный контур.</p>
        </div>
        <StatusBadge tone="success">Источник активен</StatusBadge>
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
        <GitFork size={18} aria-hidden="true" />
        <div><Layers3 size={20} aria-hidden="true" /><span><strong>Основной код</strong><small>{configuration.corpora.some((item) => item.kind === "modules") ? "подключён" : "не загружен"}</small></span></div>
        <GitFork size={18} aria-hidden="true" />
        <div><Puzzle size={20} aria-hidden="true" /><span><strong>Расширения</strong><small>{configuration.corpora.filter((item) => item.kind === "extension").length}</small></span></div>
      </section>

      <div className="corpus-stack">
        {configuration.corpora.map((corpus) => <CorpusCard corpus={corpus} key={corpus.id} />)}
      </div>
    </div>
  );
}

export function SourcesPage() {
  const query = useSources();
  const [searchParams, setSearchParams] = useSearchParams();
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
              ? query.data.references.map((source) => <ReferenceRow source={source} key={source.id} />)
              : <span className="reference-empty">Справочники не загружены</span>}
          </div>
        </aside>

        <main className="sources-detail">
          {selected ? (
            <ConfigurationDetail configuration={selected} />
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
        <section className="admin-next-block">
          <span className="eyebrow">Следующий согласуемый блок</span>
          <strong>Загрузка, входящие выгрузки и удаление</strong>
          <p>Административные действия получат отдельную область, прогресс и явные подтверждения.</p>
        </section>
      )}
    </div>
  );
}
