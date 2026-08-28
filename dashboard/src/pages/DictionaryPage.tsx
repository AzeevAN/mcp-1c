import {
  BookOpenText,
  Braces,
  CircleAlert,
  Plus,
  ServerCrash,
  ShieldCheck,
  Tags,
  Trash2,
} from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  DictionaryApiError,
  useAddDictionaryAlias,
  useAddDictionarySynonyms,
  useDictionary,
  useRemoveDictionaryAlias,
  useRemoveDictionarySynonyms,
} from "../shared/api/dictionary";
import { StatusBadge } from "../shared/ui/StatusBadge";

function words(value: string) {
  return value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean);
}

function MutationMessage({ message, error }: { message: string; error: unknown }) {
  if (error) {
    return (
      <div className="dictionary-feedback is-error" role="alert">
        <CircleAlert size={16} aria-hidden="true" />
        {error instanceof DictionaryApiError ? error.message : "Не удалось изменить словарь."}
      </div>
    );
  }
  if (!message) return null;
  return <div className="dictionary-feedback" role="status"><ShieldCheck size={16} />{message}</div>;
}

export function DictionaryPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const config = searchParams.get("config") || "";
  const phraseFromUrl = searchParams.get("phrase") || "";
  const dictionary = useDictionary(config);
  const addAlias = useAddDictionaryAlias();
  const removeAlias = useRemoveDictionaryAlias();
  const addSynonyms = useAddDictionarySynonyms();
  const removeSynonyms = useRemoveDictionarySynonyms();
  const [aliasPhrase, setAliasPhrase] = useState(phraseFromUrl);
  const [aliasTargets, setAliasTargets] = useState("");
  const [aliasScope, setAliasScope] = useState(config);
  const [synonymWords, setSynonymWords] = useState("");
  const [message, setMessage] = useState("");
  const selected = dictionary.data
    ? dictionary.data.configuration_names.includes(config)
      ? config
      : dictionary.data.configuration
    : "";

  useEffect(() => setAliasPhrase(phraseFromUrl), [phraseFromUrl]);
  useEffect(() => setAliasScope(selected), [selected]);
  useEffect(() => {
    if (
      dictionary.isPlaceholderData
      || !dictionary.data?.configuration
      || dictionary.data.configuration === config
    ) return;
    const next = new URLSearchParams(searchParams);
    next.set("config", dictionary.data.configuration);
    setSearchParams(next, { replace: true, preventScrollReset: true });
  }, [config, dictionary.data, dictionary.isPlaceholderData, searchParams, setSearchParams]);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: true, preventScrollReset: true });
  };

  if (dictionary.isPending) {
    return <section className="sources-message"><span className="loading-dot" />Читаем правила поиска…</section>;
  }
  if (dictionary.isError || !dictionary.data) {
    const text = dictionary.error instanceof DictionaryApiError
      ? dictionary.error.message
      : "Не удалось получить словарь.";
    return <section className="sources-message is-error"><ServerCrash />{text}</section>;
  }

  const data = dictionary.data;
  const admin = data.permissions.admin;
  const mutationError = addAlias.error || removeAlias.error || addSynonyms.error || removeSynonyms.error;
  const busy = addAlias.isPending || removeAlias.isPending || addSynonyms.isPending || removeSynonyms.isPending;

  const changeConfiguration = (value: string) => {
    setMessage("");
    setAliasScope(value);
    setParam("config", value);
  };

  const submitAlias = (event: FormEvent) => {
    event.preventDefault();
    setMessage("");
    addAlias.mutate(
      { phrase: aliasPhrase.trim(), targets: words(aliasTargets), config: aliasScope },
      {
        onSuccess: () => {
          setAliasPhrase("");
          setAliasTargets("");
          setParam("phrase", "");
          setMessage("Псевдоним сохранён и уже участвует в новых запросах.");
        },
      },
    );
  };

  const submitSynonyms = (event: FormEvent) => {
    event.preventDefault();
    setMessage("");
    addSynonyms.mutate(
      { words: words(synonymWords) },
      {
        onSuccess: () => {
          setSynonymWords("");
          setMessage("Группа синонимов сохранена и уже участвует в новых запросах.");
        },
      },
    );
  };

  return (
    <div className="dictionary-page">
      <header className="dictionary-heading">
        <div>
          <span className="eyebrow">Локальная терминология</span>
          <h1>Словарь поиска</h1>
          <p>Псевдоним ведёт фразу прямо к объектам, синоним расширяет отдельное слово. Происхождение каждого эффективного правила показано явно.</p>
        </div>
        <StatusBadge tone={admin ? "success" : "info"}>{admin ? "Правка доступна" : "Только чтение"}</StatusBadge>
      </header>

      <section className="dictionary-summary" aria-label="Сводка словаря">
        <article><BookOpenText size={20} /><span><strong>{data.stats.builtin_aliases}</strong>встроенных псевдонимов</span></article>
        <article><Braces size={20} /><span><strong>{data.stats.local_aliases}</strong>локальных псевдонимов</span></article>
        <article><Tags size={20} /><span><strong>{data.stats.local_synonym_groups}</strong>локальных групп</span></article>
      </section>

      <section className="dictionary-context">
        {data.configuration_names.length > 0 ? (
          <label className="query-field dictionary-config-field">
            <span>Конфигурация</span>
            <select aria-label="Конфигурация" value={selected} onChange={(event) => changeConfiguration(event.target.value)}>
              {data.configuration_names.map((item) => <option key={item}>{item}</option>)}
            </select>
          </label>
        ) : (
          <div className="dictionary-no-config"><CircleAlert size={18} /><span><strong>Конфигурации не загружены</strong>Видны встроенные и общие локальные правила.</span></div>
        )}
        <p><strong>Почему это важно:</strong> встроенное верно для всех установок и меняется релизом. Локальное описывает речь конкретной команды и хранится в <code>data/dictionary.json</code>.</p>
      </section>

      {!admin && (
        <div className="dictionary-readonly-note">
          <ShieldCheck size={18} />
          <span><strong>Править словарь может только администратор.</strong> В режиме чтения доступны все эффективные правила и их происхождение.</span>
        </div>
      )}
      <MutationMessage message={message} error={mutationError} />

      <section className="dictionary-panel">
        <header>
          <div><span className="eyebrow">Фраза → объекты</span><h2>Псевдонимы объектов</h2></div>
          <small>Показаны правила, эффективные для выбранной конфигурации.</small>
        </header>
        {admin && (
          <form className="dictionary-editor" onSubmit={submitAlias}>
            <div className="dictionary-editor-title"><Plus size={18} /><span><strong>Завести псевдоним</strong>Правило применяется сразу, перезапуск и переиндексация не нужны.</span></div>
            <label className="query-field"><span>Фраза</span><input aria-label="Фраза псевдонима" required value={aliasPhrase} onChange={(event) => setAliasPhrase(event.target.value)} placeholder="кто нам возит" /></label>
            <label className="query-field dictionary-target-field"><span>Объекты</span><input aria-label="Полные имена объектов" required value={aliasTargets} onChange={(event) => setAliasTargets(event.target.value)} placeholder="Справочник.Контрагенты, Документ.Поступление" /></label>
            <label className="query-field"><span>Область</span><select aria-label="Область псевдонима" value={aliasScope} onChange={(event) => setAliasScope(event.target.value)}>
              {selected && <option value={selected}>Только {selected}</option>}
              <option value="">Все конфигурации</option>
            </select></label>
            <button className="query-run-button" type="submit" disabled={busy}><Plus size={16} />Завести псевдоним</button>
          </form>
        )}
        <div className="dictionary-table-wrap">
          <table className="dictionary-table">
            <thead><tr><th>Фраза</th><th>Полные имена объектов</th><th>Происхождение</th>{admin && <th aria-label="Действия" />}</tr></thead>
            <tbody>
              {data.aliases.map((alias) => (
                <tr key={`${alias.phrase}:${alias.scope || "builtin"}`}>
                  <td><strong>{alias.phrase}</strong></td>
                  <td><div className="dictionary-targets">{alias.targets.map((target) => <code key={target}>{target}</code>)}</div></td>
                  <td><span className={alias.removable ? "dictionary-source is-local" : "dictionary-source"}>{alias.source}</span></td>
                  {admin && <td>{alias.removable && alias.scope && (
                    <button
                      className="dictionary-remove"
                      type="button"
                      disabled={busy}
                      aria-label={`Удалить псевдоним ${alias.phrase}`}
                      onClick={() => {
                        setMessage("");
                        removeAlias.mutate(
                          { phrase: alias.phrase, scope: alias.scope as string },
                          { onSuccess: () => setMessage("Локальный псевдоним удалён.") },
                        );
                      }}
                    ><Trash2 size={15} /></button>
                  )}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

      </section>

      <section className="dictionary-panel">
        <header>
          <div><span className="eyebrow">Слово = слово</span><h2>Группы синонимов</h2></div>
          <small>{data.stats.builtin_synonym_groups} встроенная группа · локальные общие для всех конфигураций</small>
        </header>
        {data.synonym_groups.length > 0 ? (
          <div className="dictionary-groups">
            {data.synonym_groups.map((group) => (
              <article key={group.join(":")}>
                <div>{group.map((item) => <span key={item}>{item}</span>)}</div>
                {admin && <button type="button" disabled={busy} aria-label={`Снять группу ${group.join(", ")}`} onClick={() => {
                  setMessage("");
                  removeSynonyms.mutate(
                    { words: group },
                    { onSuccess: () => setMessage("Локальная группа синонимов снята.") },
                  );
                }}><Trash2 size={15} />Снять</button>}
              </article>
            ))}
          </div>
        ) : (
          <div className="dictionary-groups-empty">Локальных групп пока нет. Встроенные правила продолжают работать.</div>
        )}
        {admin && (
          <form className="dictionary-editor is-synonyms" onSubmit={submitSynonyms}>
            <div className="dictionary-editor-title"><Plus size={18} /><span><strong>Завести группу</strong>Минимум два слова, разделённые пробелами или запятыми.</span></div>
            <label className="query-field dictionary-target-field"><span>Слова одной группы</span><input aria-label="Слова одной группы" required value={synonymWords} onChange={(event) => setSynonymWords(event.target.value)} placeholder="возчик перевозчик экспедитор" /></label>
            <button className="query-run-button" type="submit" disabled={busy}><Plus size={16} />Завести группу</button>
          </form>
        )}
      </section>
    </div>
  );
}
