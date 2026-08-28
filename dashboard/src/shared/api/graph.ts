import { useQuery } from "@tanstack/react-query";

export type GraphNode = {
  name: string;
  short: string;
  kind: string;
  degree: number;
  x: number;
  y: number;
  color: string;
  graph_url: string;
  object_url: string;
};

export type GraphLink = {
  source: string;
  target: string;
  title: string;
  outgoing: boolean;
};

export type GraphResponse = {
  api_version: "v1";
  configuration_names: string[];
  configuration: string;
  name: string;
  limit: number;
  limit_options: number[];
  state: "empty_registry" | "awaiting_object" | "not_found" | "isolated" | "ready";
  message: string;
  suggestions: Array<{ name: string; graph_url: string }>;
  graph: null | {
    depth: 1;
    total: number;
    shown: number;
    truncated: boolean;
    bounds: [number, number, number, number];
    subject: GraphNode;
    nodes: GraphNode[];
    links: GraphLink[];
    kinds: Array<{ kind: string; color: string }>;
  };
};

export class GraphApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function getGraph(config: string, name: string, limit: number): Promise<GraphResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (config) params.set("config", config);
  if (name) params.set("name", name);
  const response = await fetch(`/api/v1/graph?${params}`);
  const payload = await response.json() as GraphResponse & { error?: string };
  if (!response.ok) {
    throw new GraphApiError(payload.error || `Сервер ответил ${response.status}.`, response.status);
  }
  return payload;
}

export function useGraph(config: string, name: string, limit: number) {
  return useQuery({
    queryKey: ["graph", config, name, limit],
    queryFn: () => getGraph(config, name, limit),
    placeholderData: (previous) => previous,
  });
}
