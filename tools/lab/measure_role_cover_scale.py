"""Масштаб подбора ролей: `find_roles_for_access` и страницы `get_role_access`.

Подбор минимального набора ролей — DP по маскам операций: на каждого
кандидата перебираются все уже достижимые объединения (до 2¹⁶). Здесь он
меряется на ролях настоящих выгрузок и на синтетике по их образцу.

Запуск из корня проекта (каталог для SQLite — временный, в нём ничего, кроме
построенных здесь баз, нет):

    .venv/bin/python tools/lab/measure_role_cover_scale.py /tmp/roles \\
        BUH.zip ZUP.zip Retail.zip --synth 100,300,1000,1800 --stress

Архивы читаются через ``zipfile`` и не извлекаются: каждый `Rights.xml`
разбирается тем же `role_access._parse_rights`, что и при приёме, в SQLite
той же схемы. Синтетика строится из выгрузки с наибольшим числом ролей: до
числа её ролей — случайное подмножество как есть; сверх — клоны случайной
роли, у которых каждое `true` гаснет с p=0,15, а `false` загорается с
p=0,05. Распределение прав по объектам остаётся настоящим, а маски
операций у ролей перестают совпадать дословно.

Для каждой базы берутся три объекта с наибольшим числом ролей, дающих
право из `OPERATION_RIGHTS`, и меряется первая и последняя страница по всем
16 операциям и по четырём типовым (медиана из 15). Имена ролей и объектов
не печатаются — только числа; база называется по имени файла архива.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import statistics
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, "src")

from mcp1c import role_access  # noqa: E402
from mcp1c.role_access import OPERATION_RIGHTS, RoleAccessIndex  # noqa: E402

TYPICAL = ("read", "update", "insert", "posting")

# Тот же SQL агрегата объектов роли, что в `role_access._build_database`.
_ROOT_PATH = (
    "CASE WHEN instr(substr(t.path,instr(t.path,'.')+1),'.')=0 "
    "THEN t.path ELSE substr(t.path,1,instr(t.path,'.')+"
    "instr(substr(t.path,instr(t.path,'.')+1),'.')-1) END"
)
_ROOT_KEY = _ROOT_PATH.replace("t.path", "t.path_key")


def _create(path: Path) -> sqlite3.Connection:
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(role_access._SCHEMA)
    return connection


def _finish(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO role_objects(role_id,target,target_key,root_grants,"
        "descendant_grants,descendant_targets,conditional_grants) "
        f"SELECT r.role_id,min({_ROOT_PATH}),{_ROOT_KEY},"
        f"sum(t.path_key=({_ROOT_KEY})),sum(t.path_key<>({_ROOT_KEY})),"
        f"count(DISTINCT CASE WHEN t.path_key<>({_ROOT_KEY}) THEN t.path_key END),"
        "sum(r.conditional) FROM rights r JOIN targets t ON t.id=r.target_id "
        f"WHERE r.value=1 GROUP BY r.role_id,{_ROOT_KEY}"
    )
    connection.commit()
    connection.execute("ANALYZE")
    connection.commit()
    connection.close()


def build_real(archive: Path, out: Path) -> int:
    connection = _create(out)
    with zipfile.ZipFile(archive) as bundle:
        members = sorted(
            name
            for name in bundle.namelist()
            if len(PurePosixPath(name).parts) >= 4
            and PurePosixPath(name).parts[-4] == "Roles"
            and PurePosixPath(name).parts[-2:] == ("Ext", "Rights.xml")
        )
        for role_id, member in enumerate(members, 1):
            name = PurePosixPath(member).parts[-3]
            connection.execute(
                "INSERT INTO roles(id,name,name_key,uuid,comment,xml_version) "
                "VALUES (?,?,?,?,?,?)",
                (role_id, name, name.casefold(), str(uuid.uuid4()), "", ""),
            )
            with bundle.open(member) as stream:
                flags = role_access._parse_rights(stream, connection, role_id)
            connection.execute(
                "UPDATE roles SET set_for_new=?,set_for_attributes=?,"
                "independent_children=? WHERE id=?",
                (*map(int, flags), role_id),
            )
    _finish(connection)
    return len(members)


def build_synthetic(real: Path, total: int, out: Path, seed: int = 7) -> None:
    rnd = random.Random(seed)
    connection = _create(out)
    connection.execute("ATTACH ? AS src", (str(real),))
    for table in ("targets", "right_names", "conditions"):
        connection.execute(f"INSERT INTO {table} SELECT * FROM src.{table}")
    roles = connection.execute("SELECT id,name FROM src.roles ORDER BY id").fetchall()
    rights: dict[int, list[tuple]] = {}
    for row in connection.execute(
        "SELECT role_id,target_id,right_name_id,value,conditional FROM src.rights"
    ):
        rights.setdefault(row[0], []).append(row[1:])
    plan = [(role, name, False) for role, name in rnd.sample(roles, min(total, len(roles)))]
    for number in range(total - len(plan)):
        role, name = rnd.choice(roles)
        plan.append((role, f"{name}Клон{number:04d}", True))
    for role_id, (source_id, name, perturb) in enumerate(plan, 1):
        connection.execute(
            "INSERT INTO roles(id,name,name_key,uuid,comment,xml_version,set_for_new,"
            "set_for_attributes,independent_children) VALUES (?,?,?,?,?,?,0,1,0)",
            (role_id, name, name.casefold(), str(uuid.UUID(int=rnd.getrandbits(128))), "", ""),
        )
        rows = []
        for target_id, right_id, value, conditional in rights.get(source_id, ()):
            if perturb:
                if value and rnd.random() < 0.15:
                    value = 0
                elif not value and rnd.random() < 0.05:
                    value = 1
            rows.append((role_id, target_id, right_id, value, conditional))
        connection.executemany(
            "INSERT INTO rights(role_id,target_id,right_name_id,value,conditional) "
            "VALUES (?,?,?,?,?)",
            rows,
        )
    connection.commit()
    connection.execute("DETACH src")
    _finish(connection)


def _median_ms(call, repeat: int = 15) -> float:
    call()
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def measure(label: str, path: Path) -> None:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    index = RoleAccessIndex(connection, path, from_cache=True, ephemeral=False)
    roles = connection.execute("SELECT count(*) FROM roles").fetchone()[0]
    names = ",".join("?" for _ in OPERATION_RIGHTS)
    top = connection.execute(
        "SELECT t.path, count(DISTINCT r.role_id) n FROM rights r "
        "JOIN targets t ON t.id=r.target_id JOIN right_names q ON q.id=r.right_name_id "
        f"WHERE r.value=1 AND q.name_key IN ({names}) "
        "GROUP BY r.target_id ORDER BY n DESC, t.path LIMIT 3",
        tuple(value.casefold() for value in OPERATION_RIGHTS.values()),
    ).fetchall()
    for target, granted in top:
        parts = target.split(".")
        full_name, child = ".".join(parts[:2]), ".".join(parts[2:])
        for title, operations in (("16 оп.", tuple(OPERATION_RIGHTS)), ("4 оп.", TYPICAL)):
            def page(offset: int = 0):
                return index.find_roles_for_access(
                    full_name, operations, child_path=child, offset=offset, limit=20
                )

            first = page()
            last = max(0, (first.candidates_total - 1) // 20 * 20)
            print(
                f"{label:16} ролей {roles:5} | объект: ролей с правом {granted:4} | "
                f"{title:6} | кандидатов {first.candidates_total:4} | "
                f"стр. 1 {_median_ms(page):6.2f} мс, последняя "
                f"{_median_ms(lambda: page(last)):6.2f} мс"
            )
    role, rights_total = connection.execute(
        "SELECT p.name, count(*) n FROM rights r JOIN roles p ON p.id=r.role_id "
        "GROUP BY r.role_id ORDER BY n DESC LIMIT 1"
    ).fetchone()
    target = connection.execute(
        "SELECT o.target FROM role_objects o JOIN roles p ON p.id=o.role_id "
        "WHERE p.name=? ORDER BY o.root_grants+o.descendant_grants DESC LIMIT 1",
        (role,),
    ).fetchone()[0]
    subtree = index.role_access(role, target=target, subtree=True, limit=100).total
    print(
        f"{label:16} get_role_access, роль с {rights_total} правами: summary "
        f"{_median_ms(lambda: index.role_objects(role, limit=50)):.2f} мс; audit "
        f"({subtree} прав объекта) "
        f"{_median_ms(lambda: index.role_access(role, target=target, subtree=True, limit=100)):.2f} мс; "
        f"list_roles, последняя страница "
        f"{_median_ms(lambda: index.list_roles(offset=max(0, roles - 100), limit=100)):.2f} мс"
    )
    index.close()


def stress(workdir: Path) -> None:
    """Верхняя граница DP: один объект, у ролей случайные маски 16 операций.

    В настоящих правах так не бывает — маски ролей одного объекта почти
    совпадают, — но время DP растёт с числом достижимых объединений, а не
    с числом ролей, и эта граница показывает, где оно кончается.
    """
    path = workdir / "stress.roles.sqlite"
    for roles, probability in ((25, 0.5), (62, 0.5), (62, 0.2), (200, 0.2)):
        rnd = random.Random(roles)
        connection = _create(path)
        connection.execute(
            "INSERT INTO targets(id,path,path_key) VALUES (1,'Document.Тест','document.тест')"
        )
        for number, right in enumerate(OPERATION_RIGHTS.values(), 1):
            connection.execute(
                "INSERT INTO right_names(id,name,name_key) VALUES (?,?,?)",
                (number, right, right.casefold()),
            )
        for role_id in range(1, roles + 1):
            name = f"Роль{role_id:04d}"
            connection.execute(
                "INSERT INTO roles(id,name,name_key,uuid,comment,xml_version) "
                "VALUES (?,?,?,?,?,?)",
                (role_id, name, name.casefold(), f"stress-{role_id}", "", ""),
            )
            for number in range(1, len(OPERATION_RIGHTS) + 1):
                if rnd.random() < probability:
                    connection.execute(
                        "INSERT INTO rights(role_id,target_id,right_name_id,value,"
                        "conditional) VALUES (?,1,?,1,0)",
                        (role_id, number),
                    )
        _finish(connection)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        index = RoleAccessIndex(connection, path, from_cache=True, ephemeral=False)
        start = time.perf_counter()
        result = index.find_roles_for_access(
            "Document.Тест", tuple(OPERATION_RIGHTS), limit=20
        )
        print(
            f"граница DP: ролей {roles:4}, p={probability} | кандидатов "
            f"{result.candidates_total:4} | {(time.perf_counter() - start) * 1000:.0f} мс"
        )
        index.close()
    path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--synth", default="", help="числа ролей через запятую")
    parser.add_argument(
        "--stress", action="store_true", help="ещё и верхняя граница DP (до 15 с)"
    )
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)
    if args.stress:
        stress(args.workdir)
    built: list[tuple[str, Path, int]] = []
    for archive in args.archives:
        out = args.workdir / f"{archive.stem}.roles.sqlite"
        built.append((archive.stem, out, build_real(archive, out)))
    largest = max(built, key=lambda item: item[2])[1]
    for total in filter(None, args.synth.split(",")):
        out = args.workdir / f"synth{int(total)}.roles.sqlite"
        build_synthetic(largest, int(total), out)
        built.append((f"синтетика {int(total)}", out, int(total)))
    for label, path, _roles in built:
        measure(label, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
