import {
  AlertTriangle,
  BookOpenText,
  Braces,
  Database,
  FilterX,
  Search,
  ServerCrash,
  Sparkles,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  QueriesApiError,
  type QueryRunResponse,
  type QueryScope,
  runQueries,
  useQueriesSetup,
} from "../shared/api/queries";
import { StatusBadge } from "../shared/ui/StatusBadge";

const STORAGE_KEY = "mcp1c-dashboard-queries";

type StoredQueryPage = {
  phrasesText: string;
  response: QueryRunResponse | null;
  scrollY: number;
  config: string;
  scope: QueryScope | "";
};

const emptyStored: StoredQueryPage = {
  phrasesText: "",
  response: null,
  scrollY: 0,
  config: "",
  scope: "",
};

function storedScope(value: unknown): QueryScope | "" {
  return value === "objects" || value === "fields" || value === "syntax" ? value : "";
}

function readStored(): StoredQueryPage {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) || "null") as Partial<StoredQueryPage> | null;
    return {
      phrasesText: typeof parsed?.phrasesText === "string" ? parsed.phrasesText : "",
      response: parsed?.response?.api_version === "v1" ? parsed.response : null,
      scrollY: typeof parsed?.scrollY === "number" ? parsed.scrollY : 0,
      config: typeof parsed?.config === "string" ? parsed.config : "",
      scope: storedScope(parsed?.scope),
    };
  } catch {
    return emptyStored;
  }
}

function writeStored(patch: Partial<StoredQueryPage>) {
  try {
    const current = readStored();
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ ...current, ...patch }));
  } catch {
    // Приватный режим или исчерпанная квота не должны ломать сам поиск.
  }
}

const scopeDescription: Record<QueryScope, string> = {
  objects: "Метаданные конфигурации",
  fields: "Реквизиты и измерения",
  syntax: "Справка платформы",
};

const scopeIcon = {
  objects: Database,
  fields: Braces,
  syntax: BookOpenText,
};

function formatScore(score: number) {
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(score);
}

function ResultGroup({ result }: { result: QueryRunResponse["results"][number] }) {
  const visible = result.hits.length;
  return (
    <article className="query-result-group">
      <header>
        <div>
          <span className="eyebrow">Поисковая фраза</span>
          <h2>{result.phrase}</h2>
        </div>
        <StatusBadge tone={visible ? "success" : result.hidden.length ? "warning" : "danger"}>
          {visible ? `${visible} из 5` : result.hidden.length ? "Скрыто версией" : "Нет попаданий"}
        </StatusBadge>
      </header>

      {!visible && (
        <div className="query-no-hits">
          <FilterX size={19} aria-hidden="true" />
          {result.hidden.length ? "В этой версии платформы видимых результатов нет." : "Ничего не найдено."}
        </div>
      )}

      {result.hits.length > 0 && (
        <div className="query-hits" aria-label={`Результаты: ${result.phrase}`}>
          {result.hits.map((hit) => (
            <div className="query-hit-row" key={`${hit.position}:${hit.id}`}>
              <span className="query-hit-position">{hit.position}</span>
              <div className="query-hit-title">
                <Link to={hit.card_url}>{hit.title}</Link>
                <span>{hit.kind}</span>
              </div>
              <strong className="query-hit-score" title="Оценка ранжирования">{formatScore(hit.score)}</strong>
              <span className="query-hit-reason">{hit.reason || "—"}</span>
            </div>
          ))}
        </div>
      )}

      {result.hidden.length > 0 && (
        <details className="query-hidden">
          <summary>Скрыто фильтром версии <strong>{result.hidden.length}</strong></summary>
          <ul>
            {result.hidden.map((item) => (
              <li key={`${item.title}:${item.reason}`}><code>{item.title}</code><span>{item.reason}</span></li>
            ))}
          </ul>
        </details>
      )}

      {result.alias_url && (
        <footer>
          <Link to={result.alias_url}><Sparkles size={16} aria-hidden="true" />Завести псевдоним для этой фразы</Link>
        </footer>
      )}
    </article>
  );
}

export function QueriesPage() {
  const setup = useQueriesSetup();
  const [searchParams, setSearchParams] = useSearchParams();
  const stored = useMemo(readStored, []);
  const [phrasesText, setPhrasesText] = useState(stored.phrasesText);
  const [response, setResponse] = useState<QueryRunResponse | null>(stored.response);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);

  const names = setup.data?.configuration_names ?? [];
  const restoredConfig = stored.config || stored.response?.request.config || "";
  const requestedConfig = searchParams.get("config") || restoredConfig;
  const selectedConfig = names.includes(requestedConfig)
    ? requestedConfig
    : setup.data?.default_configuration ?? "";
  const requestedScope = (
    searchParams.get("scope")
    || stored.scope
    || stored.response?.request.scope
    || null
  ) as QueryScope | null;
  const defaultScope: QueryScope = setup.data?.availability.configurations
    ? "objects"
    : setup.data?.availability.syntax ? "syntax" : "objects";
  const requestedScopeAvailable = requestedScope === "syntax"
    ? setup.data?.availability.syntax
    : requestedScope === "objects" || requestedScope === "fields"
      ? setup.data?.availability.configurations
      : false;
  const selectedScope = setup.data?.scopes.some((scope) => scope.id === requestedScope) && requestedScopeAvailable
    ? requestedScope!
    : defaultScope;
  const scopeAvailable = selectedScope === "syntax"
    ? Boolean(setup.data?.availability.syntax)
    : Boolean(setup.data?.availability.configurations && selectedConfig);
  const phrases = phrasesText.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const visibleResponse = response?.request.config === selectedConfig && response.request.scope === selectedScope
    ? response
    : null;

  useEffect(() => {
    if (!setup.data) return;
    const next = new URLSearchParams(searchParams);
    if (next.get("scope") !== selectedScope) next.set("scope", selectedScope);
    if (selectedConfig && next.get("config") !== selectedConfig) next.set("config", selectedConfig);
    if (!selectedConfig) next.delete("config");
    if (next.toString() !== searchParams.toString()) {
      setSearchParams(next, { replace: true, preventScrollReset: true });
    }
  }, [searchParams, selectedConfig, selectedScope, setSearchParams, setup.data]);

  useEffect(() => {
    writeStored({ phrasesText, response });
  }, [phrasesText, response]);

  useEffect(() => {
    if (!setup.data) return;
    writeStored({ config: selectedConfig, scope: selectedScope });
  }, [selectedConfig, selectedScope, setup.data]);

  useEffect(() => {
    const saved = readStored().scrollY;
    const timer = window.setTimeout(() => {
      if (saved > 0) window.scrollTo({ top: saved, behavior: "auto" });
    }, 0);
    return () => {
      window.clearTimeout(timer);
      writeStored({ scrollY: window.scrollY });
    };
  }, []);

  const setSelection = (key: "config" | "scope", value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true, preventScrollReset: true });
    setResponse(null);
    setError("");
  };

  const chooseScope = (scope: QueryScope) => {
    const next = new URLSearchParams(searchParams);
    next.set("scope", scope);
    if (scope !== "syntax" && !selectedConfig && setup.data?.default_configuration) {
      next.set("config", setup.data.default_configuration);
    }
    setSearchParams(next, { replace: true, preventScrollReset: true });
    setResponse(null);
    setError("");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    if (!setup.data || !scopeAvailable) return;
    if (!phrases.length) {
      setError("Укажите хотя бы одну поисковую фразу.");
      return;
    }
    if (phrases.length > setup.data.limits.phrases) {
      setError(`За один прогон принимается не более ${setup.data.limits.phrases} фраз.`);
      return;
    }
    if (phrases.some((phrase) => phrase.length > setup.data.limits.phrase_chars)) {
      setError(`Каждая фраза должна содержать не более ${setup.data.limits.phrase_chars} символов.`);
      return;
    }
    setRunning(true);
    try {
      const result = await runQueries({ config: selectedConfig, scope: selectedScope, phrases });
      setResponse(result);
      writeStored({ phrasesText, response: result });
    } catch (caught) {
      setError(caught instanceof QueriesApiError ? caught.message : "Не удалось выполнить поиск.");
    } finally {
      setRunning(false);
    }
  };

  if (setup.isLoading) {
    return <section className="sources-message"><span className="loading-dot" />Проверяем поисковые источники…</section>;
  }
  if (setup.isError || !setup.data) {
    return <section className="sources-message is-error"><ServerCrash />Не удалось получить контракт поиска.</section>;
  }

  const contract = setup.data;
  const noneAvailable = !contract.availability.configurations && !contract.availability.syntax;

  return (
    <div className="queries-page">
      <header className="queries-page-heading">
        <div>
          <span className="eyebrow">Диагностика поиска</span>
          <h1>Проверка поисковых формулировок</h1>
          <p>Одна фраза на строку. Результаты строит тот же индекс, которым пользуются MCP-инструменты.</p>
        </div>
        <StatusBadge tone={noneAvailable ? "warning" : "info"}>
          {noneAvailable ? "Нет источников" : "До 5 результатов"}
        </StatusBadge>
      </header>

      {noneAvailable && (
        <section className="query-source-warning is-empty">
          <AlertTriangle size={20} aria-hidden="true" />
          <span><strong>Поисковые источники пока не загружены</strong>Добавьте структуру конфигурации или справку на странице «Источники».</span>
        </section>
      )}
      <div className="queries-workbench">
        <form className="query-controls" onSubmit={submit}>
          <div className="query-controls-title">
            <span className="query-control-icon"><Search size={19} aria-hidden="true" /></span>
            <div><strong>Новый прогон</strong><small>Настройки сохраняются в адресе страницы</small></div>
          </div>

          <label className="query-field">
            <span>Конфигурация</span>
            <select
              aria-label="Конфигурация"
              value={selectedConfig}
              onChange={(event) => setSelection("config", event.target.value)}
              disabled={!names.length}
            >
              {selectedScope === "syntax" && !names.length && <option value="">Без фильтра по версии</option>}
              {names.map((name) => <option key={name}>{name}</option>)}
            </select>
          </label>

          <fieldset className="query-scope-list">
            <legend>Область поиска</legend>
            {contract.scopes.map((scope) => {
              const Icon = scopeIcon[scope.id];
              const available = scope.id === "syntax"
                ? contract.availability.syntax
                : contract.availability.configurations;
              return (
                <label className={selectedScope === scope.id ? "is-selected" : ""} key={scope.id}>
                  <input
                    type="radio"
                    name="query-scope"
                    value={scope.id}
                    checked={selectedScope === scope.id}
                    disabled={!available}
                    onChange={() => chooseScope(scope.id)}
                    aria-label={`${scope.label}: ${scopeDescription[scope.id]}`}
                  />
                  <Icon size={18} aria-hidden="true" />
                  <span><strong>{scope.label}</strong><small>{scopeDescription[scope.id]}</small></span>
                </label>
              );
            })}
          </fieldset>

          <label className="query-field query-phrases-field">
            <span>Поисковые фразы</span>
            <textarea
              aria-label="Поисковые фразы"
              rows={9}
              value={phrasesText}
              onChange={(event) => setPhrasesText(event.target.value)}
              placeholder={"контрагенты\nномер телефона\nкак соединить таблицы в запросе"}
            />
          </label>
          <div className={phrases.length > contract.limits.phrases ? "query-counter is-error" : "query-counter"}>
            <span>{phrases.length} из {contract.limits.phrases} фраз</span>
            <span>до {contract.limits.phrase_chars} знаков в каждой</span>
          </div>

          {error && <div className="query-form-error" role="alert"><AlertTriangle size={17} />{error}</div>}
          <button className="query-run-button" type="submit" disabled={!scopeAvailable || running}>
            <Search size={17} aria-hidden="true" />{running ? "Ищем…" : "Прогнать запросы"}
          </button>
        </form>

        <section className="query-output" id="query-results" aria-live="polite">
          {visibleResponse ? (
            <>
              {(!visibleResponse.sources_revision
                || visibleResponse.sources_revision !== setup.data.sources_revision
                || setup.isFetching) && (
                <div className="query-form-error" role="status">
                  <AlertTriangle size={17} />
                  Результат устарел или его актуальность ещё не подтверждена. Источники могли измениться; прогоните запросы повторно. Ниже сохранена история.
                </div>
              )}
              <div className="query-output-heading">
                <div><span className="eyebrow">Последний прогон</span><h2>Результаты</h2></div>
                <span>{visibleResponse.results.length} фраз</span>
              </div>
              <div className="query-result-list">
                {visibleResponse.results.map((result) => <ResultGroup result={result} key={result.phrase} />)}
              </div>
            </>
          ) : (
            <div className="query-output-empty">
              <Search size={30} aria-hidden="true" />
              <h2>Здесь появится выдача</h2>
              <p>Для каждой фразы покажем вид элемента, числовую оценку, причину места и то, что скрыла версия платформы.</p>
            </div>
          )}
        </section>
      </div>

      <footer className="query-contract-strip">
        <span><strong>Один алгоритм</strong>Ранжирование не повторяется во frontend.</span>
        <span><strong>Версия учтена</strong>Недоступные элементы показаны отдельно.</span>
        <span><strong>Промах можно лечить</strong>Фраза переносится в создание псевдонима.</span>
      </footer>
    </div>
  );
}
