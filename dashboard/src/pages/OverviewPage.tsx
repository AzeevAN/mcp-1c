import {
  ArrowRight,
  Database,
  Network,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { useBootstrap } from "../shared/api/bootstrap";
import { getCapabilities } from "../shared/api/capabilities";
import { useSources } from "../shared/api/sources";
import { MetricCard } from "../shared/ui/MetricCard";
import { StatusBadge, type StatusTone } from "../shared/ui/StatusBadge";

type AttentionItem = {
  id: string;
  title: string;
  detail: string;
  to: string;
};

function formatTokens(value: number): string {
  return `≈ ${new Intl.NumberFormat("ru-RU").format(value)}`;
}

export function OverviewPage() {
  const bootstrap = useBootstrap();
  const sources = useSources();
  const isAdmin = bootstrap.data?.permissions.admin === true;
  const capabilities = useQuery({
    queryKey: ["capabilities"],
    queryFn: getCapabilities,
    enabled: isAdmin,
  });
  const capabilityData = isAdmin ? capabilities.data : undefined;
  const summary = bootstrap.data?.summary;
  const configurations = sources.data?.configurations ?? [];
  const codeCorpora = sources.data
    ? configurations.reduce(
      (total, configuration) => total + configuration.corpora.filter((corpus) => corpus.phase !== "missing").length,
      0,
    )
    : summary?.code_corpora;
  const modules = capabilityData?.modules ?? [];
  const activeModules = modules.filter((module) => module.active);
  const toolCount = activeModules.reduce((total, module) => total + (module.tool_count ?? 0), 0);
  const contextTokens = activeModules.reduce((total, module) => total + (module.approx_tokens ?? 0), 0);
  const allToolCountsMeasured = activeModules.every((module) => module.tool_count !== null);
  const allContextMeasured = capabilityData !== undefined && activeModules.every((module) => module.approx_tokens !== null);
  const toolCountLabel = capabilityData
    ? allToolCountsMeasured ? String(toolCount) : toolCount ? `≥ ${toolCount}` : "не измерено"
    : "—";
  const contextLabel = capabilityData
    ? allContextMeasured ? formatTokens(contextTokens) : contextTokens ? `≥ ${new Intl.NumberFormat("ru-RU").format(contextTokens)}` : "не измерено"
    : "—";

  const attention: AttentionItem[] = [];
  if (capabilityData?.pending_restart) {
    attention.push({
      id: "capabilities-restart",
      title: "Настройки модулей ждут перезапуска",
      detail: "Сохранённый набор инструментов пока не совпадает с текущим процессом MCP.",
      to: "/capabilities",
    });
  }
  for (const configuration of configurations) {
    if (configuration.activation_status === "RELOAD_REQUIRED") {
      attention.push({
        id: `${configuration.id}:reload`,
        title: `${configuration.id}: требуется повторная активация`,
        detail: "Текущий источник нельзя считать полностью опубликованным.",
        to: "/sources",
      });
    }
    if (!configuration.platform || configuration.platform === "unknown") {
      attention.push({
        id: `${configuration.id}:platform`,
        title: `${configuration.id}: версия платформы неизвестна`,
        detail: "Фильтрация справки по доступности версий может быть неполной.",
        to: "/sources",
      });
    }
    const problemCorpora = configuration.corpora.filter((corpus) =>
      ["limited", "error"].includes(corpus.phase),
    );
    if (problemCorpora.length) {
      attention.push({
        id: `${configuration.id}:corpora`,
        title: `${configuration.id}: есть ограничения корпусов кода`,
        detail: `${problemCorpora.length} ${problemCorpora.length === 1 ? "корпус требует" : "корпуса требуют"} проверки покрытия или ошибки разбора.`,
        to: "/sources",
      });
    }
    const warnings = [
      ...configuration.notes,
      ...(configuration.source?.warnings ?? []),
      ...configuration.corpora.flatMap((corpus) => corpus.source?.warnings ?? []),
    ];
    if (configuration.source?.incomplete || warnings.length) {
      attention.push({
        id: `${configuration.id}:warnings`,
        title: `${configuration.id}: источник загружен с ограничениями`,
        detail: warnings.length
          ? `Зафиксировано предупреждений: ${warnings.length}.`
          : "Источник помечен как неполный.",
        to: "/sources",
      });
    }
  }
  const referenceWarnings = (sources.data?.references ?? []).flatMap((reference) => reference.warnings);
  if ((sources.data?.references ?? []).some((reference) => reference.incomplete) || referenceWarnings.length) {
    attention.push({
      id: "reference-sources",
      title: "Справка платформы загружена с ограничениями",
      detail: referenceWarnings.length
        ? `Зафиксировано предупреждений: ${referenceWarnings.length}.`
        : "Один из источников справки помечен как неполный.",
      to: "/sources",
    });
  }

  const hasApiError = bootstrap.isError || sources.isError || (isAdmin && capabilities.isError);
  const isChecking = bootstrap.isLoading || sources.isLoading || (isAdmin && capabilities.isLoading);
  let readinessTone: StatusTone = "success";
  let readinessLabel = "Система готова";
  let readinessText = "Источники опубликованы, отложенных перезапусков и подтверждённых ошибок нет.";
  if (hasApiError) {
    readinessTone = "danger";
    readinessLabel = "Часть данных недоступна";
    readinessText = "Не удалось получить полное состояние сервера. Проверьте доступность API.";
  } else if (capabilityData?.pending_restart) {
    readinessTone = "warning";
    readinessLabel = "Требуется перезапуск";
    readinessText = "Настройки модулей сохранены, но ещё не применены текущим процессом MCP.";
  } else if (attention.length) {
    readinessTone = "warning";
    readinessLabel = "Требует внимания";
    readinessText = `Найдено состояний, которые могут ограничивать ответы агентам: ${attention.length}.`;
  } else if (isChecking) {
    readinessTone = "info";
    readinessLabel = "Проверяем состояние";
    readinessText = "Получаем актуальные сведения об источниках и доступных инструментах.";
  }

  return (
    <div className="page-stack overview-page">
      <section className="hero-panel overview-hero" aria-labelledby="overview-title">
        <div>
          <span className="eyebrow">Обзор системы</span>
          <h1 id="overview-title">Центр конфигураций</h1>
          <p>{readinessText}</p>
        </div>
        <StatusBadge tone={readinessTone}>{readinessLabel}</StatusBadge>
      </section>

      <section className="metrics-grid" aria-label="Сводка по источникам">
        <MetricCard label="Конфигурации" value={summary?.configurations ?? "—"} hint="опубликованные структуры" />
        <MetricCard label="Объекты метаданных" value={summary?.metadata_objects ?? "—"} hint="поиск, карточки и связи" />
        <MetricCard label="Корпуса кода" value={codeCorpora ?? "—"} hint="основной код и расширения" />
        <MetricCard label="Версии справки платформы" value={summary?.reference_sources ?? "—"} hint="для поиска по синтаксису" />
      </section>

      <section className="section-card overview-agent-card" aria-labelledby="agent-access-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Доступ агентов</span>
            <h2 id="agent-access-title">Текущий MCP-контракт</h2>
          </div>
          {isAdmin ? (
            <Link className="overview-heading-link" to="/capabilities">
              Настроить модули <ArrowRight size={16} aria-hidden="true" />
            </Link>
          ) : (
            <span className="overview-heading-link">Только для администратора</span>
          )}
        </div>
        <div className="overview-agent-metrics">
          <div><span>Активные модули</span><strong>{capabilityData ? activeModules.length : "—"}</strong></div>
          <div><span>Инструменты модулей</span><strong>{toolCountLabel}</strong></div>
          <div><span>Контекст модулей</span><strong>{contextLabel}</strong><small>{allContextMeasured ? "токенов при старте сессии" : "не все модули измерены"}</small></div>
        </div>
        <div className="overview-module-list" aria-label="Активные дополнительные модули">
          {!isAdmin ? (
            <span className="is-muted">Сведения о модулях доступны администратору</span>
          ) : activeModules.length ? activeModules.map((module) => (
            <span key={module.id}>{module.display_name}</span>
          )) : (
            <span className="is-muted">Дополнительные модули отключены</span>
          )}
        </div>
      </section>

      <section className="section-card" aria-labelledby="attention-title">
        <div className="section-heading">
          <div>
            <span className="eyebrow">Контроль состояния</span>
            <h2 id="attention-title">Требует внимания</h2>
          </div>
          <StatusBadge tone={hasApiError ? "danger" : attention.length ? "warning" : "success"}>
            {hasApiError ? "Нет полной картины" : attention.length ? `${attention.length} замечаний` : "Всё готово"}
          </StatusBadge>
        </div>
        {hasApiError ? (
          <div className="overview-attention-empty is-error">
            Состояние источников или модулей недоступно. Обновите страницу после восстановления API.
          </div>
        ) : isChecking ? (
          <div className="overview-attention-empty"><span className="loading-dot" />Проверяем источники и модули…</div>
        ) : attention.length ? (
          <div className="overview-attention-list">
            {attention.map((item) => (
              <Link key={item.id} to={item.to}>
                <span><strong>{item.title}</strong><small>{item.detail}</small></span>
                <ArrowRight size={18} aria-hidden="true" />
              </Link>
            ))}
          </div>
        ) : (
          <div className="overview-attention-empty is-success">
            Отложенных перезапусков, ошибок активации и подтверждённых ограничений источников нет.
          </div>
        )}
      </section>

      {configurations.length > 0 && (
        <section className="section-card" aria-labelledby="configuration-state-title">
          <div className="section-heading">
            <div>
              <span className="eyebrow">Опубликованные данные</span>
              <h2 id="configuration-state-title">Состояние конфигураций</h2>
            </div>
            <Link className="overview-heading-link" to="/sources">
              Все источники <ArrowRight size={16} aria-hidden="true" />
            </Link>
          </div>
          <div className="overview-configuration-list">
            {configurations.map((configuration) => {
              const tone: StatusTone = configuration.activation_status === "ACTIVE" ? "success" : "warning";
              const status = configuration.activation_status === "ACTIVE"
                ? "Активна"
                : configuration.activation_status === "RELOAD_REQUIRED"
                  ? "Нужна активация"
                  : "Статус неизвестен";
              return (
                <article key={configuration.id}>
                  <div className="overview-configuration-title">
                    <strong>{configuration.id}</strong>
                    <StatusBadge tone={tone}>{status}</StatusBadge>
                  </div>
                  <dl>
                    <div><dt>Режим</dt><dd>{configuration.activation_mode}</dd></div>
                    <div><dt>Платформа</dt><dd>{configuration.platform || "unknown"}</dd></div>
                    <div><dt>Объекты</dt><dd>{new Intl.NumberFormat("ru-RU").format(configuration.objects)}</dd></div>
                    <div><dt>Код</dt><dd>{configuration.corpora.filter((corpus) => corpus.phase !== "missing").length}</dd></div>
                  </dl>
                </article>
              );
            })}
          </div>
        </section>
      )}

      <section className="overview-quick-actions" aria-label="Быстрые действия">
        <Link to="/queries"><Search aria-hidden="true" /><span><strong>Проверить запрос</strong><small>Поиск глазами агента</small></span></Link>
        <Link to="/graph"><Network aria-hidden="true" /><span><strong>Открыть связи</strong><small>Граф объектов метаданных</small></span></Link>
        <Link to="/sources"><Database aria-hidden="true" /><span><strong>Управлять источниками</strong><small>Загрузка и диагностика</small></span></Link>
        {isAdmin && (
          <Link to="/capabilities"><SlidersHorizontal aria-hidden="true" /><span><strong>Настроить модули</strong><small>Инструменты и контекст</small></span></Link>
        )}
      </section>
    </div>
  );
}
