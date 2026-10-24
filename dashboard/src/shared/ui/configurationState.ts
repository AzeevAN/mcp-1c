import type { CodeCorpus, ConfigurationSource } from "../api/sources";
import type { StatusTone } from "./StatusBadge";

type StateView = { label: string; tone: StatusTone; detail: string };

export const corpusPhaseView: Record<CodeCorpus["phase"], { label: string; tone: StatusTone }> = {
  ready: { label: "Готово", tone: "success" },
  limited: { label: "С ограничениями", tone: "warning" },
  missing: { label: "Не загружен", tone: "info" },
};

export function structureOrigin(configuration: ConfigurationSource): string {
  if (configuration.source_origin === "source_a") return "Структура конфигурации";
  if (configuration.source_origin === "source_b") return "Полная выгрузка";
  return "Формат загрузки неизвестен";
}

export function activationStatus(configuration: ConfigurationSource): StateView {
  if (configuration.activation_status === "RELOAD_REQUIRED") return {
    label: "Требуется повторная загрузка", tone: "warning",
    detail: "Опубликованные данные требуют повторной загрузки полной выгрузки конфигурации. Перезапуск сервера не исправит это состояние.",
  };
  if (configuration.activation_status === "ACTIVE") return {
    label: "Активна", tone: "success", detail: "",
  };
  return { label: "Активация неизвестна", tone: "info", detail: "" };
}

export function mainCodeStatus(configuration: ConfigurationSource): string {
  // При барьере API скрывает корпуса: пустой список не доказывает отсутствия кода.
  if (configuration.activation_status === "RELOAD_REQUIRED") return "Недоступен до повторной загрузки";
  const corpus = configuration.corpora.find((item) => item.kind === "modules");
  return corpus ? corpusPhaseView[corpus.phase].label : corpusPhaseView.missing.label;
}

export function availableCodeCount(configuration: ConfigurationSource): number {
  if (configuration.activation_status === "RELOAD_REQUIRED") return 0;
  return configuration.corpora.filter((corpus) => corpus.phase === "ready" || corpus.phase === "limited").length;
}
