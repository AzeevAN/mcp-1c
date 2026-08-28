import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "./shell/AppShell";
import { OverviewPage } from "../pages/OverviewPage";
import { LoginPage } from "../pages/LoginPage";
import { PlaceholderPage } from "../pages/PlaceholderPage";
import { QueriesPage } from "../pages/QueriesPage";
import { SourcesPage } from "../pages/SourcesPage";

export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <OverviewPage /> },
      {
        path: "sources",
        element: <SourcesPage />,
      },
      {
        path: "queries",
        element: <QueriesPage />,
      },
      {
        path: "object",
        element: (
          <PlaceholderPage
            eyebrow="Следующий предметный срез"
            title="Карточка объекта"
            description="Адрес результата сохранён. Содержимое карточки будет перенесено после отдельной сверки классического пути."
          />
        ),
      },
      {
        path: "syntax",
        element: (
          <PlaceholderPage
            eyebrow="Следующий предметный срез"
            title="Карточка синтаксиса"
            description="Адрес результата сохранён. Содержимое карточки будет перенесено после отдельной сверки классического пути."
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
