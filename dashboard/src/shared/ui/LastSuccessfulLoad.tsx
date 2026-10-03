type Props = {
  value?: string | null;
};

export function LastSuccessfulLoad({ value }: Props) {
  const timestamp = value ? Date.parse(value) : NaN;
  const valid = Number.isFinite(timestamp);
  const date = new Date(timestamp);

  return (
    <span className="last-successful-load">
      <span className="last-successful-load-label">Последняя загрузка</span>
      {valid ? (
        <time
          dateTime={value!}
          title={new Intl.DateTimeFormat("ru-RU", {
            year: "numeric",
            month: "long",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
            timeZoneName: "long",
          }).format(date)}
        >
          {new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(date)}
        </time>
      ) : (
        <span className="last-successful-load-empty">Дата не сохранена</span>
      )}
    </span>
  );
}
