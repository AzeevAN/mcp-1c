import { BookOpen, ChevronRight, X } from "lucide-react";
import { useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useReferenceStatus } from "../shared/api/reference";
import { type ReferenceAdminState, useAdminSources } from "../shared/api/sourceAdmin";

const referenceStateLabels: Record<string, string> = {
  missing: "не загружена",
  untrusted: "не доверена",
  corrupt: "повреждена",
  incompatible: "несовместима",
};

function isVerified(reference: ReferenceAdminState["active"] | undefined): boolean {
  return reference?.ready === true
    && reference.state === "ready"
    && reference.signature === "ed25519";
}

export function ReferenceSidebar({ admin }: { admin: boolean }) {
  const administrative = useAdminSources(admin);
  const publicStatus = useReferenceStatus(!admin);
  const reference = admin ? administrative.data?.reference : publicStatus.data;
  const query = admin ? administrative : publicStatus;
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = () => { setOpen(false); trigger.current?.focus(); };
  const active = query.isError ? undefined : reference?.active;
  const verified = isVerified(active);
  const label = query.isError
    ? "Статус недоступен"
    : verified
      ? "подключена"
      : active ? referenceStateLabels[active.state] ?? "проверка не подтверждена" : "Загружаем статус…";
  const tone = query.isError
    ? "danger"
    : verified
      ? "success"
      : active?.state === "missing" ? "warning" : active ? "danger" : null;
  const errorMessage = query.error instanceof Error ? query.error.message : "Статус справки недоступен.";

  return (
    <section className="reference-sidebar" aria-label="Общая справка">
      <h2>Общая справка</h2>
      <button ref={trigger} className={`reference-sidebar-button${tone ? ` is-${tone}` : ""}`} type="button"
        aria-label={`Общая справка: ${label}`} aria-haspopup="dialog" onClick={() => setOpen(true)} disabled={!active}>
        <BookOpen size={21} aria-hidden="true" />
        <span className="reference-sidebar-copy">
          <strong>{active?.items != null ? `${new Intl.NumberFormat("ru-RU").format(active.items)} материалов` : "Для всех конфигураций"}</strong>
          <span className="reference-sidebar-state"><i aria-hidden="true" />{label}</span>
        </span>
        <ChevronRight size={17} aria-hidden="true" />
      </button>
      {query.isError && <p className="inline-warning">{errorMessage}</p>}
      {query.isError && <button className="reference-retry" type="button" onClick={() => void query.refetch()}>Повторить</button>}
      {open && !query.isError && reference && <ReferenceDialog reference={reference} admin={admin} onClose={close} />}
    </section>
  );
}

function ReferenceDialog({ reference, admin, onClose }: {
  reference: ReferenceAdminState; admin: boolean; onClose: () => void;
}) {
  const active = reference.active;
  const verified = isVerified(active);
  const signatureLabel = active.signature === "ed25519"
    ? verified ? "Проверена" : "Заявлена, пакет не готов"
    : active.signature === "not-checked"
      ? "Не проверена"
      : active.signature === "verification-error" ? "Ошибка проверки" : active.signature;
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <section className="reference-dialog" role="dialog" aria-modal="true" aria-labelledby="reference-dialog-title" tabIndex={-1}>
        <button className="modal-close" type="button" onClick={onClose} aria-label="Закрыть"><X size={17} aria-hidden="true" /></button>
        <span className="reference-dialog-icon"><BookOpen size={24} aria-hidden="true" /></span>
        <h2 id="reference-dialog-title">Общая справка</h2>
        <p className="reference-dialog-subtitle">Единая встроенная справка для всех конфигураций.</p>
        <dl className="reference-dialog-facts">
          {active.items != null && <div><dt>Материалы</dt><dd>{new Intl.NumberFormat("ru-RU").format(active.items)}</dd></div>}
          {signatureLabel && <div><dt>Подпись</dt><dd>{signatureLabel}</dd></div>}
          {active.schema_version && <div><dt>Версия формата</dt><dd>{active.schema_version}</dd></div>}
          {active.index_cache && <div><dt>Индекс</dt><dd>{active.index_cache}</dd></div>}
        </dl>
        {!verified && active.message && <p className="inline-warning">{active.message}</p>}
        <p className="reference-dialog-subtitle">
          {admin
            ? verified
              ? "Пакет включён в поставляемый образ и изменяется только вместе с релизом."
              : "Пакет общей справки не готов к использованию."
            : verified
              ? "Только чтение. Пакет общей справки проверен подписью."
              : "Только чтение. Доверие к пакету общей справки не подтверждено."}
        </p>
      </section>
    </div>, document.body,
  );
}
