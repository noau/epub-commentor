"""``python -m epub_commentor.daemon`` entry point."""

from __future__ import annotations

from .server import build_arg_parser, serve


def main() -> int:
    args = build_arg_parser().parse_args()
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
