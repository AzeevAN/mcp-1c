type PlaceholderPageProps = {
  eyebrow: string;
  title: string;
  description: string;
};

export function PlaceholderPage({ eyebrow, title, description }: PlaceholderPageProps) {
  return (
    <section className="placeholder-page">
      <span className="eyebrow">{eyebrow}</span>
      <h1>{title}</h1>
      <p>{description}</p>
      <div className="placeholder-boundary">
        Сначала фиксируем данные и пользовательский путь текущей страницы, затем переносим один законченный блок.
      </div>
    </section>
  );
}
