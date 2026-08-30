import { createBrowserRouter } from "react-router-dom";

import { AppShell } from "./shell/AppShell";
import { OverviewPage } from "../pages/OverviewPage";
import { LoginPage } from "../pages/LoginPage";
import { QueriesPage } from "../pages/QueriesPage";
import { SourcesPage } from "../pages/SourcesPage";
import { CardPage } from "../pages/CardPage";
import { GraphPage } from "../pages/GraphPage";
import { DictionaryPage } from "../pages/DictionaryPage";
import { ReferencePage } from "../pages/ReferencePage";

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
        path: "reference",
        element: <ReferencePage />,
      },
      {
        path: "object",
        element: <CardPage kind="object" />,
      },
      {
        path: "syntax",
        element: <CardPage kind="syntax" />,
      },
      {
        path: "graph",
        element: <GraphPage />,
      },
      {
        path: "dictionary",
        element: <DictionaryPage />,
      },
    ],
  },
]);
