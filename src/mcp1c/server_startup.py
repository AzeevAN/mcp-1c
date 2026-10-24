"""Проверка окружения и запуск сервера с владением каталогом данных."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .capabilities import (
    CapabilityConfigurationError,
    CapabilityContractError,
    CapabilityRuntime,
    resolve_capability_settings,
)
from .process_restart import RestartController
from .resource_limits import intake_archive_limits
from .runtime_config import (
    ACCESS_HTTP,
    ACCESS_HTTPS_PROXY,
    AccessModeError,
    HostConfigurationError,
    TokenConfigurationError,
    access_mode,
    allowed_hosts as configured_allowed_hosts,
    forwarded_allow_ips as configured_forwarded_allow_ips,
    host_check_required,
    is_loopback_bind,
    require_tokens,
    token_is_strong,
)

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from .registry import Registry


WRITABLE_DATA_DIRECTORIES = (
    "bootstrap",
    "incoming",
    "sources",
    "index",
    "logs",
    "reference",
)


class DataDirectoryError(RuntimeError):
    """Каталог данных не подходит для изменяемого серверного запуска."""


def _data_directory_error(path: Path, error: OSError) -> DataDirectoryError:
    reason = error.strerror or str(error)
    return DataDirectoryError(
        f"Каталог данных `{path}` недоступен для записи: {reason}. "
        f"Процесс запущен с uid={os.geteuid()} gid={os.getegid()}. "
        "Для обычного Docker на Linux остановите контейнер и назначьте "
        "владельца каталога, подключённого как `/data`, на хосте: "
        "`sudo chown -R 10001:10001 <MCP1C_DATA_DIR>`. "
        "Не исправляйте это через `chmod 777` и не запускайте сервер от root."
    )


def _probe_directory_write(path: Path) -> None:
    """Доказать создание и удаление файла фактическим системным вызовом."""
    name = f".mcp1c-write-test-{uuid.uuid4().hex}"
    fd: int | None = None
    created = False
    try:
        directory_fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
            created = True
            os.close(fd)
            fd = None
            os.unlink(name, dir_fd=directory_fd)
            created = False
        finally:
            if fd is not None:
                os.close(fd)
            if created:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)
    except OSError as error:
        raise _data_directory_error(path, error) from error


def require_writable_data(data_dir: str | Path) -> None:
    """Подготовить и проверить каталоги, которые изменяет живой сервер.

    Проверка включается отдельным флагом у Docker-образа. Обычный запуск CLI
    сохраняет возможность читать готовый Registry с read-only носителя; у
    контейнера с загрузкой, словарём и входящими архивами запись обязательна.
    """
    root = Path(data_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _data_directory_error(root, error) from error
    if not root.is_dir():
        raise _data_directory_error(root, NotADirectoryError(str(root)))

    _probe_directory_write(root)
    for name in WRITABLE_DATA_DIRECTORIES:
        directory = root / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _data_directory_error(directory, error) from error
        if not directory.is_dir():
            raise _data_directory_error(directory, NotADirectoryError(str(directory)))
        _probe_directory_write(directory)


def main(
    argv: list[str] | None,
    *,
    description: str | None,
    registry_factory: Callable[[str], Registry],
    build_server: Callable[..., MCPServer],
    require_writable_data: Callable[[str | Path], None],
    run_streamable_http: Callable[..., None],
    ownership_timeout: float,
) -> int:
    parser = argparse.ArgumentParser(prog="mcp1c-server", description=description)
    parser.add_argument("--data", default="data", help="каталог данных сервера")
    parser.add_argument(
        "--transport",
        default="streamable-http",
        choices=("streamable-http", "stdio"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--trust-proxy-headers",
        action="store_true",
        help=(
            "доверять X-Forwarded-*; только за изолированным reverse proxy, "
            "который перезаписывает эти заголовки"
        ),
    )
    parser.add_argument(
        "--require-writable-data",
        action="store_true",
        help=(
            "перед стартом создать рабочие подкаталоги и доказать запись в них; "
            "используется Docker-образом"
        ),
    )
    parser.add_argument(
        "--require-tokens",
        action="store_true",
        help=(
            "до старта потребовать два разных безопасных API_TOKEN и "
            "ADMIN_TOKEN; используется официальным Docker-образом"
        ),
    )
    parser.add_argument(
        "--insecure-open-read",
        action="store_true",
        help=(
            "разрешить HTTP не на loopback без API_TOKEN: чтение структуры "
            "конфигураций откроется всем, кто дотянется до адреса"
        ),
    )
    args = parser.parse_args(argv)
    network_http = args.transport != "stdio" and not is_loopback_bind(args.host)

    try:
        capability_store, enabled_capabilities = resolve_capability_settings(
            args.data,
        )
        access = access_mode()
        if args.require_tokens:
            require_tokens()
        if network_http:
            for name in ("API_TOKEN", "ADMIN_TOKEN"):
                value = os.environ.get(name, "")
                if value and not token_is_strong(value):
                    raise TokenConfigurationError(
                        f"{name}: для сетевого HTTP нужен стойкий случайный "
                        "токен не короче 32 печатных ASCII-символов без пробелов."
                    )
        # Сам бюджет читается лениво, при первом intake-запросе; неверное
        # значение там превращалось в 500 на каждом запросе вместо отказа
        # старта, который оператор увидит сразу.
        try:
            intake_archive_limits()
        except ValueError as error:
            parser.error(str(error))
        hosts: tuple[str, ...] | None = None
        forwarded: str | None = None
        if args.transport != "stdio":
            hosts = (
                configured_allowed_hosts(bind_host=args.host)
                if host_check_required(args.host)
                else None
            )
            forwarded = configured_forwarded_allow_ips(
                args.trust_proxy_headers or access == ACCESS_HTTPS_PROXY
            )
    except (
        AccessModeError,
        CapabilityConfigurationError,
        HostConfigurationError,
        TokenConfigurationError,
    ) as error:
        parser.error(str(error))

    open_network_read = network_http and not os.environ.get("API_TOKEN", "")
    if open_network_read and not args.insecure_open_read:
        # Инвариант «вне своей машины токен чтения обязателен» держится кодом,
        # а не только документацией: MCP отдаёт структуру конфигураций целиком.
        parser.error(
            f"--host {args.host} слушает не только эту машину, а API_TOKEN не "
            "задан: чтение конфигураций было бы открыто всей сети. Задайте "
            "API_TOKEN или осознанно добавьте --insecure-open-read."
        )
    if open_network_read:
        print(
            "Предупреждение: --insecure-open-read открывает чтение без токена "
            "всем, кто дотянется до адреса. Имена, по которым к серверу "
            "обращаются клиенты, перечислите в MCP1C_ALLOWED_HOSTS.",
            file=sys.stderr,
        )

    if args.transport != "stdio" and hosts is None:
        if access == ACCESS_HTTPS_PROXY:
            message = (
                "Предупреждение: проверка Host/Origin выключена; задайте "
                "MCP1C_PUBLIC_BASE_URL или MCP1C_ALLOWED_HOSTS для HTTPS proxy."
            )
        else:
            message = (
                "Предупреждение: проверка Host/Origin выключена; задайте "
                "конкретный MCP1C_PUBLISHED_ADDRESS, MCP1C_PUBLIC_BASE_URL "
                "или MCP1C_ALLOWED_HOSTS."
            )
        print(message, file=sys.stderr)

    if (
        network_http
        and access == ACCESS_HTTPS_PROXY
        and not os.environ.get("MCP1C_TRUSTED_PROXIES", "").strip()
    ):
        print(
            "Предупреждение: MCP1C_ACCESS=https-proxy на сетевом адресе при "
            "пустом MCP1C_TRUSTED_PROXIES доверяет proxy-заголовкам всех "
            "соединений. Укажите адреса или CIDR доверенного reverse proxy.",
            file=sys.stderr,
        )

    if args.transport != "stdio" and access == ACCESS_HTTP:
        print(
            "Предупреждение: MCP1C_ACCESS=http включает прямой HTTP без TLS. "
            "API_TOKEN и ADMIN_TOKEN передаются по сети без шифрования; "
            "ограничьте доступ средствами сети и firewall.",
            file=sys.stderr,
        )

    if args.require_writable_data:
        try:
            require_writable_data(args.data)
        except DataDirectoryError as error:
            parser.error(str(error))

    registry = registry_factory(args.data)
    # Сервер — единственный владелец data/: гостем он не смог бы ни
    # публиковать поколения, ни разбирать журналы. Старый контейнер при
    # обновлении или долгий CLI отпускают замок не сразу, поэтому ждём.
    if not registry.acquire_ownership(
        ownership_timeout,
        on_wait=lambda: print(
            "Каталог данных занят другим процессом mcp1c; ждём до "
            f"{ownership_timeout:.0f} с, пока он его освободит.",
            file=sys.stderr,
        ),
    ):
        print(
            "Каталог данных так и не освободился: его держит другой процесс "
            "mcp1c (сервер или CLI). Остановите его и запустите сервер снова.",
            file=sys.stderr,
        )
        return 2
    for message in registry.startup():
        print(f"  {message}", file=sys.stderr)

    if not registry.snapshot().configurations:
        print(
            "Внимание: не загружено ни одной конфигурации. "
            "Положите выгрузку в data/bootstrap/ или добавьте через reg-add.",
            file=sys.stderr,
        )

    try:
        server = build_server(
            registry,
            transport=args.transport,
            restart=RestartController.from_environment(),
            enabled_capabilities=enabled_capabilities,
            capability_runtime=CapabilityRuntime(
                capability_store,
                active=enabled_capabilities,
            ),
        )
    except CapabilityContractError as error:
        parser.error(str(error))
    if args.transport == "stdio":
        # По stdio сервер разговаривает с одним клиентом, который его и
        # запустил: токен там не с кем проверять и не от кого защищаться.
        server.run(transport="stdio")
    else:
        run_streamable_http(
            server,
            host=args.host,
            port=args.port,
            forwarded_allow_ips=forwarded,
            allowed_hosts=hosts,
        )
    return 0
