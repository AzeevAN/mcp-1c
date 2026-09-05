import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  bindDirectory, unbindDirectory, refreshDirectory,
  type DirectorySources, type IntakeCandidate,
} from "../shared/api/configIntake";

export function ConfigDirectories({ sources, configuration, busy, onCandidate }: {
  sources: DirectorySources;
  configuration: string;
  busy: boolean;
  onCandidate: (candidate: IntakeCandidate | null) => void;
}) {
  const client = useQueryClient();
  const [selected, setSelected] = useState("");
  const [choosing, setChoosing] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const bound = sources.bindings[configuration];
  const run = async (action: "bind" | "unbind" | "refresh") => {
    setPending(true);
    setError("");
    onCandidate(null);
    try {
      if (action === "refresh") {
        const result = await refreshDirectory(configuration);
        onCandidate(result.candidate);
      } else {
        const updated = action === "bind"
          ? await bindDirectory(configuration, selected)
          : await unbindDirectory(configuration);
        client.setQueryData(["sources", "directories"], updated);
        setChoosing(false);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Не удалось проверить каталог.");
    } finally {
      setPending(false);
    }
  };
  const disabled = busy || pending;
  if (!sources.roots.length && !bound) return null;
  return (
    <>
      {bound && <small>Каталог: {bound}{!sources.roots.includes(bound) ? " — недоступен" : ""}</small>}
      {sources.roots.length > 0 && <button type="button" className="button-secondary" disabled={disabled}
        onClick={() => { setSelected(bound ?? ""); setError(""); setChoosing(true); }}>
        {bound ? "Сменить каталог" : "Выбрать каталог"}
      </button>}
      {bound && <button type="button" className="button-primary" disabled={disabled}
        onClick={() => void run("refresh")}>Обновить из каталога</button>}
      {bound && !sources.roots.length && <button type="button" className="button-secondary" disabled={disabled}
        onClick={() => void run("unbind")}>Отключить каталог</button>}
      {pending && <span role="status">Проверяем каталог…</span>}
      {error && !choosing && <p className="admin-feedback is-danger" role="alert">{error}</p>}
      {choosing && <div className="modal-backdrop" role="presentation">
        <section className="intake-preview-dialog" role="dialog" aria-modal="true" aria-label="Каталог основной конфигурации">
          <button type="button" className="modal-close" aria-label="Закрыть выбор каталога" disabled={pending}
            onClick={() => setChoosing(false)}>×</button>
          <h2>Каталог основной конфигурации</h2>
          <p><strong>{configuration}</strong></p>
          <p>Выберите корень выгрузки, подключённый администратором. При подключении проверяется соответствие конфигурации; загруженные данные обновляются отдельно.</p>
          <label className="intake-parent-select">
            <span>Доступный каталог</span>
            <select aria-label={`Источник для ${configuration}`} value={selected} disabled={disabled}
              onChange={(event) => setSelected(event.target.value)}>
              <option value="">Не выбран</option>
              {bound && !sources.roots.includes(bound) && <option value={bound}>{bound} — недоступен</option>}
              {sources.roots.map((root) => <option key={root} value={root}>{root}</option>)}
            </select>
          </label>
          {error && <p className="admin-feedback is-danger" role="alert">{error}</p>}
          <footer className="intake-preview-actions">
            {bound && <button type="button" className="button-secondary" disabled={disabled}
              onClick={() => void run("unbind")}>Отключить каталог</button>}
            <button type="button" className="button-primary" disabled={disabled || !sources.roots.includes(selected)}
              onClick={() => void run("bind")}>Подключить</button>
          </footer>
        </section>
      </div>}
    </>
  );
}
