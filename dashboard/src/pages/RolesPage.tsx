import {
  AlertTriangle,
  ChevronRight,
  FileLock2,
  Search,
  ShieldCheck,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  type DeclaredRightRow,
  type FindRolesRequest,
  type RestrictionRequest,
  type RoleAccessRequest,
  type RoleDescriptor,
  RolesApiError,
  useFindRoles,
  useRoleAccess,
  useRoleRestriction,
  useRolesCatalog,
} from "../shared/api/roles";
import { StatusBadge } from "../shared/ui/StatusBadge";

function errorText(error: unknown) {
  return error instanceof RolesApiError
    ? error.message
    : "Не удалось прочитать объявленные права ролей.";
}

function stateLabel(state: DeclaredRightRow["state"]) {
  if (state === "explicit_false") return "Явный false";
  if (state === "conditional_true") return "Условный true · RLS";
  return "Безусловный true";
}

function stateClass(state: DeclaredRightRow["state"]) {
  if (state === "explicit_false") return "is-denied";
  if (state === "conditional_true") return "is-conditional";
  return "is-allowed";
}

function defaultFlag(value: boolean | null) {
  if (value === null) return "не задано";
  return value ? "true" : "false";
}

function pageLabel(offset: number, limit: number, returned: number, total: number) {
  const page = Math.floor(offset / Math.max(limit, 1)) + 1;
  return `Страница ${page} · показано ${returned} из ${total}`;
}

export function RolesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedConfig = searchParams.get("config") || "";
  const mode = searchParams.get("mode") === "object" ? "object" : "role";
  const requestedRole = searchParams.get("role") || "";
  const [catalogCursor, setCatalogCursor] = useState<string | null>(null);
  const [roleOptions, setRoleOptions] = useState<RoleDescriptor[]>([]);
  const catalog = useRolesCatalog(requestedConfig, catalogCursor);
  const effectiveConfig = requestedConfig || catalog.data?.configuration || "";
  const [accessCursor, setAccessCursor] = useState<string | undefined>();
  const accessRequest: RoleAccessRequest | null = (
    catalog.data?.state === "ready" && effectiveConfig && requestedRole
  ) ? {
      config: effectiveConfig,
      role: requestedRole,
      cursor: accessCursor,
    } : null;
  const access = useRoleAccess(accessRequest);

  const [objectName, setObjectName] = useState(searchParams.get("object") || "");
  const [operations, setOperations] = useState<string[]>([]);
  const [includeConditional, setIncludeConditional] = useState(false);
  const [findRequest, setFindRequest] = useState<FindRolesRequest | null>(null);
  const found = useFindRoles(findRequest);

  const [restrictionRequest, setRestrictionRequest] = useState<RestrictionRequest | null>(null);
  const [restrictionContent, setRestrictionContent] = useState("");
  const restriction = useRoleRestriction(restrictionRequest);

  useEffect(() => {
    const page = restriction.data;
    if (!page?.content || !restrictionRequest) return;
    setRestrictionContent((current) => (
      restrictionRequest.cursor ? current + page.content : page.content || ""
    ));
  }, [restriction.data, restrictionRequest]);

  useEffect(() => {
    if (catalog.data?.state !== "ready") return;
    const incoming = catalog.data.roles || [];
    setRoleOptions((current) => {
      if (!catalogCursor) return incoming;
      const known = new Set(current.map((item) => item.uuid));
      return [...current, ...incoming.filter((item) => !known.has(item.uuid))];
    });
  }, [catalog.data, catalogCursor]);

  const visibleRoleOptions = roleOptions.length > 0
    ? roleOptions
    : catalog.data?.roles || [];
  const operationOptions = catalog.data?.operations || [];
  const selectedDescriptor = useMemo(
    () => visibleRoleOptions.find((item) => item.name === requestedRole),
    [requestedRole, visibleRoleOptions],
  );

  const updateParams = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    Object.entries(changes).forEach(([name, value]) => {
      if (value) next.set(name, value);
      else next.delete(name);
    });
    setSearchParams(next, { preventScrollReset: true });
  };

  const chooseConfiguration = (config: string) => {
    setCatalogCursor(null);
    setRoleOptions([]);
    setAccessCursor(undefined);
    setFindRequest(null);
    setRestrictionRequest(null);
    setRestrictionContent("");
    updateParams({ config, role: null, object: null });
  };

  const chooseRole = (role: string) => {
    setAccessCursor(undefined);
    setRestrictionRequest(null);
    setRestrictionContent("");
    updateParams({ role });
  };

  const chooseMode = (nextMode: "role" | "object") => {
    updateParams({ mode: nextMode === "object" ? "object" : null });
  };

  const toggleOperation = (operation: string) => {
    setOperations((current) => current.includes(operation)
      ? current.filter((item) => item !== operation)
      : [...current, operation]);
  };

  const submitFind = (event: FormEvent) => {
    event.preventDefault();
    if (!effectiveConfig || !objectName.trim() || operations.length === 0) return;
    const request = {
      config: effectiveConfig,
      fullName: objectName.trim(),
      operations,
      includeConditional,
    };
    setFindRequest(request);
    updateParams({ mode: "object", object: request.fullName });
  };

  const openRestriction = (ref: string) => {
    if (!effectiveConfig || !requestedRole) return;
    setRestrictionContent("");
    setRestrictionRequest({ config: effectiveConfig, role: requestedRole, ref });
  };

  const continueRestriction = () => {
    const cursor = restriction.data?.page?.next_cursor;
    if (!restrictionRequest || !cursor) return;
    setRestrictionRequest({ ...restrictionRequest, cursor });
  };

  if (catalog.isPending) {
    return <section className="roles-page"><div className="section-card"><span className="loading-dot" />Проверяем снимок ролей…</div></section>;
  }

  if (catalog.isError || !catalog.data) {
    return <section className="roles-page"><div className="roles-unavailable" role="alert">{errorText(catalog.error)}</div></section>;
  }

  const data = catalog.data;

  return (
    <section className="roles-page page-stack">
      <header className="roles-heading">
        <div>
          <span className="eyebrow">Read-only evidence</span>
          <h1>Роли и права</h1>
          <p>{data.disclaimer}</p>
        </div>
        <StatusBadge tone="success">Только чтение</StatusBadge>
      </header>

      <div className="roles-context-card">
        <label>
          <span>Конфигурация</span>
          <select
            aria-label="Конфигурация"
            value={effectiveConfig}
            onChange={(event) => chooseConfiguration(event.target.value)}
          >
            {data.state === "selection_required" && <option value="">Выберите конфигурацию</option>}
            {data.configuration_names.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        <div>
          <span>Generation</span>
          <code>{data.generation || "—"}</code>
        </div>
        <div>
          <span>SHA источника</span>
          <code title={data.source_sha256 || ""}>{data.source_sha256?.slice(0, 16) || "—"}</code>
        </div>
      </div>

      {data.state !== "ready" ? (
        <div className="roles-unavailable" role="alert">
          <AlertTriangle size={22} aria-hidden="true" />
          <div>
            <strong>{data.message || "Снимок ролей недоступен."}</strong>
            <span>Остальные слои конфигурации продолжают работать.</span>
          </div>
          <Link to="/sources">Открыть источники</Link>
        </div>
      ) : (
        <>
          <div className="roles-tabs" role="tablist" aria-label="Направление анализа">
            <button
              role="tab"
              aria-selected={mode === "role"}
              className={mode === "role" ? "is-active" : ""}
              type="button"
              onClick={() => chooseMode("role")}
            >
              Роль → доступы
            </button>
            <button
              role="tab"
              aria-selected={mode === "object"}
              className={mode === "object" ? "is-active" : ""}
              type="button"
              onClick={() => chooseMode("object")}
            >
              Объект → роли
            </button>
          </div>

          {mode === "role" ? (
            <div className="roles-workspace">
              <aside className="role-picker-card">
                <label>
                  <span>Роль</span>
                  <select
                    aria-label="Роль"
                    value={requestedRole}
                    onChange={(event) => chooseRole(event.target.value)}
                  >
                    <option value="">Выберите роль</option>
                    {visibleRoleOptions.map((item) => (
                      <option key={item.uuid} value={item.name}>
                        {item.synonyms[0]?.content || item.name}
                      </option>
                    ))}
                  </select>
                </label>
                <small>Ролей в снимке: {data.roles_total || 0}. Список и права выдаются страницами.</small>
                {data.page?.next_cursor && (
                  <button
                    type="button"
                    disabled={catalog.isFetching}
                    onClick={() => setCatalogCursor(data.page?.next_cursor || null)}
                  >
                    {catalog.isFetching ? "Читаем роли…" : "Загрузить ещё роли"}
                  </button>
                )}
                {selectedDescriptor && (
                  <div className="role-descriptor">
                    <strong>{selectedDescriptor.synonyms[0]?.content || selectedDescriptor.name}</strong>
                    <code>{selectedDescriptor.name}</code>
                    <p>{selectedDescriptor.comment || "Без комментария."}</p>
                    <span>UUID {selectedDescriptor.uuid}</span>
                  </div>
                )}
              </aside>

              <div className="role-access-panel">
                {!requestedRole && (
                  <div className="roles-empty-state">
                    <FileLock2 size={30} aria-hidden="true" />
                    <strong>Выберите роль</strong>
                    <span>Сервер вернёт только первую страницу объявленных прав.</span>
                  </div>
                )}
                {requestedRole && access.isPending && <div className="section-card"><span className="loading-dot" />Читаем страницу прав…</div>}
                {requestedRole && access.isError && <div className="roles-unavailable" role="alert">{errorText(access.error)}</div>}
                {access.data?.state === "ready" && access.data.role && access.data.page && (
                  <>
                    <div className="role-evidence-note">
                      <ShieldCheck size={19} aria-hidden="true" />
                      <span>
                        <strong>Default-флаги — свидетельство, не вычисленный доступ.</strong>
                        Resolver не подмешивает недоказанное наследование.
                      </span>
                    </div>
                    <div className="role-flags">
                      <span>Новые объекты: <strong>{defaultFlag(access.data.role.default_flags.set_for_new_objects)}</strong></span>
                      <span>Реквизиты по умолчанию: <strong>{defaultFlag(access.data.role.default_flags.set_for_attributes_by_default)}</strong></span>
                      <span>Независимые дочерние: <strong>{defaultFlag(access.data.role.default_flags.independent_rights_of_child_objects)}</strong></span>
                    </div>
                    <div className="role-rights-list">
                      {(access.data.rights || []).map((right) => (
                        <article key={`${right.target}:${right.name}`}>
                          <div>
                            <code>{right.target}</code>
                            <strong>{right.name}</strong>
                          </div>
                          <span className={`role-right-state ${stateClass(right.state)}`}>
                            {stateLabel(right.state)}
                          </span>
                          {right.restrictions.map((item) => (
                            <button
                              type="button"
                              aria-label="Показать RLS"
                              key={item.ref}
                              onClick={() => openRestriction(item.ref)}
                            >
                              Показать RLS
                              <small>{item.chars.toLocaleString("ru-RU")} символов</small>
                            </button>
                          ))}
                        </article>
                      ))}
                    </div>
                    <div className="roles-pagination">
                      <span>{pageLabel(access.data.page.offset, access.data.page.limit, access.data.page.returned, access.data.rights_total || 0)}</span>
                      {access.data.page.next_cursor && (
                        <button
                          type="button"
                          onClick={() => setAccessCursor(access.data!.page!.next_cursor || undefined)}
                        >
                          Следующая страница прав<ChevronRight size={15} />
                        </button>
                      )}
                    </div>
                  </>
                )}
                {restrictionRequest && (
                  <section className="role-restriction-card" aria-live="polite">
                    <header>
                      <div><span>Явно открытое условие</span><strong>RLS</strong></div>
                      <small>Читается окнами до 2 000 символов</small>
                    </header>
                    {restriction.isPending && <span>Читаем окно RLS…</span>}
                    {restriction.isError && <div role="alert">{errorText(restriction.error)}</div>}
                    {restriction.data?.fields && restriction.data.fields.length > 0 && (
                      <div className="role-restriction-fields">
                        <strong>Поля ограничения</strong>
                        <ul>
                          {restriction.data.fields.map((field, index) => (
                            <li key={`${index}:${field}`}><code>{field}</code></li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {restriction.data?.total_chars === 0 && (
                      <p className="role-restriction-empty">Условие RLS пустое.</p>
                    )}
                    {restrictionContent && <pre>{restrictionContent}</pre>}
                    {restriction.data?.page?.next_cursor && (
                      <button type="button" onClick={continueRestriction}>Дочитать RLS</button>
                    )}
                  </section>
                )}
              </div>
            </div>
          ) : (
            <div className="role-object-mode">
              <form className="role-find-form" onSubmit={submitFind}>
                <label>
                  <span>Полное имя объекта</span>
                  <input
                    aria-label="Полное имя объекта"
                    value={objectName}
                    onChange={(event) => setObjectName(event.target.value)}
                    placeholder="Справочник.Заказы"
                    required
                  />
                </label>
                <fieldset>
                  <legend>Операции и точные права платформы</legend>
                  {operationOptions.map((item) => (
                    <label key={item.operation}>
                      <input
                        type="checkbox"
                        aria-label={`${item.operation} → ${item.platform_right}`}
                        checked={operations.includes(item.operation)}
                        onChange={() => toggleOperation(item.operation)}
                      />
                      <span>{item.operation} → <code>{item.platform_right}</code></span>
                    </label>
                  ))}
                </fieldset>
                <label className="role-conditional-toggle">
                  <input
                    type="checkbox"
                    checked={includeConditional}
                    onChange={(event) => setIncludeConditional(event.target.checked)}
                  />
                  Учитывать условные RLS-кандидаты
                </label>
                <button type="submit" disabled={!objectName.trim() || operations.length === 0}>
                  <Search size={16} aria-hidden="true" />Найти роли
                </button>
              </form>

              <div className="role-candidates-panel">
                {findRequest && found.isPending && <div className="section-card"><span className="loading-dot" />Resolver проверяет объявленные права…</div>}
                {findRequest && found.isError && <div className="roles-unavailable" role="alert">{errorText(found.error)}</div>}
                {found.data?.state === "ready" && (
                  <>
                    <div className="role-resolution-summary">
                      <span>Точная цель: <code>{found.data.source_target}</code></span>
                      <span>Условных кандидатов исключено: {found.data.conditional_candidates_excluded || 0}</span>
                      {found.data.minimal_role_set ? (
                        <div>
                          <small>Доказанный минимальный набор</small>
                          <strong>{found.data.minimal_role_set.roles.join(" + ")}</strong>
                          <span>Доказательство: {found.data.minimal_role_set.proof}</span>
                        </div>
                      ) : (
                        <div><strong>Полное доказанное покрытие не найдено</strong></div>
                      )}
                    </div>
                    <div className="role-candidates-list">
                      {(found.data.candidates || []).map((candidate) => (
                        <article key={candidate.role.uuid}>
                          <header>
                            <strong>{candidate.role.synonyms[0]?.content || candidate.role.name}</strong>
                            <code>{candidate.role.name}</code>
                            <StatusBadge tone={candidate.complete ? "success" : "warning"}>
                              {candidate.complete ? "Полное покрытие" : "Частичное покрытие"}
                            </StatusBadge>
                          </header>
                          <span>Даёт: {candidate.matched_operations.join(", ") || "—"}</span>
                          <span>Не хватает: {candidate.missing_operations.join(", ") || "—"}</span>
                          <span>Явный false: {candidate.denied_operations.join(", ") || "—"}</span>
                          {candidate.conditional_operations.length > 0 && (
                            <span>Условно через RLS: {candidate.conditional_operations.join(", ")}</span>
                          )}
                        </article>
                      ))}
                    </div>
                    {found.data.page?.next_cursor && (
                      <button
                        className="role-next-candidates"
                        type="button"
                        onClick={() => setFindRequest((current) => current ? ({
                          ...current,
                          cursor: found.data!.page!.next_cursor || undefined,
                        }) : current)}
                      >
                        Следующая страница кандидатов
                      </button>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
