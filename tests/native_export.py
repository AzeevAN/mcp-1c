"""Синтетическая выгрузка в файлы, опубликованная native-путём B_FULL.

Тесты кода раньше собирали реестр из schema v1 и отдельного архива кода
(`add_modules`). Этого пути в продукте больше нет: дашборд и intake
публикуют полную выгрузку в файлы одним поколением. Здесь — тот же путь
(`DirectoryExportTree` → `probe_export` → `collect_source_b` →
`convert_collection` → `materialize_generation` → `stage_generation` с
активацией B_FULL → `publish_generation`), без HTTP и фоновых заданий.

Структура берётся из модели `Configuration` (те же объекты, что у
`conftest.build_configuration`), владельцы кода — из каталогов дерева
модулей: `CommonModules/<Имя>`, `Documents/<Имя>` и т. п. Каждому владельцу
кода и каждой форме выписывается минимальный дескриптор, без которого
выгрузка в файлы не бывает.
"""

from __future__ import annotations

import itertools
import shutil
import tempfile
import weakref
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from mcp1c.intake_v2_collector import collect_source_b
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.intake_v2_operations import _activation_for_generation
from mcp1c.intake_v2_probe import probe_export
from mcp1c.intake_v2_transport import DirectoryExportTree
from mcp1c.model import Configuration, Field

NS = "http://v8.1c.ru/8.3/MDClasses"
NS_V8 = "http://v8.1c.ru/8.1/data/core"
NS_XS = "http://www.w3.org/2001/XMLSchema"
NS_CFG = "http://v8.1c.ru/8.1/data/enterprise/current-config"

# Каталог дерева выгрузки -> (тег в Configuration.xml, вид в модели).
_KINDS = {
    "Catalogs": ("Catalog", "Справочник"),
    "Documents": ("Document", "Документ"),
    "InformationRegisters": ("InformationRegister", "РегистрСведений"),
    "AccumulationRegisters": ("AccumulationRegister", "РегистрНакопления"),
    "Constants": ("Constant", "Константа"),
    "Enums": ("Enum", "Перечисление"),
    "ExchangePlans": ("ExchangePlan", "ПланОбмена"),
    "CommonModules": ("CommonModule", "ОбщийМодуль"),
    "CommonForms": ("CommonForm", "ОбщаяФорма"),
    "Reports": ("Report", "Отчет"),
    "DataProcessors": ("DataProcessor", "Обработка"),
    "EventSubscriptions": ("EventSubscription", "ПодпискаНаСобытие"),
    "ScheduledJobs": ("ScheduledJob", "РегламентноеЗадание"),
}
_BY_RU = {ru: (directory, tag) for directory, (tag, ru) in _KINDS.items()}
_REF = {
    "Справочник": "CatalogRef",
    "Документ": "DocumentRef",
    "Перечисление": "EnumRef",
}

_counter = itertools.count(1)

# Модель структуры, с которой реестр получил основное поколение: повторная
# публикация кода — это новая полная выгрузка, и структура в ней та же.
_МОДЕЛИ: "weakref.WeakKeyDictionary[object, Configuration]" = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True)
class КодВыгрузки:
    """Замороженный снимок дерева кода, как раньше — архив кода."""

    корень: Path
    extension: str | None = None
    version: str = ""


def _localized(value: str) -> str:
    return (
        "<v8:item><v8:lang>ru</v8:lang>"
        f"<v8:content>{escape(value)}</v8:content></v8:item>"
    )


def _document(tag: str, properties: str, children: str = "") -> bytes:
    return (
        f'<MetaDataObject xmlns="{NS}" xmlns:v8="{NS_V8}" '
        f'xmlns:xs="{NS_XS}" xmlns:cfg="{NS_CFG}">'
        f"<{tag}><Properties>{properties}</Properties>"
        f"<ChildObjects>{children}</ChildObjects></{tag}>"
        "</MetaDataObject>"
    ).encode()


def _type(field: Field) -> str:
    values = []
    for value in field.types or ["Строка"]:
        kind, _, name = value.partition(".")
        if kind in _REF and name:
            values.append(f"cfg:{_REF[kind]}.{name}")
        else:
            values.append("xs:string")
    body = "".join(f"<v8:Type>{value}</v8:Type>" for value in values)
    return f"<Type>{body}</Type>"


def _attribute(field: Field) -> str:
    synonym = f"<Synonym>{_localized(field.synonym)}</Synonym>" if field.synonym else ""
    return (
        "<Attribute><Properties>"
        f"<Name>{escape(field.name)}</Name>{synonym}{_type(field)}"
        "</Properties></Attribute>"
    )


# Свойства модели schema v1 -> теги Properties выгрузки в файлы.
_PROPERTIES = {
    "CommonModule": {
        "global": "Global",
        "server": "Server",
        "client_managed": "ClientManagedApplication",
        "server_call": "ServerCall",
        "privileged": "Privileged",
        "external_connection": "ExternalConnection",
        "return_values_reuse": "ReturnValuesReuse",
    },
}


def _handler(value: object) -> str:
    """`Модуль.Процедура` schema v1 -> `CommonModule.Модуль.Процедура`."""
    text = str(value)
    return text if text.startswith("CommonModule.") else f"CommonModule.{text}"


def _properties_xml(tag: str, props: dict) -> str:
    result = ""
    if tag == "EventSubscription" and props.get("handler"):
        result += (
            "<Source><v8:Type>cfg:CatalogObject.Контрагенты</v8:Type></Source>"
            "<Event>OnWrite</Event>"
            f"<Handler>{escape(_handler(props['handler']))}</Handler>"
        )
    if tag == "ScheduledJob" and props.get("method"):
        result += (
            f"<MethodName>{escape(_handler(props['method']))}</MethodName>"
            "<Use>true</Use><Predefined>false</Predefined>"
        )
    for key, xml_tag in _PROPERTIES.get(tag, {}).items():
        if key not in props:
            continue
        value = props[key]
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        result += f"<{xml_tag}>{escape(text)}</{xml_tag}>"
    return result


def _descriptor(tag: str, name: str, *, synonym: str = "", children: str = "",
                extension: bool = False, properties_xml: str = "") -> bytes:
    properties = f"<Name>{escape(name)}</Name>{properties_xml}"
    if extension:
        properties += "<ObjectBelonging>Adopted</ObjectBelonging>"
    if synonym:
        properties += f"<Synonym>{_localized(synonym)}</Synonym>"
    return _document(tag, properties, children)


def _configuration_xml(
    name: str,
    *,
    synonym: str,
    version: str,
    children: list[tuple[str, str]],
    extension: str | None,
) -> bytes:
    if extension:
        properties = (
            f"<Name>{escape(extension)}</Name>"
            f"<Version>{escape(version)}</Version>"
            f"<NamePrefix>{escape(extension)}_</NamePrefix>"
            "<ObjectBelonging>Adopted</ObjectBelonging>"
            "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
            "<CompatibilityMode></CompatibilityMode>"
        )
    else:
        properties = (
            f"<Name>{escape(name)}</Name>"
            f"<Synonym>{_localized(synonym)}</Synonym>"
            f"<Version>{escape(version)}</Version>"
            "<CompatibilityMode>Version8_3_21</CompatibilityMode>"
        )
    body = "".join(
        f"<{tag}>{escape(child)}</{tag}>" for tag, child in children
    )
    return _document("Configuration", properties, body)


_BY_TAG = {tag: directory for directory, (tag, _ru) in _KINDS.items()}


def _владелец(relative: Path) -> tuple[str, str, str | None, bool] | None:
    """(каталог вида, имя, форма, плоская) для файла кода; None — не код."""
    parts = relative.parts
    if len(parts) == 2 and parts[0] == "CommonModules" and parts[1].endswith(".Module"):
        # Скомпилированный общий модуль иерархической выгрузки.
        return parts[0], parts[1][: -len(".Module")], None, False
    if len(parts) >= 2 and parts[0] in _KINDS:
        форма = parts[3] if len(parts) >= 4 and parts[2] == "Forms" else None
        return parts[0], parts[1], форма, False
    if len(parts) == 1:
        сегменты = parts[0].split(".")
        if len(сегменты) >= 3 and сегменты[0] in _BY_TAG:
            форма = (
                сегменты[3]
                if len(сегменты) >= 5 and сегменты[2] == "Form"
                else None
            )
            return _BY_TAG[сегменты[0]], сегменты[1], форма, True
    return None


def _свой_дескриптор(путь: Path) -> bool:
    """Тест сам положил настоящий дескриптор — его не подменяем."""
    try:
        return NS.encode() in путь.read_bytes()
    except OSError:
        return False


def выгрузка_в_файлы(
    цель: Path,
    *,
    configuration: Configuration,
    корень_кода: Path | None = None,
    extension: str | None = None,
    code_version: str = "",
) -> Path:
    """Собрать каталог полной выгрузки в файлы: структура, код, формы."""
    цель.mkdir(parents=True, exist_ok=False)
    объекты: dict[tuple[str, str], dict] = {}
    if extension is None:
        for obj in configuration.objects.values():
            mapping = _BY_RU.get(obj.kind)
            if mapping is None:
                continue
            объекты[(mapping[0], obj.name)] = {"synonym": obj.synonym, "object": obj}

    плоская = False
    if корень_кода is not None:
        for файл in sorted(корень_кода.rglob("*")):
            if not файл.is_file():
                continue
            relative = файл.relative_to(корень_кода)
            if relative.name == "Configuration.xml":
                continue
            target = цель / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(файл, target)
            владелец = _владелец(relative)
            if владелец is None:
                continue
            directory, name, форма, flat = владелец
            плоская = плоская or flat
            info = объекты.setdefault((directory, name), {"synonym": "", "object": None})
            if форма is not None:
                info.setdefault("forms", set()).add(форма)

    children: list[tuple[str, str]] = []
    for (directory, name), info in sorted(объекты.items()):
        tag = _KINDS[directory][0]
        children.append((tag, name))
        obj = info.get("object")
        attributes = ""
        if obj is not None:
            attributes = "".join(_attribute(field) for field in obj.attributes)
            for part in obj.tabular_parts:
                attributes += (
                    "<TabularSection><Properties>"
                    f"<Name>{escape(part.name)}</Name>"
                    + (f"<Synonym>{_localized(part.synonym)}</Synonym>" if part.synonym else "")
                    + "</Properties><ChildObjects>"
                    + "".join(_attribute(field) for field in part.attributes)
                    + "</ChildObjects></TabularSection>"
                )
        forms = sorted(info.get("forms", ()))
        if tag != "CommonForm":
            attributes += "".join(f"<Form>{escape(form)}</Form>" for form in forms)
        if плоская:
            descriptor = цель / f"{tag}.{name}.xml"
        else:
            (цель / directory).mkdir(exist_ok=True)
            descriptor = цель / directory / f"{name}.xml"
        if not _свой_дескриптор(descriptor):
            descriptor.write_bytes(
                _descriptor(
                    tag,
                    name,
                    synonym=info.get("synonym") or "",
                    children=attributes,
                    extension=extension is not None,
                    properties_xml=_properties_xml(tag, obj.props) if obj is not None else "",
                )
            )
        if tag == "CommonForm":
            continue
        for form in forms:
            if плоская:
                form_descriptor = цель / f"{tag}.{name}.Form.{form}.xml"
            else:
                form_descriptor = цель / directory / name / "Forms" / f"{form}.xml"
                form_descriptor.parent.mkdir(parents=True, exist_ok=True)
            if not _свой_дескриптор(form_descriptor):
                form_descriptor.write_bytes(
                    _descriptor("Form", form, extension=extension is not None)
                )

    (цель / "Configuration.xml").write_bytes(
        _configuration_xml(
            configuration.name,
            synonym=configuration.synonym,
            version=code_version or configuration.version,
            children=children,
            extension=extension,
        )
    )
    return цель


def опубликовать_выгрузку(
    registry,
    каталог: Path,
    *,
    origin: str | None = None,
    parent_configuration: str = "",
    activation: bool = True,
):
    """Тот же путь, что intake/дашборд: probe → collect → convert → publish."""
    tree = DirectoryExportTree(каталог, origin_name=origin, settle_seconds=0)
    probe = probe_export(tree)
    работа = Path(tempfile.mkdtemp(prefix="native-export-", dir=каталог.parent))
    номер = next(_counter)
    collection = collect_source_b(tree, probe, работа / "collection")
    generation = materialize_generation(
        collection,
        convert_collection(collection),
        работа / "generation",
        generation_id=f"generation-test-{номер}",
        parent_configuration=parent_configuration,
    )
    # Без активации поколение выглядит как опубликованное до SOURCE-MODES:
    # реестр показывает его только диагностически (RELOAD_REQUIRED).
    manifest_activation = (
        _activation_for_generation(
            generation.manifest, generation.payloads, registry, f"test-job-{номер}"
        )
        if activation
        else None
    )
    staged = registry.stage_generation(
        generation.manifest, generation.payloads, activation=manifest_activation
    )
    return registry.publish_generation(staged)


def опубликовать_код(
    registry,
    корень_кода: Path | None,
    *,
    configuration: Configuration,
    extension: str | None = None,
    code_version: str = "",
    рабочий: Path,
    origin: str | None = None,
    activation: bool = True,
):
    """Выгрузка в файлы из модели и дерева кода — сразу активное поколение."""
    каталог = выгрузка_в_файлы(
        рабочий / f"выгрузка-{next(_counter)}",
        configuration=configuration,
        корень_кода=корень_кода,
        extension=extension,
        code_version=code_version,
    )
    pointer = опубликовать_выгрузку(
        registry,
        каталог,
        origin=origin,
        parent_configuration=configuration.name if extension else "",
        activation=activation,
    )
    if extension is None:
        _МОДЕЛИ[registry] = configuration
    if extension is None and configuration.platform and activation:
        # Выгрузка в файлы не знает точной сборки платформы; человек
        # объявляет её на странице источников — так же поступает и фикстура,
        # чтобы фильтр справки по версии работал как у schema v1.
        registry.set_platform_version(configuration.name, configuration.platform)
    return pointer


def снимок_кода(
    корень: Path, цель: Path, *, version: str = "", extension: str | None = None
) -> КодВыгрузки:
    """Скопировать дерево кода: последующие правки теста его не меняют."""
    shutil.copytree(корень, цель / "код", ignore=shutil.ignore_patterns("data"))
    return КодВыгрузки(цель / "код", extension, version)


def _код_из_архива(архив: Path) -> КодВыгрузки:
    """ZIP выгрузки в файлы -> дерево кода; вид и имя — по Configuration.xml."""
    import zipfile

    from mcp1c.intake_v2 import SourceKind
    from mcp1c.intake_v2_probe import _identity_from_properties, _properties

    цель = Path(tempfile.mkdtemp(prefix="native-zip-", dir=архив.parent)) / "код"
    with zipfile.ZipFile(архив) as zf:
        свойства = _properties(zf.read("Configuration.xml"))
        вид, имя = _identity_from_properties(свойства)
        for info in zf.infolist():
            if info.is_dir() or info.filename == "Configuration.xml":
                continue
            путь = цель / info.filename
            путь.parent.mkdir(parents=True, exist_ok=True)
            путь.write_bytes(zf.read(info))
    цель.mkdir(parents=True, exist_ok=True)
    return КодВыгрузки(
        цель,
        имя if вид is SourceKind.EXTENSION else None,
        свойства.get("Version", ""),
    )


def загрузить_код(registry, код: "КодВыгрузки | Path", *, configuration: str):
    """Новая полная выгрузка той же конфигурации или её расширения."""
    from conftest import build_configuration

    if not isinstance(код, КодВыгрузки):
        код = _код_из_архива(Path(код))

    модель = _МОДЕЛИ.get(registry) or build_configuration(name=configuration)
    if модель.name != configuration:
        модель = build_configuration(name=configuration)
    опубликовать_код(
        registry,
        код.корень,
        configuration=модель,
        extension=код.extension,
        code_version=код.version,
        рабочий=Path(tempfile.mkdtemp(prefix="native-code-", dir=код.корень.parent)),
    )
    # Как прежний `add_modules`: вернуть Source опубликованного корпуса кода.
    from mcp1c import index_cache

    source_id = (
        f"{configuration}:modules"
        if код.extension is None
        else f"{configuration}:ext:{index_cache.safe_name(код.extension)}"
    )
    with registry._lock:
        loaded = registry.modules.get(source_id)
    # Корпус могли снять сразу после публикации (гонки в тестах remove).
    return loaded.source if loaded is not None else None


def записать_старый_код(
    registry,
    корень_кода: Path,
    *,
    configuration: str,
    extension: str | None = None,
    version: str = "",
    origin: str = "",
    sha256: str = "",
):
    """Состояние data/, которое оставлял удалённый приём кода (`add_modules`).

    Код лежит каталогом в `data/modules/<Конфигурация>` (или
    `data/extensions/<Конфигурация>/<Расширение>`), а в registry.json — строка
    `KIND_MODULES`/`KIND_EXTENSION` рядом со строкой schema v1. Так выглядят
    данные до 4.2.0: startup снимает такие строки с учёта с понятной
    причиной и не трогает каталоги. Реестр должен уже держать сохранённую
    конфигурацию.
    """
    import hashlib
    import json

    from mcp1c import index_cache
    from mcp1c.intake import SELECTION_VERSION
    from mcp1c.registry import KIND_EXTENSION, KIND_MODULES, STATUS_READY, Source

    if extension is None:
        source_id = f"{configuration}:modules"
        root = registry.data_dir / "modules" / index_cache.safe_name(configuration)
        kind = KIND_MODULES
    else:
        source_id = f"{configuration}:ext:{index_cache.safe_name(extension)}"
        root = (
            registry.data_dir
            / "extensions"
            / index_cache.safe_name(configuration)
            / index_cache.safe_name(extension)
        )
        kind = KIND_EXTENSION
    if root.exists():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    # Корень кода у части тестов — сам tmp_path, где лежит и data/.
    служебные = {root.resolve(), registry.data_dir.resolve()}

    def пропустить(каталог: str, имена: list[str]) -> set[str]:
        return {
            имя
            for имя in имена
            if any(
                путь == (Path(каталог) / имя).resolve()
                or (Path(каталог) / имя).resolve() in путь.parents
                for путь in служебные
            )
        }

    shutil.copytree(корень_кода, root, ignore=пропустить)
    файлов = sum(1 for path in root.rglob("*") if path.is_file())
    digest = sha256 or hashlib.sha256(
        b"".join(
            path.relative_to(root).as_posix().encode() + path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        )
    ).hexdigest()
    source = Source(
        id=source_id,
        kind=kind,
        origin=origin or ("расширение.zip" if extension else "модули.zip"),
        sha256=digest,
        loaded_at="2026-10-01T00:00:00+00:00",
        platform="",
        status=STATUS_READY,
        items_total=файлов,
        stored_path=registry._relative(root),
        selection_version=SELECTION_VERSION,
        locator_generation=1,
        code_version=version,
    )
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    rows = [row for row in payload.get("sources", []) if row.get("id") != source_id]
    rows.append(source.to_dict())
    payload["sources"] = rows
    registry.registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return source


def старый_реестр(
    рабочий: Path,
    корень_кода: Path | None,
    *,
    configuration: Configuration,
    extension: str | None = None,
    version: str = "",
):
    """data/ прежней версии: schema v1 и код отдельно; вернуть каталог данных."""
    from conftest import write_export
    from mcp1c.registry import Registry

    входящее = рабочий / "incoming-v1"
    входящее.mkdir(parents=True, exist_ok=True)
    registry = Registry(рабочий / "data")
    registry.add_configuration(write_export(входящее, configuration))
    registry.save()
    if корень_кода is not None:
        записать_старый_код(
            registry,
            корень_кода,
            configuration=configuration.name,
            extension=extension,
            version=version,
        )
    return registry.data_dir
