import { useQuery } from "@tanstack/react-query";

import type { ReferenceAdminState } from "./sourceAdmin";

export type ReferenceSearchParams = {
  query: string;
  domain?: string;
  kind?: string;
  platform?: string;
  include_explicit?: boolean;
  include_hidden?: boolean;
  limit?: number;
};

export type ReferenceAvailability = {
  status: "available" | "unavailable" | "unknown";
  platform: string | null;
  reason: string;
  introduced: string | null;
  removed: string | null;
  known_present_in: string | null;
  evidence: Array<Record<string, string>>;
};

export type ReferenceHit = {
  id: string;
  matched_section_id: string | null;
  domain: string;
  kind: string;
  title_ru: string;
  title_en: string;
  signature: string;
  access_scope: string;
  availability: ReferenceAvailability;
  score: number;
  reason: string;
};

export type ReferenceSearchResponse = {
  query: string;
  domain: string | null;
  kind: string | null;
  platform: string | null;
  results: ReferenceHit[];
  has_more: boolean;
  unavailable_matches: ReferenceHit[];
};

export type ReferenceItemParams = {
  item_id: string;
  section_id?: string;
  cursor?: string;
  platform?: string;
  max_chars?: number;
};

export type ReferenceItemResponse = {
  card: {
    id: string;
    section_id: string | null;
    domain: string;
    kind: string;
    title_ru: string;
    title_en: string;
    source_key: string;
    source_path: string;
  };
  availability: ReferenceAvailability;
  content_format: "markdown";
  content: string;
  html: string;
  continuation: {
    offset: number;
    next_offset: number;
    total_chars: number;
    next_cursor: string | null;
  };
};

export class ReferenceApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function responseJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({})) as T & { error?: string };
  if (!response.ok) {
    throw new ReferenceApiError(
      payload.error || `Сервер ответил ${response.status}.`,
      response.status,
    );
  }
  return payload;
}

function queryString(values: Record<string, string | number | boolean | undefined>) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(values)) {
    if (value === undefined || value === "" || value === false) continue;
    params.set(name, value === true ? "1" : String(value));
  }
  return params.toString();
}

export function useReferenceStatus(enabled = true) {
  return useQuery({
    queryKey: ["reference", "status"],
    enabled,
    queryFn: async () => responseJson<ReferenceAdminState>(
      await fetch("/api/v1/reference"),
    ),
  });
}

export function useReferenceSearch(params: ReferenceSearchParams | null) {
  return useQuery({
    queryKey: ["reference", "search", params],
    queryFn: async () => responseJson<ReferenceSearchResponse>(
      await fetch(`/api/v1/reference/search?${queryString(params ?? {})}`),
    ),
    enabled: Boolean(params?.query),
  });
}

export function useReferenceItem(params: ReferenceItemParams | null) {
  return useQuery({
    queryKey: ["reference", "item", params],
    queryFn: async () => responseJson<ReferenceItemResponse>(
      await fetch(`/api/v1/reference/item?${queryString(params ?? {})}`),
    ),
    enabled: Boolean(params?.item_id),
  });
}
