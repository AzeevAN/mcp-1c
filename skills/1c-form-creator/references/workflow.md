# Сборка и изменение формы

## Входы

- Путь к файловой выгрузке конфигурации (`Configuration.xml`), её версия XML, версия платформы и тип формы.
- Точное имя владельца `Обработка.Имя`, `Справочник.Имя` и т. п.; имя формы; роль формы.
- Спецификация управляемой формы по текущему контракту локального `form_core`. Для обработки с таблицей начни с `templates/data-processor-form.json`; обязательные поля другого профиля см. в `templates/minimal-form.json`. Точный парсер схемы находится в `scripts/form_core/models.py`; не изобретай ключи и не переноси схему другой версии. Для списка выбора и числового реквизита есть короткие примеры в `schema-patterns.md`.
- Полный прикладной `Module.bsl`, если форма содержит команды или события. Каркас, выданный компилятором, не реализует задачу.
- Для нового владельца: metadata specification или `--auto-data-processor` только для основной формы `Обработка.*`.

## Команды

```bash
python3 SKILL_DIR/scripts/form_creator.py inspect --config CONFIG_DIR
python3 SKILL_DIR/scripts/form_creator.py build --spec FORM_SPEC.json --module Module.bsl --auto-data-processor --config CONFIG_DIR --output EMPTY_STAGING_DIR
```

`build` проверяет совпадение `format_version` с `Configuration.xml`, собирает форму и metadata bundle, запускает статические проверки и только затем пишет результат. В `EMPTY_STAGING_DIR` будут owner-relative файлы, `Configuration.xml` с регистрацией владельца, `Configuration.xml.patch`, `verification.json`. Исходная выгрузка не меняется. Для существующего владельца без новой регистрации опусти `--config` и применяй только файлы формы после сравнения с оригиналом. Для отдельной формы без descriptor опусти `--auto-data-processor` и `--metadata-spec`.

Проверка после сборки встроенной обработки:

```bash
python3 SKILL_DIR/scripts/form_creator.py validate \
  --form-xml EMPTY_STAGING_DIR/DataProcessors/Имя/Forms/Форма/Ext/Form.xml \
  --module EMPTY_STAGING_DIR/DataProcessors/Имя/Forms/Форма/Ext/Form/Module.bsl \
  --owner Обработка.Имя --role object --form-name Форма \
  --bundle EMPTY_STAGING_DIR --format-version 2.20
```

`--bundle` принимает корень комплекта. Файлы `Configuration.xml`, `Configuration.xml.patch` и `verification.json` в нём служебные; валидатор выбирает только артефакты владельца. Черновой `.bsl` держи **за пределами** комплекта, иначе проверка справедливо отвергнет лишний файл. Если нужно пересобрать, укажи новый пустой каталог результата. Копия `Configuration.xml` сохраняет исходный стиль строк, а патч может вынести закрывающий `</ChildObjects>` на новую строку; проверь ограниченность diff. При работе с существующим XML сначала пробуй:

```bash
python3 SKILL_DIR/scripts/form_creator.py extract --form-xml FORM_XML --module FORM_MODULE_BSL --owner Обработка.Имя --role object --form-name Форма --output NEW_SPEC.json
```

`extract` откажет при неточном round-trip. Считай это защитой от потери свойств при обновлении. Для чужой формы используй нативный редактор 1С или дифф по точному XML-образцу той же версии и повторную статическую и нативную проверку.

## Что сдавать

Предъяви список изменённых owner-relative файлов и `Configuration.xml` diff; отдельно состояние `static`, `bsl_api`, `native_import`, `runtime_visual`, `business_behavior`. Если нет платформы 1С, последние 3 остаются `not_checked`. Для XLSX проверь пользовательский сценарий на реальном маленьком файле с заголовком и 5 колонками, затем добавление дополнительных колонок в таблице формы. Выбор кодировки проверяй отдельно на CSV/TXT, если он входит в задачу.
