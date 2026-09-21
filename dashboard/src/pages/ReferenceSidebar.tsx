import { BookOpen, ChevronRight, X } from "lucide-react";
import { useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useReferenceStatus } from "../shared/api/reference";
import { type ReferenceAdminState, useAdminSources } from "../shared/api/sourceAdmin";

export function ReferenceSidebar({ admin }: { admin: boolean }) {
  const administrative = useAdminSources(admin);
  const publicStatus = useReferenceStatus(!admin);
  const reference = admin ? administrative.data?.reference : publicStatus.data;
  const query = admin ? administrative : publicStatus;
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = () => { setOpen(false); trigger.current?.focus(); };
  const active = reference?.active;
  const label = query.isError ? "Статус недоступен" : active?.state === "ready" ? "подключена" : active?.state ?? "Загружаем статус…";

  return (
    <section className="reference-sidebar" aria-label="Общая справка">
      <h2>Общая справка</h2>
      <button ref={trigger} className="reference-sidebar-button is-success" type="button"
        aria-label={`Общая справка: ${label}`} aria-haspopup="dialog" onClick={() => setOpen(true)} disabled={!active}>
        <BookOpen size={21} aria-hidden="true" />
        <span className="reference-sidebar-copy">
          <strong>{active?.items != null ? `${new Intl.NumberFormat("ru-RU").format(active.items)} материалов` : "Для всех конфигураций"}</strong>
          <span className="reference-sidebar-state"><i aria-hidden="true" />{label}</span>
        </span>
        <ChevronRight size={17} aria-hidden="true" />
      </button>
      {query.isError && <button className="reference-retry" type="button" onClick={() => void query.refetch()}>Повторить</button>}
      {open && reference && <ReferenceDialog reference={reference} admin={admin} onClose={close} />}
    </section>
  );
}

function ReferenceDialog({ reference, admin, onClose }: {
  reference: ReferenceAdminState; admin: boolean; onClose: () => void;
}) {
  const active = reference.active;
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="reference-dialog" role="dialog" aria-modal="true" aria-labelledby="reference-dialog-title" tabIndex={-1}>
        <button className="modal-close" type="button" onClick={onClose} aria-label="Закрыть"><X size={17} aria-hidden="true" /></button>
        <span className="reference-dialog-icon"><BookOpen size={24} aria-hidden="true" /></span>
        <h2 id="reference-dialog-title">Общая справка</h2>
        <p className="reference-dialog-subtitle">Единая встроенная справка для всех конфигураций.</p>
        <dl className="reference-dialog-facts">
          {active.items != null && <div><dt>Материалы</dt><dd>{new Intl.NumberFormat("ru-RU").format(active.items)}</dd></div>}
          {active.signature && <div><dt>Подпись</dt><dd>{active.signature === "ed25519" ? "Проверена" : active.signature}</dd></div>}
          {active.schema_version && <div><dt>Версия формата</dt><dd>{active.schema_version}</dd></div>}
          {active.index_cache && <div><dt>Индекс</dt><dd>{active.index_cache}</dd></div>}
        </dl>
        <p className="reference-dialog-subtitle">
          {admin ? "Пакет включён в поставляемый образ и изменяется только вместе с релизом." : "Только чтение. Пакет общей справки проверен подписью."}
        </p>
      </section>
    </div>, document.body,
  );
}
