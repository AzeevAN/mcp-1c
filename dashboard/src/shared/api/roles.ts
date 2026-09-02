import { useQuery } from "@tanstack/react-query";

export type RoleState = "ready" | "missing" | "error" | "selection_required";

export type RoleDescriptor = {
  uuid: string;
  name: string;
  synonyms: Array<{ language: string; content: string }>;
  comment: string;
  comment_truncated: boolean;
  xml_version: string;
  default_flags: {
    set_for_new_objects: boolean;
    set_for_attributes_by_default: boolean;
    independent_rights_of_child_objects: boolean;
    resolver_effect: "evidence_only";
  };
};

type Page = {
  offset: number;
  limit: number;
  returned: number;
  next_cursor: string | null;
};

type RoleBase = {
  api_version: "v1";
  state: RoleState;
  configuration: string | null;
  generation: string | null;
  source_sha256: string | null;
  declaration_scope: "declared_role_rights";
  disclaimer: string;
  message?: string;
};

export type RolesCatalog = RoleBase & {
  configuration_names: string[];
  operations?: Array<{ operation: string; platform_right: string }>;
  roles_total?: number;
  roles?: RoleDescriptor[];
  page?: Page;
};

export type DeclaredRightRow = {
  target: string;
  name: string;
  value: boolean;
  state: "explicit_false" | "unconditional_true" | "conditional_true";
  restrictions: Array<{
    field: string;
    chars: number;
    bytes: number;
    ref: string;
  }>;
};

export type RoleAccessResponse = RoleBase & {
  mode?: "rights" | "templates";
  role?: RoleDescriptor;
  target?: string | null;
  rights_total?: number;
  rights?: DeclaredRightRow[];
  templates_total?: number;
  templates?: Array<{ name: string; chars: number; bytes: number; ref: string }>;
  page?: Page;
  templates_page?: Page;
};

export type RoleCandidate = {
  role: RoleDescriptor;
  complete: boolean;
  matched_operations: string[];
  missing_operations: string[];
  conditional_operations: string[];
  denied_operations: string[];
  matched_rights: Array<{
    target: string;
    name: string;
    value: boolean;
    state: "unconditional_true" | "conditional_true";
  }>;
};

export type FindRolesResponse = RoleBase & {
  source_target?: string;
  checked_rights?: Array<{ operation: string; platform_right: string }>;
  include_conditional?: boolean;
  conditional_candidates_excluded?: number;
  candidates_total?: number;
  candidates?: RoleCandidate[];
  minimal_role_set?: null | { roles: string[]; proof: string };
  warnings?: string[];
  page?: Page;
};

export type RoleRestrictionResponse = RoleBase & {
  mode?: "restriction" | "template";
  role?: string;
  restriction_ref?: string;
  field?: string;
  template?: string;
  target?: string;
  right?: string;
  content?: string;
  total_chars?: number;
  total_bytes?: number;
  page?: {
    offset: number;
    max_chars: number;
    returned_chars: number;
    next_cursor: string | null;
  };
};

export class RolesApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function roleJson<T extends { state?: RoleState; error?: string }>(url: string): Promise<T> {
  const response = await fetch(url);
  const payload = await response.json() as T;
  // missing/error/selection_required — штатные состояния страницы, даже
  // если HTTP подчёркивает их кодом 409.
  if (!response.ok && !payload.state) {
    throw new RolesApiError(payload.error || `Сервер ответил ${response.status}.`, response.status);
  }
  return payload;
}

export function useRolesCatalog(config: string, cursor: string | null = null) {
  return useQuery({
    queryKey: ["roles", "catalog", config, cursor],
    queryFn: () => {
      const params = new URLSearchParams({ limit: "50" });
      if (config) params.set("config", config);
      if (cursor) params.set("cursor", cursor);
      return roleJson<RolesCatalog>(`/api/v1/roles?${params}`);
    },
  });
}

export type RoleAccessRequest = {
  config: string;
  role: string;
  cursor?: string;
  fullName?: string;
};

export function useRoleAccess(request: RoleAccessRequest | null) {
  return useQuery({
    queryKey: ["roles", "access", request],
    enabled: request !== null,
    queryFn: () => {
      const params = new URLSearchParams({
        config: request!.config,
        role: request!.role,
        limit: "50",
      });
      if (request!.cursor) params.set("cursor", request!.cursor);
      if (request!.fullName) params.set("full_name", request!.fullName);
      return roleJson<RoleAccessResponse>(`/api/v1/roles/access?${params}`);
    },
  });
}

export type FindRolesRequest = {
  config: string;
  fullName: string;
  operations: string[];
  includeConditional: boolean;
  cursor?: string;
};

export function useFindRoles(request: FindRolesRequest | null) {
  return useQuery({
    queryKey: ["roles", "find", request],
    enabled: request !== null,
    queryFn: () => {
      const params = new URLSearchParams({
        config: request!.config,
        full_name: request!.fullName,
        limit: "10",
      });
      request!.operations.forEach((operation) => params.append("operation", operation));
      if (request!.includeConditional) params.set("include_conditional", "1");
      if (request!.cursor) params.set("cursor", request!.cursor);
      return roleJson<FindRolesResponse>(`/api/v1/roles/find?${params}`);
    },
  });
}

export type RestrictionRequest = {
  config: string;
  role: string;
  ref: string;
  cursor?: string;
};

export function useRoleRestriction(request: RestrictionRequest | null) {
  return useQuery({
    queryKey: ["roles", "restriction", request],
    enabled: request !== null,
    queryFn: () => {
      const params = new URLSearchParams({
        config: request!.config,
        role: request!.role,
        restriction_ref: request!.ref,
        max_chars: "2000",
      });
      if (request!.cursor) params.set("cursor", request!.cursor);
      return roleJson<RoleRestrictionResponse>(`/api/v1/roles/restriction?${params}`);
    },
  });
}
