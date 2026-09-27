import {
  ArrowLeft,
  BookOpenText,
  Braces,
  Code2,
  Eye,
  FileQuestion,
  ServerCrash,
} from "lucide-react";
import { useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  CardApiError,
  type CardDetail,
  type CardKind,
  type CardNavigation,
  useCard,
} from "../shared/api/cards";
import { StatusBadge } from "../shared/ui/StatusBadge";
import "./CardPage.css";

const detailLabel: Record<CardDetail, string> = {
  brief: "Кратко",
  fields: "Поля",
  full: "Полностью",
  links: "Ссылки",
};

const sectionLabel: Record<string, string> = {
  methods: "Методы",
  properties: "Свойства",
  events: "События",
  constructors: "Конструкторы",
  collection: "Элементы коллекции",
  see_also: "См. также",
};

function navigationAddress(address: string, config: string): string {
  const params = new URLSearchParams({ name: address, detail: "full" });
  if (config) params.set("config", config);
  return `/syntax?${params}`;
}

function Navigation({ groups, config, detail, onPage, onContinue }: {
  groups: CardNavigation[];
  config: string;
  detail: CardDetail;
  onPage: (offset: number) => void;
  onContinue: (offset: number) => void;
}) {
  if (!groups.length) return null;
  return (
    <section className="card-navigation" aria-label="Навигация по справке">
      <h2>Связанные страницы справки</h2>
      {groups.map((group, index) => (
        <div className="card-navigation-group" key={`${group.variant}-${index}`}>
          {group.variant && <h3>{group.variant}</h3>}
          {group.platform && <p>Версия платформы: {group.platform}</p>}
          {group.state === "legacy" && <p>Для этой справки ссылки ещё не разобраны. Требуется повторная загрузка источника.</p>}
          {group.state === "unknown" && <p>Ссылки для этой версии платформы не подтверждены.</p>}
          {group.state === "known" && (
            <>
              {group.total === 0 ? <p>Связанных страниц нет.</p> : (
                <>
                  <p>Ссылки: {group.total}.{group.items.length ? ` Показаны ${group.offset + 1}–${group.offset + group.items.length}.` : " На этой странице ссылок нет."}</p>
                  {group.items.length > 0 && <ul>
                    {group.items.map((item, itemIndex) => (
                      <li key={`${item.section}-${item.address}-${itemIndex}`}>
                        <span className="card-navigation-section">{sectionLabel[item.section] || item.section}: </span>
                        {item.status === "ready" && item.address
                          ? <Link to={navigationAddress(item.address, config)}>{item.label}</Link>
                          : <span>{item.label} <small>({item.status === "unavailable" ? "недоступно в этой версии" : item.status === "ambiguous" ? "неоднозначная цель" : "цель не найдена"})</small></span>}
                      </li>
                    ))}
                  </ul>}
                </>
              )}
              {detail === "full" && group.next_offset !== null && (
                <button type="button" onClick={() => onContinue(group.next_offset!)}>Показать остальные ссылки</button>
              )}
              {detail === "links" && (group.offset > 0 || group.next_offset !== null) && (
                <nav className="card-navigation-pages" aria-label="Страницы ссылок">
                  <button type="button" disabled={group.offset === 0} onClick={() => onPage(Math.max(0, group.offset - 50))}>Назад</button>
                  <button type="button" disabled={group.next_offset === null} onClick={() => group.next_offset !== null && onPage(group.next_offset)}>Далее</button>
                </nav>
              )}
            </>
          )}
        </div>
      ))}
    </section>
  );
}

const allowedBackPaths = new Set(["/graph", "/queries"]);
const unsafeBackCharacters = /[\\\u0000-\u001f\u007f]|%(?:5c|0[0-9a-f]|1[0-9a-f]|7f)/i;

function normalizedBackUrl(requested: string | null): string {
  if (!requested?.startsWith("/") || unsafeBackCharacters.test(requested)) return "/queries";

  try {
    const normalized = new URL(requested, window.location.origin);
    if (normalized.origin !== window.location.origin || !allowedBackPaths.has(normalized.pathname)) {
      return "/queries";
    }
    const queryIndex = requested.indexOf("?");
    const hashIndex = requested.indexOf("#");
    const suffixIndex = [queryIndex, hashIndex]
      .filter((index) => index >= 0)
      .reduce((first, index) => Math.min(first, index), requested.length);
    return normalized.pathname + requested.slice(suffixIndex);
  } catch {
    return "/queries";
  }
}

export function CardPage({ kind }: { kind: CardKind }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const name = searchParams.get("name") || "";
  const config = searchParams.get("config") || "";
  const requestedDetail = searchParams.get("detail");
  const requestedBack = searchParams.get("from");
  const backUrl = normalizedBackUrl(requestedBack);
  const backLabel = backUrl.startsWith("/graph") ? "Вернуться к связям" : "К результатам запросов";
  const detail: CardDetail = requestedDetail === "brief" || requestedDetail === "full" || (kind === "syntax" && requestedDetail === "links")
    ? requestedDetail
    : "fields";
  const requestedOffset = Number(searchParams.get("links_offset") || "0");
  const linksOffset = detail === "links" && Number.isSafeInteger(requestedOffset) && requestedOffset >= 0
    ? requestedOffset : 0;
  const raw = searchParams.get("raw") === "1";
  const card = useCard(kind, config, name, detail, linksOffset);
  const noun = kind === "object" ? "объекта" : "синтаксиса";
  const Icon = kind === "object" ? Braces : BookOpenText;

  useEffect(() => {
    if (!card.data) return;
    const next = new URLSearchParams(searchParams);
    let changed = false;
    if (requestedDetail && next.get("detail") !== card.data.detail) {
      next.set("detail", card.data.detail);
      changed = true;
    }
    if (!config && card.data.configuration) {
      next.set("config", card.data.configuration);
      changed = true;
    }
    if (changed) setSearchParams(next, { replace: true, preventScrollReset: true });
  }, [card.data, config, requestedDetail, searchParams, setSearchParams]);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key === "name" || key === "config" || key === "detail") next.delete("links_offset");
    setSearchParams(next, { replace: true, preventScrollReset: true });
  };

  const continueLinks = (offset: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("detail", "links");
    next.set("links_offset", String(offset));
    setSearchParams(next, { replace: true, preventScrollReset: true });
  };

  if (!name) {
    return (
      <section className="card-empty-state">
        <FileQuestion size={34} aria-hidden="true" />
        <span className="eyebrow">Нужен точный адрес</span>
        <h1>Карточка не выбрана</h1>
        <p>Сначала найдите элемент на странице «Запросы» — результат передаст сюда точное имя и конфигурацию.</p>
        <Link to="/queries">Перейти к запросам</Link>
      </section>
    );
  }

  if (card.isPending) {
    return <section className="sources-message"><span className="loading-dot" />Собираем карточку {noun}…</section>;
  }

  if (card.isError || !card.data) {
    const message = card.error instanceof CardApiError
      ? card.error.message
      : `Не удалось получить карточку ${noun}.`;
    return (
      <section className="card-empty-state is-error" role="alert">
        <ServerCrash size={34} aria-hidden="true" />
        <span className="eyebrow">Карточка недоступна</span>
        <h1>Не удалось открыть {name}</h1>
        <p>{message}</p>
        <Link to={backUrl}>{backLabel}</Link>
      </section>
    );
  }

  const data = card.data;

  return (
    <div className="card-page">
      <header className="card-page-heading">
        <div className="card-page-heading-copy">
          <Link className="card-back-link" to={backUrl}><ArrowLeft size={16} />{backLabel}</Link>
          <span className="eyebrow">Диагностическая карточка {noun}</span>
          <h1>{name}</h1>
          <p>Данные не пересобираются во frontend: ниже буквально тот ответ, который MCP отдаёт агенту.</p>
        </div>
        <StatusBadge tone="info">Только чтение</StatusBadge>
      </header>

      <div className="card-workspace">
        <aside className="card-controls" aria-label="Настройки карточки">
          <div className="card-kind-mark">
            <span><Icon size={20} aria-hidden="true" /></span>
            <div><strong>Карточка {noun}</strong><small>{kind === "object" ? "Метаданные и код" : "Справка платформы"}</small></div>
          </div>

          <label className="query-field">
            <span>Конфигурация</span>
            <select
              aria-label="Конфигурация"
              value={data.configuration}
              onChange={(event) => setParam("config", event.target.value)}
              disabled={!data.configuration_names.length}
            >
              {!data.configuration_names.length && <option value="">Без фильтра по версии</option>}
              {data.configuration_names.map((item) => <option key={item}>{item}</option>)}
            </select>
          </label>

          <fieldset className="card-detail-control">
            <legend>Подробность</legend>
            {data.detail_levels.map((level) => (
              <button
                type="button"
                key={level}
                aria-label={level}
                aria-pressed={data.detail === level}
                onClick={() => setParam("detail", level)}
              >
                <strong>{level}</strong>
                <small>{detailLabel[level]}</small>
              </button>
            ))}
          </fieldset>

          <div className="card-view-control" aria-label="Представление карточки">
            <button type="button" aria-pressed={!raw} onClick={() => setParam("raw", "")}>
              <Eye size={16} aria-hidden="true" />Разобрать
            </button>
            <button type="button" aria-pressed={raw} onClick={() => setParam("raw", "1")}>
              <Code2 size={16} aria-hidden="true" />Как есть
            </button>
          </div>

          <div className="card-contract-note">
            <strong>Тот же ответ, что получает агент</strong>
            <span>Уровень меняет объём ответа на сервере. Представление меняет только способ показа.</span>
          </div>
        </aside>

        <article className={raw ? "card-document is-raw" : "card-document"}>
          <div className="card-document-bar">
            <span>{raw ? "Исходный Markdown" : "Разобранный ответ"}</span>
            <code>{data.detail}</code>
          </div>
          {raw ? (
            <pre className="card-raw-text">{data.markdown}</pre>
          ) : (
            <div className="card-markdown" dangerouslySetInnerHTML={{ __html: data.html }} />
          )}
          {kind === "syntax" && (data.detail === "full" || data.detail === "links") && data.navigation && (
            <Navigation groups={data.navigation} config={data.configuration || config} detail={data.detail} onPage={(offset) => setParam("links_offset", offset ? String(offset) : "")} onContinue={continueLinks} />
          )}
        </article>
      </div>
    </div>
  );
}
