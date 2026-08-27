import type { PropsWithChildren } from "react";

type StatusTone = "success" | "warning" | "danger" | "info";

type StatusBadgeProps = PropsWithChildren<{
  tone: StatusTone;
}>;

export function StatusBadge({ tone, children }: StatusBadgeProps) {
  return <span className={`status-badge is-${tone}`}>{children}</span>;
}
