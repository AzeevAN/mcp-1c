import {
  BookOpenText,
  Boxes,
  ChevronLeft,
  CircleGauge,
  DatabaseZap,
  GitBranch,
  LogIn,
  LogOut,
  Library,
  SearchCode,
  ShieldCheck,
} from "lucide-react";
import { Link, Navigate, NavLink, Outlet, useLocation } from "react-router-dom";

import { useUiStore } from "../../store/uiStore";
import { DashboardApiError, useBootstrap } from "../../shared/api/bootstrap";

const navigation = [
  { to: "/", label: "Обзор", icon: CircleGauge, end: true },
  { to: "/sources", label: "Источники", icon: DatabaseZap },
  { to: "/queries", label: "Запросы", icon: SearchCode },
  { to: "/reference", label: "Общая справка", icon: Library },
  { to: "/graph", label: "Связи", icon: GitBranch },
  { to: "/dictionary", label: "Словарь", icon: BookOpenText },
];

export function AppShell() {
  const compact = useUiStore((state) => state.sidebarCompact);
  const toggleSidebar = useUiStore((state) => state.toggleSidebar);
  const bootstrap = useBootstrap();
  const location = useLocation();
  const currentPath = location.pathname + location.search;

  if (bootstrap.isPending) {
    return (
      <main className="auth-gate" aria-live="polite">
        <span className="loading-dot" />Проверяем доступ к дашборду…
      </main>
    );
  }

  if (bootstrap.error instanceof DashboardApiError && bootstrap.error.status === 401) {
    return <Navigate to={`/login?next=${encodeURIComponent(currentPath)}`} replace />;
  }

  if (bootstrap.isError) {
    return (
      <main className="auth-gate is-error">
        Не удалось проверить доступ. Обновите страницу после восстановления сервера.
      </main>
    );
  }

  const online = true;
  const admin = bootstrap.data.permissions.admin;
  const sessionLevel = bootstrap.data.authentication.session_level;
  const loginTarget = `/login?next=${encodeURIComponent(currentPath)}`;

  return (
    <div className={compact ? "app-shell is-compact" : "app-shell"}>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            M1
          </span>
          <span className="brand-copy">
            <strong>mcp-1c</strong>
            <small>центр конфигураций</small>
          </span>
        </div>

        <nav className="primary-nav" aria-label="Основная навигация">
          {navigation.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} title={compact ? label : undefined}>
              <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className="server-mini-card">
            <Boxes size={18} aria-hidden="true" />
            <span>
              <strong>{online ? "MCP работает" : "Проверяем MCP"}</strong>
              <small>{bootstrap.data?.server.version ?? "соединение…"}</small>
            </span>
          </div>
          <button
            className="sidebar-toggle"
            type="button"
            onClick={toggleSidebar}
            aria-label={compact ? "Развернуть меню" : "Свернуть меню"}
          >
            <ChevronLeft size={18} aria-hidden="true" />
            <span>Свернуть меню</span>
          </button>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="topbar-kicker">Рабочий контур</span>
            <strong>Данные MCP-сервера</strong>
          </div>
          <div className="topbar-actions">
            <div className="connection-badge is-online">
              <span aria-hidden="true" />На связи
            </div>
            <div className={admin ? "access-badge is-admin" : "access-badge"}>
              <ShieldCheck size={16} aria-hidden="true" />
              {admin ? "Администратор" : "Только чтение"}
            </div>
            {!admin && bootstrap.data.authentication.admin_available && (
              <Link className="session-action" to={loginTarget}>
                <LogIn size={16} aria-hidden="true" />Войти как администратор
              </Link>
            )}
            {sessionLevel && (
              <form method="post" action="/logout" className="logout-form">
                <button className="session-action" type="submit">
                  <LogOut size={16} aria-hidden="true" />Выйти
                </button>
              </form>
            )}
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
