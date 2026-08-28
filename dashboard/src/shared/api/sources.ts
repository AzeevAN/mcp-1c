import { useQuery } from "@tanstack/react-query";

export type SourceItem = {
  id: string;
  kind: "configuration" | "modules" | "extension" | "extension-runtime" | "syntax" | "query";
  platform: string;
  items_total: number;
  status: string;
  loaded_at: string;
  code_version: string;
  incomplete: boolean;
  warnings: string[];
};

export type Coverage = {
  has_limitations: boolean;
  modules: {
    total: number;
    source_available: number;
    empty: number;
    partial: number;
    unreadable: number;
    conflict: number;
    compiled_without_source: number;
  };
  procedures: { total: number; full: number; partial: number };
  form_structures: { total: number; full: number; partial: number; unreadable: number };
  form_modules: {
    total: number;
    read: number;
    empty: number;
    missing: number;
    unreadable: number;
  };
  problems_total: number;
  problem_categories: Array<{ category: string; count: number }>;
};

export type CodeCorpus = {
  id: string;
  label: string;
  kind: "modules" | "extension";
  phase: "ready" | "limited" | "building" | "error" | "missing";
  state: string;
  source: SourceItem | null;
  coverage: Coverage | null;
  journal: string;
  journal_url: string;
};

export type ConfigurationSource = {
  id: string;
  version: string;
  platform: string;
  objects: number;
  edges: number;
  loaded_at: string;
  notes: string[];
  source: SourceItem | null;
  extension_runtime?: SourceItem | null;
  corpora: CodeCorpus[];
};

export type SourcesResponse = {
  api_version: "v1";
  permissions: { read: boolean; admin: boolean };
  configurations: ConfigurationSource[];
  references: SourceItem[];
};

class SourcesApiError extends Error {
  constructor(readonly status: number) {
    super(`API источников ответил ${status}.`);
  }
}

async function getSources(): Promise<SourcesResponse> {
  const response = await fetch("/api/v1/sources", {
    headers: { accept: "application/json" },
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new SourcesApiError(response.status);
  }
  return response.json() as Promise<SourcesResponse>;
}

export function useSources() {
  return useQuery({
    queryKey: ["sources"],
    queryFn: getSources,
    retry: (failureCount, error) =>
      error instanceof SourcesApiError && error.status === 409
        ? failureCount < 5
        : failureCount < 3,
    retryDelay: (attempt) => Math.min(100 * 2 ** attempt, 500),
    refetchInterval: (query) =>
      query.state.data?.configurations.some((configuration) =>
        configuration.corpora.some((corpus) => corpus.phase === "building"),
      )
        ? 2_000
        : false,
  });
}
