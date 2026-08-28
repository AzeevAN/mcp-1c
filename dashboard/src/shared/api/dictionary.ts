import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type DictionaryAlias = {
  phrase: string;
  targets: string[];
  source: string;
  scope: string | null;
  removable: boolean;
};

export type DictionaryResponse = {
  api_version: "v1";
  permissions: { read: boolean; admin: boolean };
  configuration_names: string[];
  configuration: string;
  aliases: DictionaryAlias[];
  synonym_groups: string[][];
  stats: {
    local_synonym_groups: number;
    builtin_synonym_groups: number;
    builtin_aliases: number;
    configurations_with_aliases: number;
    local_aliases: number;
  };
};

type ApiErrorPayload = { error?: string };

export class DictionaryApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "DictionaryApiError";
  }
}

async function request<T>(path: string, body?: object): Promise<T> {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body
      ? { accept: "application/json", "content-type": "application/json" }
      : { accept: "application/json" },
    credentials: "same-origin",
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({})) as T & ApiErrorPayload;
  if (!response.ok) {
    throw new DictionaryApiError(
      payload.error || `API словаря ответил ${response.status}.`,
      response.status,
    );
  }
  return payload;
}

export function useDictionary(config: string) {
  const params = new URLSearchParams();
  if (config) params.set("config", config);
  const suffix = params.size ? `?${params}` : "";
  return useQuery({
    queryKey: ["dictionary", config],
    queryFn: () => request<DictionaryResponse>(`/api/v1/dictionary${suffix}`),
    placeholderData: (previous) => previous,
  });
}

function useDictionaryMutation<TVariables>(
  path: string,
  body: (variables: TVariables) => object,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (variables: TVariables) => request<{ changed: object }>(path, body(variables)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["dictionary"] });
    },
  });
}

export function useAddDictionaryAlias() {
  return useDictionaryMutation(
    "/api/v1/dictionary/aliases",
    (value: { phrase: string; targets: string[]; config: string }) => value,
  );
}

export function useRemoveDictionaryAlias() {
  return useDictionaryMutation(
    "/api/v1/dictionary/aliases/remove",
    (value: { phrase: string; scope: string }) => value,
  );
}

export function useAddDictionarySynonyms() {
  return useDictionaryMutation(
    "/api/v1/dictionary/synonyms",
    (value: { words: string[] }) => value,
  );
}

export function useRemoveDictionarySynonyms() {
  return useDictionaryMutation(
    "/api/v1/dictionary/synonyms/remove",
    (value: { words: string[] }) => value,
  );
}
