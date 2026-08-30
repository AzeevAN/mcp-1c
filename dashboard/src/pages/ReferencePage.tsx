import { AlertCircle, BookOpenCheck, FileSearch, Search } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  ReferenceApiError,
  useReferenceItem,
  useReferenceSearch,
  useReferenceStatus,
} from "../shared/api/reference";
import { StatusBadge } from "../shared/ui/StatusBadge";

function errorText(error: unknown) {
  return error instanceof ReferenceApiError
    ? error.message
    : "Не удалось прочитать общую справку.";
}

export function ReferencePage() {
  const status = useReferenceStatus();
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("query") || "");
  const [domain, setDomain] = useState(searchParams.get("domain") || "");
  const [kind, setKind] = useState(searchParams.get("kind") || "");
  const [platform, setPlatform] = useState(searchParams.get("platform") || "");
  const [limit, setLimit] = useState(searchParams.get("limit") || "10");
  const [includeExplicit, setIncludeExplicit] = useState(searchParams.get("include_explicit") === "1");
  const [includeHidden, setIncludeHidden] = useState(searchParams.get("include_hidden") === "1");
  const active = status.data?.active;
  const requestedQuery = searchParams.get("query") || "";
  const itemId = searchParams.get("item_id") || "";
  const sectionId = searchParams.get("section_id") || "";
  const cursor = searchParams.get("cursor") || "";
  const requestedPlatform = searchParams.get("platform") || "";
  const search = useReferenceSearch(active?.ready && requestedQuery ? {
    query: requestedQuery,
    domain: searchParams.get("domain") || undefined,
    kind: searchParams.get("kind") || undefined,
    platform: requestedPlatform || undefined,
    include_explicit: searchParams.get("include_explicit") === "1",
    include_hidden: searchParams.get("include_hidden") === "1",
    limit: Number(searchParams.get("limit") || "10"),
  } : null);
  const item = useReferenceItem(active?.ready && itemId ? {
    item_id: itemId,
    section_id: sectionId || undefined,
    cursor: cursor || undefined,
    platform: requestedPlatform || undefined,
    max_chars: 8_000,
  } : null);

  useEffect(() => {
    setQuery(searchParams.get("query") || "");
    setDomain(searchParams.get("domain") || "");
    setKind(searchParams.get("kind") || "");
    setPlatform(searchParams.get("platform") || "");
    setLimit(searchParams.get("limit") || "10");
    setIncludeExplicit(searchParams.get("include_explicit") === "1");
    setIncludeHidden(searchParams.get("include_hidden") === "1");
  }, [searchParams]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const next = new URLSearchParams();
    next.set("query", query.trim());
    if (domain.trim()) next.set("domain", domain.trim());
    if (kind.trim()) next.set("kind", kind.trim());
    if (platform.trim()) next.set("platform", platform.trim());
    next.set("limit", limit);
    if (includeExplicit) next.set("include_explicit", "1");
    if (includeHidden) next.set("include_hidden", "1");
    setSearchParams(next);
  };

  const select = (id: string, matchedSection: string | null) => {
    const next = new URLSearchParams(searchParams);
    next.set("item_id", id);
    if (matchedSection) next.set("section_id", matchedSection);
    else next.delete("section_id");
    next.delete("cursor");
    setSearchParams(next);
  };

  const continueReading = (nextCursor: string) => {
    const next = new URLSearchParams(searchParams);
    next.set("cursor", nextCursor);
    setSearchParams(next);
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
          <span className="eyebrow">Read-only проверка</span>
          <h1>Общая справка</h1>
          <p>Адаптер не активен, основной MCP продолжает работать.</p>
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
          <span className="eyebrow">Read-only проверка</span>
          <h1>Общая справка</h1>
          <p>Поиск и карточка проходят через тот же провайдер, что две MCP-операции.</p>
        </div>
        <StatusBadge tone="success">Подключена</StatusBadge>
      </header>
      <p className="reference-status"><BookOpenCheck size={18} aria-hidden="true" />{active.message}</p>

      <form className="reference-search" onSubmit={submit}>
        <label className="reference-query"><span>Поиск</span><input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={4096} required /></label>
        <label><span>Домен</span><input value={domain} onChange={(event) => setDomain(event.target.value)} maxLength={100} /></label>
        <label><span>Вид</span><input value={kind} onChange={(event) => setKind(event.target.value)} maxLength={100} /></label>
        <label><span>Версия платформы</span><input value={platform} onChange={(event) => setPlatform(event.target.value)} maxLength={64} placeholder="8.3.20" /></label>
        <label><span>Лимит</span><input type="number" value={limit} onChange={(event) => setLimit(event.target.value)} min={1} max={50} required /></label>
        <div className="reference-flags">
          <label><input type="checkbox" checked={includeExplicit} onChange={(event) => setIncludeExplicit(event.target.checked)} />Explicit</label>
          <label><input type="checkbox" checked={includeHidden} onChange={(event) => setIncludeHidden(event.target.checked)} />Hidden</label>
        </div>
        <button className="query-run-button" type="submit"><Search size={17} aria-hidden="true" />Найти</button>
      </form>

      {(search.isError || item.isError) && (
        <div className="reference-unavailable" role="alert">{errorText(search.error || item.error)}</div>
      )}
      {search.isPending && requestedQuery && <div className="section-card">Ищем…</div>}
      {search.data && (
        <section className="reference-results section-card">
          <header><div><span className="eyebrow">Результаты</span><h2>{search.data.query}</h2></div><StatusBadge tone={search.data.results.length ? "success" : "warning"}>{search.data.results.length}</StatusBadge></header>
          {search.data.results.length ? (
            <div className="reference-result-list">
              {search.data.results.map((hit) => (
                <button type="button" key={hit.id} onClick={() => select(hit.id, hit.matched_section_id)} className={itemId === hit.id ? "is-selected" : ""}>
                  <FileSearch size={18} aria-hidden="true" />
                  <span><strong>{hit.title_ru || hit.title_en || hit.id}</strong><small>{hit.kind} · {hit.domain}</small><em>{hit.reason}</em></span>
                </button>
              ))}
            </div>
          ) : <p className="reference-empty">Ничего не найдено.</p>}
        </section>
      )}
      {item.isPending && itemId && <div className="section-card">Читаем карточку…</div>}
      {item.data && (
        <article className="reference-card section-card">
          <header><div><span className="eyebrow">Карточка</span><h2>{item.data.card.title_ru || item.data.card.title_en || item.data.card.id}</h2></div><StatusBadge tone="info">{item.data.card.kind}</StatusBadge></header>
          <code>{item.data.card.id}</code>
          <pre>{item.data.content}</pre>
          {item.data.continuation.next_cursor && (
            <button className="button-secondary" type="button" onClick={() => continueReading(item.data!.continuation.next_cursor!)}>Следующая часть</button>
          )}
        </article>
      )}
    </section>
  );
}
