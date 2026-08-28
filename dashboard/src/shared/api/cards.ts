import { useQuery } from "@tanstack/react-query";

export type CardKind = "object" | "syntax";
export type CardDetail = "brief" | "fields" | "full";

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
): Promise<CardResponse> {
  const params = new URLSearchParams({ name, detail });
  if (config) params.set("config", config);
  const response = await fetch(`/api/v1/cards/${kind}?${params}`);
  const payload = await response.json() as CardResponse & { error?: string };
  if (!response.ok) {
    throw new CardApiError(payload.error || `Сервер ответил ${response.status}.`, response.status);
  }
  return payload;
}

export function useCard(kind: CardKind, config: string, name: string, detail: CardDetail) {
  return useQuery({
    queryKey: ["card", kind, config, name, detail],
    queryFn: () => getCard(kind, config, name, detail),
    enabled: Boolean(name),
  });
}
