import {
  AlertTriangle,
  ArrowDownLeft,
  ArrowUpRight,
  Boxes,
  Focus,
  GitBranch,
  Search,
  ServerCrash,
} from "lucide-react";
import {
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  GraphApiError,
  type GraphLink,
  type GraphNode,
  type GraphResponse,
  useGraph,
} from "../shared/api/graph";
import { StatusBadge } from "../shared/ui/StatusBadge";

const DEFAULT_LIMIT = 30;

function nodeMap(graph: NonNullable<GraphResponse["graph"]>) {
  return new Map(
    [graph.subject, ...graph.nodes].map((node) => [node.name, node]),
  );
}

function GraphCanvas({ graph }: { graph: NonNullable<GraphResponse["graph"]> }) {
  const initial = graph.bounds;
  const [viewBox, setViewBox] = useState<[number, number, number, number]>(initial);
  const canvas = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const nodes = useMemo(() => nodeMap(graph), [graph]);

  useEffect(() => setViewBox(graph.bounds), [graph]);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;

    const zoom = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const factor = event.deltaY > 0 ? 1.1 : 0.9;
      setViewBox(([x, y, width, height]) => {
        const nextWidth = Math.min(initial[2] * 5, Math.max(initial[2] * 0.2, width * factor));
        const nextHeight = height * (nextWidth / width);
        return [
          x + (width - nextWidth) / 2,
          y + (height - nextHeight) / 2,
          nextWidth,
          nextHeight,
        ];
      });
    };

    element.addEventListener("wheel", zoom, { passive: false });
    return () => element.removeEventListener("wheel", zoom);
  }, [initial]);

  const startDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    drag.current = { x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const moveDrag = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (!drag.current) return;
    const scale = viewBox[2] / Math.max(event.currentTarget.clientWidth, 1);
    const dx = (event.clientX - drag.current.x) * scale;
    const dy = (event.clientY - drag.current.y) * scale;
    drag.current = { x: event.clientX, y: event.clientY };
    setViewBox(([x, y, width, height]) => [x - dx, y - dy, width, height]);
  };

  const line = (link: GraphLink) => {
    const source = nodes.get(link.source);
    const target = nodes.get(link.target);
    if (!source || !target) return null;
    return (
      <line
        className={link.outgoing ? "graph-edge is-outgoing" : "graph-edge is-incoming"}
        key={`${link.source}:${link.target}`}
        x1={source.x}
        y1={source.y}
        x2={target.x}
        y2={target.y}
        markerEnd="url(#spa-graph-arrow)"
      >
        <title>{link.title}</title>
      </line>
    );
  };

  const node = (item: GraphNode, subject = false) => {
    const radius = subject ? 16 : 10;
    return (
      <Link to={item.graph_url} key={item.name} aria-hidden="true" tabIndex={-1}>
        <title>{item.name} · связей {item.degree}</title>
        <circle className="graph-node-hit" cx={item.x} cy={item.y} r={subject ? 25 : 18} />
        <circle className="graph-node-halo" cx={item.x} cy={item.y} r={subject ? 21 : 15} />
        <circle
          className={subject ? "graph-node is-subject" : "graph-node"}
          cx={item.x}
          cy={item.y}
          r={radius}
          fill={item.color}
        />
        <text x={item.x} y={item.y + (subject ? 34 : 27)} textAnchor="middle">
          {item.short.slice(0, 26)}
        </text>
      </Link>
    );
  };

  return (
    <div className="graph-canvas-frame">
      <button className="graph-reset" type="button" onClick={() => setViewBox(initial)}>
        <Focus size={15} aria-hidden="true" />Вписать
      </button>
      <svg
        ref={canvas}
        className="graph-canvas"
        role="img"
        aria-label={`Окрестность объекта ${graph.subject.name}`}
        viewBox={viewBox.join(" ")}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={() => { drag.current = null; }}
        onPointerCancel={() => { drag.current = null; }}
      >
        <defs>
          <marker id="spa-graph-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0 0 L8 4 L0 8 z" />
          </marker>
        </defs>
        <g>{graph.links.map(line)}</g>
        <g>{graph.nodes.map((item) => node(item))}</g>
        {node(graph.subject, true)}
      </svg>
      <div className="graph-canvas-hint">Тянуть — перемещение · колесо — масштаб · клик — новый центр</div>
    </div>
  );
}

function GraphResult({ data }: { data: GraphResponse }) {
  const graph = data.graph;
  if (data.state === "awaiting_object") {
    return (
      <section className="graph-state-card">
        <Search size={30} aria-hidden="true" />
        <h2>Выберите центр графа</h2>
        <p>{data.message}</p>
        <Link to="/queries">Открыть страницу «Запросы»</Link>
      </section>
    );
  }
  if (data.state === "not_found") {
    return (
      <section className="graph-state-card is-error" role="alert">
        <AlertTriangle size={28} aria-hidden="true" />
        <h2>Объект не найден</h2>
        <p>{data.message}</p>
        {data.suggestions.length > 0 && (
          <div className="graph-suggestions">
            <span>Возможно, имелось в виду:</span>
            {data.suggestions.map((item) => <Link to={item.graph_url} key={item.name}>{item.name}</Link>)}
          </div>
        )}
      </section>
    );
  }
  if (!graph) return null;
  if (data.state === "isolated") {
    return (
      <section className="graph-state-card is-isolated">
        <Boxes size={30} aria-hidden="true" />
        <h2>Изолированный объект</h2>
        <p>{data.message}</p>
        <small>Связи из форм и схем компоновки текущая выгрузка не собирает.</small>
        <Link to={graph.subject.object_url}>Открыть карточку объекта</Link>
      </section>
    );
  }

  return (
    <div className="graph-result">
      {graph.truncated && (
        <div className="graph-truncated-note">
          <AlertTriangle size={18} aria-hidden="true" />
          <span><strong>Показано {graph.shown} из {graph.total}</strong>Самые связанные соседи показаны первыми. Поднимите предел, чтобы увидеть остальные.</span>
        </div>
      )}
      <div className="graph-result-grid">
        <GraphCanvas graph={graph} />
        <aside className="graph-inspector">
          <div className="graph-subject-card">
            <span className="eyebrow">Центр окрестности</span>
            <h2>{graph.subject.short}</h2>
            <code>{graph.subject.name}</code>
            <div><span>{graph.subject.kind}</span><strong>{graph.total} связей</strong></div>
            <Link to={graph.subject.object_url}>Открыть карточку объекта</Link>
          </div>

          <div className="graph-neighbour-list" aria-label="Соседи объекта">
            {graph.nodes.map((item, index) => {
              const link = graph.links[index];
              return (
                <article key={item.name}>
                  <i style={{ background: item.color }} aria-hidden="true" />
                  <div>
                    <Link to={item.graph_url} aria-label={`Построить вокруг ${item.name}`}>{item.short}</Link>
                    <small>{item.kind} · всего связей {item.degree}</small>
                    <span className={link.outgoing ? "is-outgoing" : "is-incoming"}>
                      {link.outgoing ? <ArrowUpRight size={13} /> : <ArrowDownLeft size={13} />}
                      {link.outgoing ? "выбранный объект ссылается" : "ссылается на выбранный объект"}
                    </span>
                    <em>{link.title || "Связь без подписи"}</em>
                  </div>
                </article>
              );
            })}
          </div>
        </aside>
      </div>
      <div className="graph-legend" aria-label="Виды объектов">
        {graph.kinds.map((item) => <span key={item.kind}><i style={{ background: item.color }} />{item.kind || "—"}</span>)}
        <span className="graph-depth-note"><strong>Глубина 1</strong>Дальше узлы раскрывает человек.</span>
      </div>
    </div>
  );
}

export function GraphPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const config = searchParams.get("config") || "";
  const name = searchParams.get("name") || "";
  const rawLimit = Number(searchParams.get("limit") || DEFAULT_LIMIT);
  const limit = Number.isFinite(rawLimit) && rawLimit > 0
    ? Math.min(Math.round(rawLimit), 400)
    : DEFAULT_LIMIT;
  const [draftName, setDraftName] = useState(name);
  const graph = useGraph(config, name, limit);

  useEffect(() => setDraftName(name), [name]);
  useEffect(() => {
    if (
      graph.isPlaceholderData
      || !graph.data?.configuration
      || graph.data.configuration === config
    ) return;
    const next = new URLSearchParams(searchParams);
    next.set("config", graph.data.configuration);
    setSearchParams(next, { replace: true, preventScrollReset: true });
  }, [config, graph.data, searchParams, setSearchParams]);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true, preventScrollReset: true });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setParam("name", draftName.trim());
  };

  if (graph.isPending) {
    return <section className="sources-message"><span className="loading-dot" />Собираем окрестность объекта…</section>;
  }
  if (graph.isError || !graph.data) {
    const message = graph.error instanceof GraphApiError
      ? graph.error.message
      : "Не удалось получить граф связей.";
    return <section className="sources-message is-error"><ServerCrash />{message}</section>;
  }

  const data = graph.data;
  const selectedConfiguration = data.configuration_names.includes(config)
    ? config
    : data.configuration;
  if (data.state === "empty_registry") {
    return (
      <section className="card-empty-state">
        <GitBranch size={34} aria-hidden="true" />
        <span className="eyebrow">Нет структуры конфигурации</span>
        <h1>Связи пока недоступны</h1>
        <p>{data.message}</p>
        <Link to="/sources">Перейти к источникам</Link>
      </section>
    );
  }

  return (
    <div className="graph-page">
      <header className="graph-page-heading">
        <div>
          <span className="eyebrow">Окрестность метаданных</span>
          <h1>Связи объектов</h1>
          <p>Один шаг от выбранного объекта. Координаты и отбор считает сервер; браузер только рисует и позволяет двигаться по узлам.</p>
        </div>
        <StatusBadge tone="info">Глубина 1</StatusBadge>
      </header>

      <form className="graph-controls" onSubmit={submit}>
        <label className="query-field">
          <span>Конфигурация</span>
          <select aria-label="Конфигурация" value={selectedConfiguration} onChange={(event) => setParam("config", event.target.value)}>
            {data.configuration_names.map((item) => <option key={item}>{item}</option>)}
          </select>
        </label>
        <label className="query-field graph-object-field">
          <span>Полное имя объекта</span>
          <input
            aria-label="Полное имя объекта"
            value={draftName}
            onChange={(event) => setDraftName(event.target.value)}
            placeholder="Справочник.Контрагенты"
          />
        </label>
        <label className="query-field graph-limit-field">
          <span>Соседей</span>
          <select aria-label="Предел соседей" value={data.limit} onChange={(event) => setParam("limit", event.target.value)}>
            {!data.limit_options.includes(data.limit) && <option value={data.limit}>{data.limit}</option>}
            {data.limit_options.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <button className="query-run-button" type="submit"><Search size={17} />Показать связи</button>
      </form>

      <GraphResult data={data} />
    </div>
  );
}
