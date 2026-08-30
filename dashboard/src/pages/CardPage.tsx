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
  useCard,
} from "../shared/api/cards";
import { StatusBadge } from "../shared/ui/StatusBadge";

const detailLabel: Record<CardDetail, string> = {
  brief: "Кратко",
  fields: "Поля",
  full: "Полностью",
};

export function CardPage({ kind }: { kind: CardKind }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const name = searchParams.get("name") || "";
  const config = searchParams.get("config") || "";
  const requestedDetail = searchParams.get("detail");
  const detail: CardDetail = requestedDetail === "brief" || requestedDetail === "full"
    ? requestedDetail
    : "fields";
  const raw = searchParams.get("raw") === "1";
  const card = useCard(kind, config, name, detail);
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
        <Link to="/queries">Вернуться к запросам</Link>
      </section>
    );
  }

  const data = card.data;

  return (
    <div className="card-page">
      <header className="card-page-heading">
        <div className="card-page-heading-copy">
          <Link className="card-back-link" to="/queries"><ArrowLeft size={16} />К результатам запросов</Link>
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
        </article>
      </div>
    </div>
  );
}
