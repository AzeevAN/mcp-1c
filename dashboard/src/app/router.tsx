import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "./shell/AppShell";
import { OverviewPage } from "../pages/OverviewPage";
import { LoginPage } from "../pages/LoginPage";
import { PlaceholderPage } from "../pages/PlaceholderPage";

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      {
        path: "sources",
        element: (
          <PlaceholderPage
            eyebrow="Следующий этап"
            title="Источники"
            description="Состав конфигурации, корпуса кода, загрузка и диагностические журналы будут перенесены после отдельного разбора текущего пути."
          />
        ),
      },
      {
        path: "queries",
        element: (
          <PlaceholderPage
            eyebrow="Запланировано"
            title="Запросы"
            description="Проверка поисковых формулировок останется связана с теми же индексами MCP."
          />
        ),
      },
      {
        path: "graph",
        element: (
          <PlaceholderPage
            eyebrow="Запланировано"
            title="Связи"
            description="Интерактивный граф получит собственное согласование раскладки, фильтров и состояний."
          />
        ),
      },
      {
        path: "dictionary",
        element: (
          <PlaceholderPage
            eyebrow="Запланировано"
            title="Словарь"
            description="Встроенные и локальные правила останутся разделены по происхождению."
          />
        ),
      },
    ],
  },
]);
