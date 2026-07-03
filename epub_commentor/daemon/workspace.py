"""Per-job workspace management.

Each job owns a self-contained directory under ``<workspace_dir>/jobs/``::

    jobs/job_<id>/
      ├── input.epub              # cp'd in at submit time
      ├── output.commented.epub   # written by comment_epub on SUCCESS
      ├── cache/                  # LLM cache_path — empty after SUCCESS cleanup
      ├── logs/                   # LLM log_dir_path — kept, tar.gz'd on SUCCESS
      ├── commentor.log           # project logger mirror (best-effort)
      └── meta.json               # CommentorResult snapshot on SUCCESS

Isolation is enforced at submit time (input.epub is copied, not symlinked)
and at run_job() time (cache_path / log_dir_path point into this tree).
The cleanup policy keeps ``output.commented.epub`` + ``meta.json`` +
``logs/archive.tar.gz`` after SUCCESS so the operator can still grep
the audit trail; everything else is removed.
"""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

# Anything matching these globs is safe to delete on SUCCESS / prune.
_CACHE_GLOBS: tuple[str, ...] = ("cache/**",)
_LOG_FILES_GLOB: str = "logs/*.log"
_LOG_ARCHIVE_NAME: str = "logs/archive.tar.gz"
_META_FILENAME: str = "meta.json"
_INPUT_FILENAME: str = "input.epub"
_OUTPUT_FILENAME: str = "output.commented.epub"


@dataclass(frozen=True)
class Workspace:
    """Filesystem layout for a single job's isolated working directory."""

    job_id: int
    base_dir: Path  # = workspace_dir.jobs_root

    @property
    def root(self) -> Path:
        return self.base_dir / f"job_{self.job_id}"

    @property
    def input_epub(self) -> Path:
        return self.root / _INPUT_FILENAME

    @property
    def output_epub(self) -> Path:
        return self.root / _OUTPUT_FILENAME

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    @property
    def meta_json(self) -> Path:
        return self.root / _META_FILENAME

    @property
    def log_archive(self) -> Path:
        return self.root / _LOG_ARCHIVE_NAME

    def ensure_dirs(self) -> None:
        """Create cache/ and logs/ if absent.

        ``input.epub`` must already exist when this is called — the
        submit step writes it. The job's root is created transitively.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def archive_logs(self) -> None:
        """Tar+gzip ``logs/*.log`` into ``logs/archive.tar.gz``.

        Called on SUCCESS so the directory doesn't grow unbounded across
        many jobs. Idempotent: if there are no ``.log`` files, the
        archive is left untouched (no empty archive written).
        """
        log_files = sorted(self.log_dir.glob("*.log"))
        if not log_files:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self.log_archive
        # ``a:gz`` would require a complex re-pack; recreate from scratch.
        # The previous archive (if any) is overwritten — there should
        # only ever be one SUCCESS per job id.
        with tarfile.open(archive_path, "w:gz") as tar:
            for log_file in log_files:
                tar.add(log_file, arcname=log_file.name)
        for log_file in log_files:
            try:
                log_file.unlink()
            except FileNotFoundError:
                pass

    def cleanup_cache(self) -> None:
        """Delete the LLM cache directory after a SUCCESS / FAILED.

        On FAILED the cache is removed too because validation failures
        may have polluted entries; we want the next retry to start
        from a clean cache for that job id. On SUCCESS the cache is
        redundant (LLM can re-derive from prompt + response cheaply).
        """
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)

    def remove(self) -> None:
        """Recursively delete the entire workspace.

        Used by ``epubctl prune`` to free disk after the operator has
        shipped the output EPUB elsewhere.
        """
        if self.root.exists():
            shutil.rmtree(self.root)


def jobs_root(workspace_dir: Path) -> Path:
    """Return ``<workspace_dir>/jobs`` (parent of every job_<id> directory)."""
    return workspace_dir / "jobs"


__all__ = ["Workspace", "jobs_root"]
