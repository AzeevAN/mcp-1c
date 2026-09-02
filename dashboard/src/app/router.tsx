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
import { ReferenceItemPage } from "../pages/ReferenceItemPage";
import { RolesPage } from "../pages/RolesPage";

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
        path: "reference/item",
        element: <ReferenceItemPage />,
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
        path: "roles",
        element: <RolesPage />,
      },
      {
        path: "dictionary",
        element: <DictionaryPage />,
      },
    ],
  },
]);
