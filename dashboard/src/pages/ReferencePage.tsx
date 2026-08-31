import {
  AlertCircle,
  ArrowRight,
  BookOpenCheck,
  ChevronDown,
  FileSearch,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  ReferenceApiError,
  type ReferenceHit,
  useReferenceSearch,
  useReferenceStatus,
} from "../shared/api/reference";
import type { ReferenceCatalog } from "../shared/api/sourceAdmin";
import { StatusBadge } from "../shared/ui/StatusBadge";

const DEFAULT_LIMIT = 5;

function errorText(error: unknown) {
  return error instanceof ReferenceApiError
    ? error.message
    : "Не удалось прочитать общую справку.";
}

function formatScore(score: number) {
  return new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(score);
}

function itemUrl(searchParams: URLSearchParams, hit: ReferenceHit) {
  const next = new URLSearchParams(searchParams);
  next.set("item_id", hit.id);
  if (hit.matched_section_id) next.set("section_id", hit.matched_section_id);
  else next.delete("section_id");
  next.delete("cursor");
  next.delete("raw");
  return `/reference/item?${next.toString()}`;
}

function domainTitle(catalog: ReferenceCatalog | null, id: string) {
  return catalog?.domains.find((item) => item.id === id)?.title || id;
}

function kindTitle(catalog: ReferenceCatalog | null, domain: string, id: string) {
  return catalog?.kinds.find(
    (item) => item.domain === domain && item.id === id,
  )?.title || id;
}

function HitRow({
  hit,
  position,
  catalog,
  searchParams,
}: {
  hit: ReferenceHit;
  position: number;
  catalog: ReferenceCatalog | null;
  searchParams: URLSearchParams;
}) {
  return (
    <div className="reference-hit-row">
      <span className="reference-hit-position">{position}</span>
      <div className="reference-hit-title">
        <Link to={itemUrl(searchParams, hit)}>
          {hit.title_ru || hit.title_en || hit.id}
          <ArrowRight size={14} aria-hidden="true" />
        </Link>
        {hit.signature && <code>{hit.signature}</code>}
        <span>
          {domainTitle(catalog, hit.domain)} · {kindTitle(catalog, hit.domain, hit.kind)}
        </span>
      </div>
      <strong className="reference-hit-score" title="Оценка ранжирования">
        {formatScore(hit.score)}
      </strong>
      <span className="reference-hit-reason">
        {hit.reason || "—"}
        {hit.availability.platform && <small>{hit.availability.reason}</small>}
      </span>
      <span
        className={`reference-availability is-${hit.availability.status}`}
        title={hit.availability.reason}
      >
        {hit.availability.status === "available"
          ? "Доступно"
          : hit.availability.status === "unavailable" ? "Недоступно" : "Версия не проверена"}
      </span>
    </div>
  );
}

export function ReferencePage() {
  const status = useReferenceStatus();
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("query") || "");
  const [domain, setDomain] = useState(searchParams.get("domain") || "");
  const [kind, setKind] = useState(searchParams.get("kind") || "");
  const [platform, setPlatform] = useState(searchParams.get("platform") || "");
  const [includeExplicit, setIncludeExplicit] = useState(
    searchParams.get("include_explicit") === "1",
  );
  const [includeHidden, setIncludeHidden] = useState(
    searchParams.get("include_hidden") === "1",
  );
  const [advancedOpen, setAdvancedOpen] = useState(Boolean(
    searchParams.get("kind")
    || searchParams.get("platform")
    || searchParams.get("include_explicit")
    || searchParams.get("include_hidden"),
  ));
  const active = status.data?.active;
  const catalog = status.data?.catalog || null;
  const requestedQuery = searchParams.get("query") || "";
  const rawLimit = Number(searchParams.get("limit") || DEFAULT_LIMIT);
  const requestedLimit = Number.isFinite(rawLimit)
    ? Math.min(50, Math.max(DEFAULT_LIMIT, rawLimit))
    : DEFAULT_LIMIT;
  const requestedPlatform = searchParams.get("platform") || "";
  const search = useReferenceSearch(active?.ready && requestedQuery ? {
    query: requestedQuery,
    domain: searchParams.get("domain") || undefined,
    kind: searchParams.get("kind") || undefined,
    platform: requestedPlatform || undefined,
    include_explicit: searchParams.get("include_explicit") === "1",
    include_hidden: searchParams.get("include_hidden") === "1",
    limit: requestedLimit,
  } : null);

  useEffect(() => {
    setQuery(searchParams.get("query") || "");
    setDomain(searchParams.get("domain") || "");
    setKind(searchParams.get("kind") || "");
    setPlatform(searchParams.get("platform") || "");
    setIncludeExplicit(searchParams.get("include_explicit") === "1");
    setIncludeHidden(searchParams.get("include_hidden") === "1");
    if (
      searchParams.get("kind")
      || searchParams.get("platform")
      || searchParams.get("include_explicit")
      || searchParams.get("include_hidden")
    ) setAdvancedOpen(true);
  }, [searchParams]);

  const kinds = useMemo(
    () => (catalog?.kinds || []).filter((item) => !domain || item.domain === domain),
    [catalog, domain],
  );
  const chooseDomain = (value: string) => {
    setDomain(value);
    if (kind && !catalog?.kinds.some(
      (item) => item.id === kind && (!value || item.domain === value),
    )) setKind("");
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = new URLSearchParams();
    next.set("query", query.trim());
    if (domain) next.set("domain", domain);
    if (kind) next.set("kind", kind);
    if (platform.trim()) next.set("platform", platform.trim());
    if (includeExplicit) next.set("include_explicit", "1");
    if (includeHidden) next.set("include_hidden", "1");
    setSearchParams(next);
  };

  const showMore = () => {
    const next = new URLSearchParams(searchParams);
    next.set("limit", String(Math.min(50, requestedLimit + DEFAULT_LIMIT)));
    setSearchParams(next, { preventScrollReset: true });
  };

  if (status.isPending) {
    return <section className="reference-page"><div className="section-card">Проверяем состояние общей справки…</div></section>;
  }
  if (status.isError || !active) {
    return <section className="reference-page"><div className="reference-unavailable" role="alert">{errorText(status.error)}</div></section>;
  }
  if (!active.ready) {
    return (
      <section className="reference-page page-stack">
        <header className="reference-page-heading">
          <span className="eyebrow">Общий справочный источник</span>
          <h1>Общая справка</h1>
          <p>Источник не активен, основной MCP продолжает работать.</p>
        </header>
        <div className="reference-unavailable">
          <AlertCircle size={22} aria-hidden="true" />
          <div><strong>{active.message}</strong><span>Состояние: {active.state}.</span></div>
          <Link to="/sources">Открыть Источники</Link>
        </div>
      </section>
    );
  }

  return (
    <section className="reference-page page-stack">
      <header className="reference-page-heading">
        <div>
          <span className="eyebrow">Общий справочный источник</span>
          <h1>Общая справка</h1>
          <p>Конструкции BSL, язык запросов, СКД и инструменты разработки — отдельно от выбранной конфигурации.</p>
        </div>
        <StatusBadge tone="success">Только чтение</StatusBadge>
      </header>
      <p className="reference-status"><BookOpenCheck size={18} aria-hidden="true" />{active.message}</p>

      <form className="reference-search" onSubmit={submit}>
        <div className="reference-search-main">
          <label className="reference-query">
            <span>Что найти</span>
            <input
              aria-label="Что найти"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={4096}
              placeholder="например, уникальные строки в запросе"
              required
            />
          </label>
          <label>
            <span>Раздел</span>
            <select
              aria-label="Раздел"
              value={domain}
              onChange={(event) => chooseDomain(event.target.value)}
            >
              <option value="">Все разделы</option>
              {catalog?.domains.map((item) => (
                <option key={item.id} value={item.id}>{item.title}</option>
              ))}
            </select>
          </label>
          <button className="query-run-button" type="submit">
            <Search size={17} aria-hidden="true" />Найти
          </button>
        </div>

        <details
          className="reference-advanced"
          open={advancedOpen}
          onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
        >
          <summary><SlidersHorizontal size={16} aria-hidden="true" />Дополнительные настройки<ChevronDown size={15} aria-hidden="true" /></summary>
          <div className="reference-advanced-fields">
            <label>
              <span>Вид материала <small>обычно не нужен</small></span>
              <select aria-label="Вид материала" value={kind} onChange={(event) => setKind(event.target.value)}>
                <option value="">Любой вид</option>
                {kinds.map((item) => (
                  <option key={`${item.domain}:${item.id}`} value={item.id}>
                    {!domain ? `${domainTitle(catalog, item.domain)} · ` : ""}{item.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Проверить совместимость с версией <small>не влияет на совпадение</small></span>
              <input
                aria-label="Проверить совместимость с версией"
                value={platform}
                onChange={(event) => setPlatform(event.target.value)}
                maxLength={64}
                placeholder="например, 8.3.20"
                list="reference-platforms"
              />
              <datalist id="reference-platforms">
                {catalog?.platform_versions.map((version) => <option key={version} value={version} />)}
              </datalist>
            </label>
            <label className="reference-option">
              <input type="checkbox" checked={includeExplicit} onChange={(event) => setIncludeExplicit(event.target.checked)} />
              <span><strong>Служебные и словарные материалы</strong><small>Дополняют обычную выдачу выбранного раздела.</small></span>
            </label>
            <label className="reference-option">
              <input type="checkbox" checked={includeHidden} onChange={(event) => setIncludeHidden(event.target.checked)} />
              <span><strong>Архивные и скрытые материалы</strong><small>Только для поиска устаревшего поведения.</small></span>
            </label>
          </div>
        </details>
      </form>

      {search.isError && (
        <div className="reference-unavailable" role="alert">{errorText(search.error)}</div>
      )}
      {search.isPending && requestedQuery && <div className="section-card">Ищем…</div>}
      {search.data && (
        <section className="reference-results section-card" aria-live="polite">
          <header>
            <div><span className="eyebrow">Результаты</span><h2>{search.data.query}</h2></div>
            <StatusBadge tone={search.data.results.length ? "success" : search.data.unavailable_matches.length ? "warning" : "danger"}>
              {search.data.results.length ? `${search.data.results.length} найдено` : search.data.unavailable_matches.length ? "Скрыто версией" : "Нет совпадений"}
            </StatusBadge>
          </header>
          {search.data.results.length ? (
            <div className="reference-hit-list" aria-label={`Результаты: ${search.data.query}`}>
              {search.data.results.map((hit, index) => (
                <HitRow
                  key={hit.id}
                  hit={hit}
                  position={index + 1}
                  catalog={catalog}
                  searchParams={searchParams}
                />
              ))}
            </div>
          ) : (
            <p className="reference-empty"><FileSearch size={19} aria-hidden="true" />Ничего не найдено. Попробуйте уточнить раздел или упростить фразу.</p>
          )}
          {search.data.has_more && requestedLimit < 50 && (
            <button className="reference-more" type="button" onClick={showMore}>Показать ещё</button>
          )}
          {search.data.unavailable_matches.length > 0 && (
            <details className="reference-unavailable-matches">
              <summary>Не подходит для версии {search.data.platform} <strong>{search.data.unavailable_matches.length}</strong></summary>
              <div>
                {search.data.unavailable_matches.map((hit, index) => (
                  <HitRow
                    key={hit.id}
                    hit={hit}
                    position={index + 1}
                    catalog={catalog}
                    searchParams={searchParams}
                  />
                ))}
              </div>
            </details>
          )}
        </section>
      )}
    </section>
  );
}
