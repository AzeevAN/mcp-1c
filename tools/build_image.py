#!/usr/bin/env python3
"""Собрать production-образ только из файлов чистого Git HEAD."""

from __future__ import annotations

import argparse
import subprocess
import sys


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", default="mcp1c:local")
    args = parser.parse_args()

    status = _run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        capture_output=True,
        text=True,
    ).stdout
    if status:
        parser.error(
            "production-образ собирается только из чистого Git HEAD; "
            "закоммитьте или уберите изменения"
        )

    revision = _run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"],
        stdout=subprocess.PIPE,
    )
    assert archive.stdout is not None
    try:
        build = subprocess.run(
            [
                "docker",
                "build",
                "--target",
                "runtime",
                "--label",
                f"org.opencontainers.image.revision={revision}",
                "--tag",
                args.image,
                "-",
            ],
            stdin=archive.stdout,
            check=False,
        )
    finally:
        archive.stdout.close()
    archive_status = archive.wait()
    if archive_status != 0:
        raise subprocess.CalledProcessError(archive_status, archive.args)
    if build.returncode != 0:
        raise subprocess.CalledProcessError(build.returncode, build.args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FileNotFoundError as error:
        print(f"Не найдена команда: {error.filename}", file=sys.stderr)
        raise SystemExit(127) from error
