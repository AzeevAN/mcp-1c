import { useQuery } from "@tanstack/react-query";

export type QueryScope = "objects" | "fields" | "syntax";

export type QueriesSetup = {
  api_version: "v1";
  configuration_names: string[];
  default_configuration: string;
  scopes: Array<{
    id: QueryScope;
    label: string;
    requires_configuration: boolean;
  }>;
  limits: {
    phrases: number;
    phrase_chars: number;
    results_per_phrase: number;
  };
  availability: {
    configurations: boolean;
    syntax: boolean;
  };
};

export type QueryHit = {
  position: number;
  id: string;
  title: string;
  kind: string;
  score: number;
  reason: string;
  card_url: string;
};

export type QueryRunResponse = {
  api_version: "v1";
  request: {
    config: string;
    scope: QueryScope;
    phrases: string[];
  };
  results: Array<{
    phrase: string;
    alias_url: string | null;
    hits: QueryHit[];
    hidden: Array<{ title: string; reason: string }>;
  }>;
};

export class QueriesApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function responseJson<T>(response: Response): Promise<T> {
  const payload = await response.json() as T & { error?: string };
  if (!response.ok) {
    throw new QueriesApiError(payload.error || `Сервер ответил ${response.status}.`, response.status);
  }
  return payload;
}

async function getQueriesSetup(): Promise<QueriesSetup> {
  return responseJson<QueriesSetup>(await fetch("/api/v1/queries"));
}

export async function runQueries(request: QueryRunResponse["request"]): Promise<QueryRunResponse> {
  const response = await fetch("/api/v1/queries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  return responseJson<QueryRunResponse>(response);
}

export function useQueriesSetup() {
  return useQuery({
    queryKey: ["queries-setup"],
    queryFn: getQueriesSetup,
  });
}
