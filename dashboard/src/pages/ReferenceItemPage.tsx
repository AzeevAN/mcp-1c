import {
  ArrowLeft,
  BookOpenText,
  ChevronRight,
  Code2,
  Eye,
  FileQuestion,
  ServerCrash,
} from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import {
  ReferenceApiError,
  useReferenceItem,
  useReferenceStatus,
} from "../shared/api/reference";
import { StatusBadge } from "../shared/ui/StatusBadge";

function withoutCardParams(searchParams: URLSearchParams) {
  const next = new URLSearchParams(searchParams);
  for (const key of ["item_id", "section_id", "cursor", "raw"]) next.delete(key);
  const query = next.toString();
  return query ? `/reference?${query}` : "/reference";
}

function withoutSectionParams(searchParams: URLSearchParams) {
  const next = new URLSearchParams(searchParams);
  next.delete("section_id");
  next.delete("cursor");
  return `/reference/item?${next.toString()}`;
}

export function ReferenceItemPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const itemId = searchParams.get("item_id") || "";
  const sectionId = searchParams.get("section_id") || "";
  const cursor = searchParams.get("cursor") || "";
  const platform = searchParams.get("platform") || "";
  const raw = searchParams.get("raw") === "1";
  const status = useReferenceStatus();
  const item = useReferenceItem(itemId ? {
    item_id: itemId,
    section_id: sectionId || undefined,
    cursor: cursor || undefined,
    platform: platform || undefined,
    max_chars: 20_000,
  } : null);
  const backUrl = withoutCardParams(searchParams);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true, preventScrollReset: true });
  };

  if (!itemId) {
    return (
      <section className="card-empty-state">
        <FileQuestion size={34} aria-hidden="true" />
        <span className="eyebrow">Нужен точный результат</span>
        <h1>Карточка не выбрана</h1>
        <p>Сначала найдите материал в общей справке — результат передаст сюда точный идентификатор.</p>
        <Link to="/reference">Перейти к поиску</Link>
      </section>
    );
  }

  if (item.isPending) {
    return <section className="sources-message"><span className="loading-dot" />Читаем карточку общей справки…</section>;
  }

  if (item.isError || !item.data) {
    const message = item.error instanceof ReferenceApiError
      ? item.error.message
      : "Не удалось получить карточку общей справки.";
    return (
      <section className="card-empty-state is-error" role="alert">
        <ServerCrash size={34} aria-hidden="true" />
        <span className="eyebrow">Карточка недоступна</span>
        <h1>Не удалось открыть {itemId}</h1>
        <p>{message}</p>
        <Link to={backUrl}>Вернуться к результатам</Link>
      </section>
    );
  }

  const data = item.data;
  const catalog = status.data?.catalog;
  const domain = catalog?.domains.find((entry) => entry.id === data.card.domain);
  const kind = catalog?.kinds.find(
    (entry) => entry.domain === data.card.domain && entry.id === data.card.kind,
  );
  const title = data.card.title_ru || data.card.title_en || data.card.id;
  const continuation = data.continuation;
  const availabilityTitle = data.availability.platform
    ? data.availability.status === "available"
      ? "Подходит для версии"
      : data.availability.status === "unavailable"
        ? "Не подходит для версии"
        : "Совместимость не определена"
    : data.availability.introduced
      ? "Версия появления"
      : data.availability.known_present_in
        ? "Известно присутствие"
        : data.availability.removed
          ? "Версия удаления"
          : "Данных о версиях нет";

  return (
    <div className="card-page reference-item-page">
      <header className="card-page-heading">
        <div className="card-page-heading-copy">
          <Link className="card-back-link" to={backUrl}>
            <ArrowLeft size={16} />К результатам общей справки
          </Link>
          <span className="eyebrow">Карточка общей справки</span>
          <h1>{title}</h1>
          <p>{data.card.title_en && data.card.title_en !== title ? data.card.title_en : data.card.id}</p>
        </div>
        <StatusBadge tone="info">Только чтение</StatusBadge>
      </header>

      <div className="card-workspace">
        <aside className="card-controls" aria-label="Сведения о карточке">
          <div className="card-kind-mark">
            <span><BookOpenText size={20} aria-hidden="true" /></span>
            <div><strong>{kind?.title || data.card.kind}</strong><small>{domain?.title || data.card.domain}</small></div>
          </div>

          <dl className="reference-card-meta">
            <div><dt>Раздел</dt><dd>{domain?.title || data.card.domain}</dd></div>
            <div><dt>Вид</dt><dd>{kind?.title || data.card.kind}</dd></div>
            <div><dt>Идентификатор</dt><dd><code>{data.card.id}</code></dd></div>
          </dl>

          {sectionId && (
            <Link className="reference-whole-card" to={withoutSectionParams(searchParams)}>
              Открыть карточку целиком<ChevronRight size={15} aria-hidden="true" />
            </Link>
          )}

          <div className="card-view-control" aria-label="Представление карточки">
            <button type="button" aria-pressed={!raw} onClick={() => setParam("raw", "")}>
              <Eye size={16} aria-hidden="true" />Разобрать
            </button>
            <button type="button" aria-pressed={raw} onClick={() => setParam("raw", "1")}>
              <Code2 size={16} aria-hidden="true" />Как есть
            </button>
          </div>

          <div className={`reference-card-availability is-${data.availability.status}`}>
            <strong>{availabilityTitle}</strong>
            <span>{data.availability.reason}</span>
          </div>

          <div className="card-contract-note">
            <strong>Тот же текст, что получает агент</strong>
            <span>Переключатель меняет только представление; содержимое приходит из провайдера общей справки.</span>
          </div>
        </aside>

        <article className={raw ? "card-document is-raw" : "card-document"}>
          <div className="card-document-bar">
            <span>{raw ? "Исходный Markdown" : "Разобранная карточка"}</span>
            <code>{continuation.next_offset} из {continuation.total_chars}</code>
          </div>
          {raw ? (
            <pre className="card-raw-text">{data.content}</pre>
          ) : (
            <div className="card-markdown" dangerouslySetInnerHTML={{ __html: data.html }} />
          )}
          {continuation.next_cursor && (
            <footer className="reference-card-continuation">
              <span>Показана часть карточки; продолжение начинается с символа {continuation.next_offset}.</span>
              <button type="button" onClick={() => setParam("cursor", continuation.next_cursor || "")}>
                Следующая часть<ChevronRight size={15} aria-hidden="true" />
              </button>
            </footer>
          )}
        </article>
      </div>
    </div>
  );
}
