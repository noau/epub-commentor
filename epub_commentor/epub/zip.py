import zipfile
from pathlib import Path
from typing import IO

_BUFFER_SIZE = 8192  # 8KB


class Zip:
    def __init__(self, source_path: Path, target_path: Path) -> None:
        source_zip: zipfile.ZipFile | None = None
        target_zip: zipfile.ZipFile | None = None
        try:
            source_zip = zipfile.ZipFile(source_path, "r")
            target_zip = zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED)
        except Exception:
            if source_zip:
                source_zip.close()
            if target_zip:
                target_zip.close()
            raise
        self._source_zip: zipfile.ZipFile = source_zip
        self._target_zip: zipfile.ZipFile = target_zip
        self._processed_files: set[Path] = set()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        try:
            if _exc_type is None:
                all_files = self._source_zip.namelist()
                for file_path in all_files:
                    if file_path.endswith("/"):
                        continue
                    if Path(file_path) not in self._processed_files:
                        self.migrate(Path(file_path))
        finally:
            self._target_zip.close()
            self._source_zip.close()

        return False

    def list_files(self, prefix_path: Path | None = None) -> list[Path]:
        all_files = self._source_zip.namelist()
        if prefix_path is None:
            return [Path(f) for f in all_files]
        prefix = prefix_path.as_posix()
        if not prefix.endswith("/"):
            prefix += "/"
        return [Path(f) for f in all_files if f.startswith(prefix)]

    def migrate(self, path: Path):
        path_str = path.as_posix()
        source_info = self._source_zip.getinfo(path_str)
        with self.read(path) as source_file:
            content = source_file.read()
        self._target_zip.writestr(
            zinfo_or_arcname=source_info,
            data=content,
            compress_type=source_info.compress_type,
        )
        self._processed_files.add(path)

    def read(self, path: Path) -> IO[bytes]:
        return self._source_zip.open(path.as_posix(), "r")

    def replace(self, path: Path) -> IO[bytes]:
        self._processed_files.add(path)
        return self._target_zip.open(path.as_posix(), "w")

    def add(self, path: Path, data: bytes | bytearray | IO[bytes]) -> None:
        """添加一个新文件到 target ZIP（不复制 source 中已有的同名文件）。

        用于注入新资源（如 commentary.css）。如果 ``data`` 是 IO 句柄，会被完整读取。
        """
        path_str = path.as_posix()
        if isinstance(data, (bytes, bytearray)):
            self._target_zip.writestr(path_str, bytes(data), compress_type=zipfile.ZIP_DEFLATED)
        else:
            with data as f:
                self._target_zip.writestr(path_str, f.read(), compress_type=zipfile.ZIP_DEFLATED)
        self._processed_files.add(path)
