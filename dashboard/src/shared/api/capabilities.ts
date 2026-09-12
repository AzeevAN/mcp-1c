import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type CapabilitiesStatus = {
  available: string[];
  active: string[];
  desired: string[];
  pending_restart: boolean;
};

type ErrorPayload = { error?: string };

export class CapabilitiesApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message);
    this.name = "CapabilitiesApiError";
  }
}

async function capabilityRequest(
  method: "GET" | "PUT",
  enabled?: string[],
): Promise<CapabilitiesStatus> {
  const response = await fetch("/api/v1/capabilities", {
    method,
    headers: enabled
      ? { accept: "application/json", "content-type": "application/json" }
      : { accept: "application/json" },
    credentials: "same-origin",
    body: enabled ? JSON.stringify({ enabled }) : undefined,
  });
  const payload = (await response.json().catch(() => ({}))) as CapabilitiesStatus & ErrorPayload;
  if (!response.ok) {
    throw new CapabilitiesApiError(
      payload.error || `API настроек модулей ответил ${response.status}.`,
      response.status,
    );
  }
  return payload;
}

export function getCapabilities(): Promise<CapabilitiesStatus> {
  return capabilityRequest("GET");
}

export function saveCapabilities(enabled: string[]): Promise<CapabilitiesStatus> {
  return capabilityRequest("PUT", enabled);
}

export function useCapabilities() {
  return useQuery({
    queryKey: ["capabilities"],
    queryFn: getCapabilities,
  });
}
export function useSaveCapabilities() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: saveCapabilities,
    onSuccess: (status) => {
      client.setQueryData(["capabilities"], status);
    },
  });
}
