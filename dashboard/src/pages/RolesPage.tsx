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
  type RoleObjectSummary,
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
  if (state === "conditional_true") return "С ограничением RLS";
  return "Предоставлено";
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

function roleLabel(role: RoleDescriptor) {
  return role.label_ru
    || role.synonyms.find((item) => item.language.toLowerCase().startsWith("ru"))?.content
    || role.name;
}

function channelLabel(channel: DeclaredRightRow["channel"]) {
  if (channel === "programmatic") return "Доступ из кода";
  if (channel === "interactive") return "Действия в интерфейсе";
  return "Права платформы";
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
  const [detailRequest, setDetailRequest] = useState<RoleAccessRequest | null>(null);
  const detail = useRoleAccess(detailRequest);

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
  const operationLabels = useMemo(
    () => new Map(operationOptions.map((item) => [
      item.operation,
      `${item.label_ru} (${item.operation})`,
    ])),
    [operationOptions],
  );
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
    setDetailRequest(null);
    updateParams({ config, role: null, object: null });
  };

  const chooseRole = (role: string) => {
    setAccessCursor(undefined);
    setRestrictionRequest(null);
    setRestrictionContent("");
    setDetailRequest(null);
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

  const openObjectDetail = (
    object: RoleObjectSummary,
    requestedDetail: "children" | "audit",
  ) => {
    if (!effectiveConfig || !requestedRole) return;
    setDetailRequest({
      config: effectiveConfig,
      role: requestedRole,
      fullName: object.full_name,
      detail: requestedDetail,
    });
  };

  const continueObjectDetail = () => {
    const cursor = detail.data?.page?.next_cursor;
    if (!detailRequest || !cursor) return;
    setDetailRequest({ ...detailRequest, cursor });
  };

  const continueObjects = () => {
    const cursor = access.data?.page?.next_cursor;
    if (!cursor) return;
    setDetailRequest(null);
    setAccessCursor(cursor);
  };

  if (catalog.isPending) {
    return <section className="roles-page"><div className="section-card"><span className="loading-dot" />Проверяем снимок ролей…</div></section>;
  }

  if (catalog.isError || !catalog.data) {
    return <section className="roles-page"><div className="roles-unavailable" role="alert">{errorText(catalog.error)}</div></section>;
  }

  const data = catalog.data;
  const activeDescriptor = access.data?.role || selectedDescriptor;

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
                        {roleLabel(item)}
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
                {activeDescriptor && (
                  <div className="role-descriptor">
                    <strong>{roleLabel(activeDescriptor)}</strong>
                    <code>{activeDescriptor.name}</code>
                    <p>{activeDescriptor.comment || "Без комментария."}</p>
                    <span>UUID {activeDescriptor.uuid}</span>
                  </div>
                )}
              </aside>

              <div className="role-access-panel">
                {!requestedRole && (
                  <div className="roles-empty-state">
                    <FileLock2 size={30} aria-hidden="true" />
                    <strong>Выберите роль</strong>
                    <span>Сервер вернёт первую страницу объектов с доказанными правами.</span>
                  </div>
                )}
                {requestedRole && access.isPending && <div className="section-card"><span className="loading-dot" />Читаем страницу объектов…</div>}
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
                    <div className="role-object-list">
                      {(access.data.objects || []).map((object) => (
                        <article className="role-object-card" key={object.target}>
                          <header className="role-object-card-head">
                            <div>
                              <span className="role-object-kind">{object.kind_ru}</span>
                              <h3>{object.name}</h3>
                              <code>{object.full_name}</code>
                            </div>
                            {object.has_rls && (
                              <span className="role-object-rls">Есть ограничения RLS</span>
                            )}
                          </header>

                          <div className="role-right-groups">
                            {(["programmatic", "interactive", "platform"] as const).map((channel) => {
                              const rights = object.root_rights.filter((right) => right.channel === channel);
                              if (rights.length === 0) return null;
                              return (
                                <section className="role-right-group" key={channel}>
                                  <h4>{channelLabel(channel)}</h4>
                                  <div>
                                    {rights.map((right) => (
                                      <span className="role-right-chip" key={`${right.target}:${right.name}`}>
                                        <span>{right.label_ru}</span>
                                        <code>{right.name}</code>
                                        {right.state === "conditional_true" && (
                                          <span className={`role-right-state ${stateClass(right.state)}`}>
                                            {stateLabel(right.state)}
                                          </span>
                                        )}
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
                                      </span>
                                    ))}
                                  </div>
                                </section>
                              );
                            })}
                          </div>

                          {object.descendants.detail_available && (
                            <div className="role-object-descendants">
                              <span>Дочерних целей с правами: <strong>{object.descendants.targets_with_grants}</strong></span>
                              <span>Предоставленных прав: <strong>{object.descendants.granted_rights}</strong></span>
                              {object.descendants.conditional_rights > 0 && (
                                <span>С RLS: <strong>{object.descendants.conditional_rights}</strong></span>
                              )}
                            </div>
                          )}

                          <div className="role-object-actions">
                            {object.descendants.detail_available && (
                              <button
                                type="button"
                                onClick={() => openObjectDetail(object, "children")}
                              >
                                Показать детали {object.name}
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => openObjectDetail(object, "audit")}
                            >
                              Открыть технический аудит {object.name}
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                    <div className="roles-pagination">
                      <span>{pageLabel(access.data.page.offset, access.data.page.limit, access.data.page.returned, access.data.objects_total || 0)} объектов</span>
                      {access.data.page.next_cursor && (
                        <button
                          type="button"
                          onClick={continueObjects}
                        >
                          Следующая страница объектов<ChevronRight size={15} />
                        </button>
                      )}
                    </div>
                  </>
                )}
                {detailRequest && (
                  <section className="role-object-detail" aria-live="polite">
                    {detail.isPending && <span><span className="loading-dot" />Читаем детализацию объекта…</span>}
                    {detail.isError && <div className="roles-unavailable" role="alert">{errorText(detail.error)}</div>}
                    {detail.data?.state === "ready" && detail.data.object && detail.data.page && (
                      <>
                        <header className="role-detail-heading">
                          <div>
                            <span>{detail.data.mode === "audit" ? "Технический аудит" : "Дочерние права"}</span>
                            <h3>{detail.data.object.name}</h3>
                            <code>{detail.data.object.full_name}</code>
                          </div>
                          {detail.data.mode === "audit" && (
                            <StatusBadge tone="warning">Включая false</StatusBadge>
                          )}
                          <button type="button" onClick={() => setDetailRequest(null)}>
                            Закрыть детали
                          </button>
                        </header>
                        <div className="role-detail-list">
                          {(detail.data.rights || []).map((right) => (
                            <article className="role-detail-row" key={`${right.target}:${right.name}`}>
                              <div>
                                <span className="role-object-kind">{right.child_kind_ru || detail.data!.object!.kind_ru}</span>
                                <strong>{right.child_name || detail.data!.object!.name}</strong>
                                <code>{right.child_path || right.target}</code>
                              </div>
                              <div>
                                <strong>{right.label_ru}</strong>
                                <code>{right.name}</code>
                              </div>
                              {(detail.data!.mode === "audit" || right.state === "conditional_true") && (
                                <span className={`role-right-state ${stateClass(right.state)}`}>
                                  {stateLabel(right.state)}
                                </span>
                              )}
                              {right.restrictions.map((item) => (
                                <button
                                  type="button"
                                  aria-label="Показать RLS"
                                  key={item.ref}
                                  onClick={() => openRestriction(item.ref)}
                                >
                                  Показать RLS
                                </button>
                              ))}
                            </article>
                          ))}
                        </div>
                        <div className="roles-pagination">
                          <span>{pageLabel(detail.data.page.offset, detail.data.page.limit, detail.data.page.returned, detail.data.rights_total || 0)} прав</span>
                          {detail.data.page.next_cursor && (
                            <button type="button" onClick={continueObjectDetail}>
                              Следующая страница деталей<ChevronRight size={15} />
                            </button>
                          )}
                        </div>
                      </>
                    )}
                  </section>
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
                      <span>
                        {item.label_ru}
                        <small>{channelLabel(item.channel)} · <code>{item.operation} → {item.platform_right}</code></small>
                      </span>
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
                            <strong>{roleLabel(candidate.role)}</strong>
                            <code>{candidate.role.name}</code>
                            <StatusBadge tone={candidate.complete ? "success" : "warning"}>
                              {candidate.complete ? "Полное покрытие" : "Частичное покрытие"}
                            </StatusBadge>
                          </header>
                          <span>Даёт: {candidate.matched_operations.map((item) => operationLabels.get(item) || item).join(", ") || "—"}</span>
                          <span>Не хватает: {candidate.missing_operations.map((item) => operationLabels.get(item) || item).join(", ") || "—"}</span>
                          <span>Не предоставляет: {candidate.denied_operations.map((item) => operationLabels.get(item) || item).join(", ") || "—"}</span>
                          {candidate.conditional_operations.length > 0 && (
                            <span>Условно через RLS: {candidate.conditional_operations.map((item) => operationLabels.get(item) || item).join(", ")}</span>
                          )}
                          {candidate.has_rls && (
                            <span>Есть ограничения RLS; условия открываются отдельно из карточки роли.</span>
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
