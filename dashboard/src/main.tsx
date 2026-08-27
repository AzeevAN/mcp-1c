import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router-dom";

import { AppProviders } from "./app/AppProviders";
import { router } from "./app/router";
import "./styles/global.css";

const root = document.getElementById("root");

if (!root) {
  throw new Error("Корневой элемент приложения не найден.");
}

createRoot(root).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
);
