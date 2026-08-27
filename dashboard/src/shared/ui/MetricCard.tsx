type MetricCardProps = {
  label: string;
  value: number | string;
  hint: string;
};

export function MetricCard({ label, value, hint }: MetricCardProps) {
  return (
    <article className="metric-card">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{hint}</small>
    </article>
  );
}
