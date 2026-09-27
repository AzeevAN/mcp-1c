import { useQuery } from "@tanstack/react-query";

export type CardKind = "object" | "syntax";
export type CardDetail = "brief" | "fields" | "full" | "links";

export type CardNavigationItem = {
  section: string;
  label: string;
  address: string;
  status: "ready" | "unresolved" | "unavailable" | "ambiguous";
  target_name: string;
};

export type CardNavigation = {
  variant: string;
  state: "known" | "legacy" | "unknown";
  platform: string;
  total: number;
  offset: number;
  next_offset: number | null;
  items: CardNavigationItem[];
};

export type CardResponse = {
  api_version: "v1";
  kind: CardKind;
  name: string;
  configuration: string;
  configuration_names: string[];
  configuration_required: boolean;
  detail: CardDetail;
  detail_levels: CardDetail[];
  markdown: string;
  html: string;
  navigation?: CardNavigation[];
};

export class CardApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function getCard(
  kind: CardKind,
  config: string,
  name: string,
  detail: CardDetail,
  linksOffset: number,
): Promise<CardResponse> {
  const params = new URLSearchParams({ name, detail });
  if (config) params.set("config", config);
  if (detail === "links" && linksOffset > 0) params.set("links_offset", String(linksOffset));
  const response = await fetch(`/api/v1/cards/${kind}?${params}`);
  const payload = await response.json() as CardResponse & { error?: string };
  if (!response.ok) {
    throw new CardApiError(payload.error || `Сервер ответил ${response.status}.`, response.status);
  }
  return payload;
}

export function useCard(kind: CardKind, config: string, name: string, detail: CardDetail, linksOffset = 0) {
  return useQuery({
    queryKey: ["card", kind, config, name, detail, detail === "links" ? linksOffset : 0],
    queryFn: () => getCard(kind, config, name, detail, linksOffset),
    enabled: Boolean(name),
  });
}
