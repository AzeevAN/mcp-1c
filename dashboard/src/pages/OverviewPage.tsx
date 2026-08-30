import { ArrowRight, Braces, MonitorCog, ServerCog } from "lucide-react";

import { useBootstrap } from "../shared/api/bootstrap";
import { MetricCard } from "../shared/ui/MetricCard";
import { StatusBadge } from "../shared/ui/StatusBadge";

export function OverviewPage() {
  const bootstrap = useBootstrap();
  const summary = bootstrap.data?.summary;

  return (
    <div className="page-stack">
      <section className="hero-panel" aria-labelledby="overview-title">
        <div>
          <span className="eyebrow">Обзор системы</span>
          <h1 id="overview-title">Центр конфигураций</h1>
          <p>
            Один интерфейс для источников, поисковых индексов и диагностики.
            MCP остаётся владельцем данных, дашборд показывает его живое состояние.
          </p>
        </div>
        <StatusBadge tone={bootstrap.isError ? "danger" : "success"}>
          {bootstrap.isError ? "Нет связи с API" : "Контур доступен"}
        </StatusBadge>
      </section>

      <section className="metrics-grid" aria-label="Сводка по источникам">
        <MetricCard label="Конфигурации" value={summary?.configurations ?? "—"} hint="структура и связанные корпуса" />
        <MetricCard label="Объекты метаданных" value={summary?.metadata_objects ?? "—"} hint="поиск, карточки и связи" />
        <MetricCard label="Корпуса кода" value={summary?.code_corpora ?? "—"} hint="основной код и расширения" />
        <MetricCard label="Справки платформы" value={summary?.reference_sources ?? "—"} hint="загруженные версии" />
      </section>

      <section className="section-card" aria-labelledby="contour-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Архитектурная граница</span>
            <h2 id="contour-title">Один владелец данных</h2>
          </div>
          <StatusBadge tone="info">API v1</StatusBadge>
        </div>
        <div className="contour-flow">
          <article>
            <ServerCog aria-hidden="true" />
            <span>01</span>
            <h3>MCP-сервер</h3>
            <p>Хранит Registry, индексы и выполняет все операции записи.</p>
          </article>
          <ArrowRight className="flow-arrow" aria-hidden="true" />
          <article>
            <Braces aria-hidden="true" />
            <span>02</span>
            <h3>HTTP API</h3>
            <p>Публикует согласованные снимки и статусы фоновых заданий.</p>
          </article>
          <ArrowRight className="flow-arrow" aria-hidden="true" />
          <article>
            <MonitorCog aria-hidden="true" />
            <span>03</span>
            <h3>React-интерфейс</h3>
            <p>Запоминает навигацию и визуализирует состояние, не читая data/.</p>
          </article>
        </div>
      </section>

      <section className="section-card" aria-labelledby="states-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Визуальный язык</span>
            <h2 id="states-title">Состояния должны различаться сразу</h2>
          </div>
        </div>
        <div className="state-samples">
          <article className="state-sample is-success">
            <StatusBadge tone="success">Готово</StatusBadge>
            <strong>Источник прочитан полностью</strong>
            <p>Спокойный зелёный используется только для подтверждённого результата.</p>
          </article>
          <article className="state-sample is-warning">
            <StatusBadge tone="warning">Внимание</StatusBadge>
            <strong>Есть ограничения покрытия</strong>
            <p>Янтарный сообщает, что нулевые значения нельзя трактовать как отсутствие.</p>
          </article>
          <article className="state-sample is-danger">
            <StatusBadge tone="danger">Ошибка</StatusBadge>
            <strong>Разбор остановлен</strong>
            <p>Красный закреплён за действием, которое не завершилось и требует решения.</p>
          </article>
        </div>
      </section>
    </div>
  );
}
