"""Command line entry point: scan, serve, reset, export."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import aggregate as A, portable
from .config import bootstrap, settings as default_settings
from .engine import Engine, local_addresses
from .pricing import PricingTable
from .scanner import Scanner
from .store import Store


def human(n: float) -> str:
    """Short decimal magnitude, for token and event counts."""
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.2f}{unit}"
    return f"{n:.0f}"


def human_bytes(n: float) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("kB", 1 << 10)):
        if abs(n) >= size:
            return f"{n / size:.2f} {unit}"
    return f"{n:.0f} B"


def cmd_scan(args, settings) -> int:
    store = Store(settings.db_path)
    pricing = PricingTable(settings.pricing_path)
    scanner = Scanner(store, settings=settings)

    last = [0.0]

    def on_progress(p) -> None:
        now = time.time()
        if now - last[0] < 0.2 and p.phase != "tailing":
            return
        last[0] = now
        d = p.as_dict()
        pct = 100 * d["bytes_done"] / d["bytes_total"] if d["bytes_total"] else 100.0
        sys.stderr.write(
            f"\r{d['phase']:<11} {d['files_done']:>5}/{d['files_total']:<5} "
            f"{pct:5.1f}%  {d['bytes_per_sec'] / 1e6:6.1f} MB/s  "
            f"raw {human(d['raw_events'])}  new {human(d['new_requests'])}   "
        )
        sys.stderr.flush()

    progress = scanner.scan_once(on_progress=None if args.quiet else on_progress)
    if not args.quiet:
        sys.stderr.write("\n")
    A.ensure_buckets_current(store, pricing)

    totals = A.totals(store, pricing, A.Filters())
    quality = A.data_quality(store, pricing)
    print(
        f"scanned {progress.files_total} files "
        f"({human_bytes(progress.bytes_done)}) in {progress.elapsed:.1f}s"
    )
    print(
        f"  {quality['raw_token_events']:,} raw events -> "
        f"{quality['deduped_requests']:,} requests "
        f"({quality['replay_ratio']:.1f}x replay)"
    )
    print(f"  input {human(totals['input_tokens'])}  cache {totals['cache_rate'] * 100:.2f}%")
    print(
        f"  cost ${totals['cost']:,.2f}  saved ${totals['saved']:,.2f}  "
        f"effective ${totals['effective_rate']:.4f}/Mtok"
    )
    if len(quality["sources"]) > 1:
        print(f"  {'client':<10}{'reqs':>9}{'input':>10}{'cache':>8}{'cost':>12}")
        for s in quality["sources"]:
            print(
                f"  {s['source']:<10}{s['requests']:>9,}"
                f"{human(s['input_tokens']):>10}"
                f"{s['cache_rate'] * 100:>7.1f}%{s['cost']:>12,.2f}"
                f"   ${s['effective_rate']:.4f}/Mtok"
            )
    if quality["unpriced_models"]:
        print(f"  unpriced models: {[m['model'] for m in quality['unpriced_models']]}")
    store.close()
    return 0


def cmd_models(args, settings) -> int:
    store = Store(settings.db_path)
    pricing = PricingTable(settings.pricing_path)
    rows = A.breakdown(store, pricing, A.Filters(), "model")
    print(f"{'model':<26}{'reqs':>8}{'input':>10}{'cache':>8}{'cost':>12}{'$/Mtok':>10}")
    for r in rows:
        print(
            f"{r['key']:<26}{r['requests']:>8}{human(r['input_tokens']):>10}"
            f"{r['cache_rate'] * 100:>7.1f}%{r['cost']:>12,.2f}"
            f"{r['effective_rate']:>10.4f}"
        )
    store.close()
    return 0


def cmd_reset(args, settings) -> int:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(settings.db_path) + suffix)
        if p.exists():
            p.unlink()
            print(f"removed {p}")
    return 0


def cmd_export(args, settings) -> int:
    """Write a portable bundle, or the old summary dump with --summary."""
    store = Store(settings.db_path)
    pricing = PricingTable(settings.pricing_path)
    if args.summary:
        filters = A.Filters()
        payload = {
            "totals": A.totals(store, pricing, filters),
            "by_model": A.breakdown(store, pricing, filters, "model"),
            "by_repo": A.breakdown(store, pricing, filters, "repo"),
            "series_day": A.series(store, pricing, filters, bucket="day", group="model"),
            "quality": A.data_quality(store, pricing),
        }
    else:
        label = args.label or store.get_meta("local_label") or portable.default_label()
        origins = None
        if args.origins:
            origins = ["" if o == "local" else o for o in args.origins.split(",")]
        payload = portable.export_bundle(
            store, pricing, label=label, origins=origins
        )
    if args.out:
        args.out.write_text(json.dumps(payload, default=str))
        summary = payload.get("summary", {})
        print(
            f"wrote {args.out} "
            f"({summary.get('requests', 0):,} requests, "
            f"{len(payload.get('buckets', []))} buckets, "
            f"{len(payload.get('sessions', []))} session rows)"
        )
    else:
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    store.close()
    return 0


def cmd_import(args, settings) -> int:
    store = Store(settings.db_path)
    try:
        bundle = json.loads(args.file.read_text())
    except (OSError, ValueError) as exc:
        print(f"cannot read {args.file}: {exc}", file=sys.stderr)
        return 2
    try:
        info = portable.preview(store, bundle)
    except portable.BundleError as exc:
        print(f"not importable: {exc}", file=sys.stderr)
        return 2
    label = args.label or info["label"]
    result = portable.import_bundle(store, bundle, label)
    pricing = PricingTable(settings.pricing_path)
    A.ensure_buckets_current(store, pricing)
    print(
        f"imported {result['buckets']} buckets and {result['sessions']} session rows"
        f" as {result['origin']!r}"
    )
    store.close()
    return 0


def cmd_machines(args, settings) -> int:
    store = Store(settings.db_path)
    pricing = PricingTable(settings.pricing_path)
    rows = portable.list_origins(store, pricing, A)
    print(f"{'machine':<24}{'reqs':>10}{'input':>10}{'cache':>8}{'cost':>12}")
    for r in rows:
        marker = "*" if r["local"] else " "
        print(
            f"{marker}{r['label']:<23}{r['requests']:>10,}"
            f"{human(r['input_tokens']):>10}{r['cache_rate'] * 100:>7.1f}%"
            f"{r['cost']:>12,.2f}"
        )
    store.close()
    return 0


def cmd_serve(args, settings) -> int:
    import uvicorn

    from .server import create_app

    settings = replace(settings, host=args.host or settings.host, port=args.port or settings.port)
    app = create_app(settings, watch=not args.no_watch)

    lines = ["Agent Cache Monitor"]
    for source in app.state.engine.scanner.sources:
        root = source.watch_roots[0] if source.watch_roots else "-"
        lines.append(f"  {source.label:<12}{root}")
    lines += [
        f"  {'database':<12}{settings.db_path}",
        f"  {'pricing':<12}{settings.pricing_path}",
    ]
    # Flushed explicitly so the banner appears immediately even when stdout is
    # redirected to a file and therefore block-buffered.
    print("\n".join(lines), flush=True)

    # An SSE stream only ends when its client goes away, so a graceful shutdown
    # that waits for open connections waits forever on any dashboard left open
    # -- the process would stop serving on SIGTERM and then never exit, which
    # under systemd means every stop and restart hits the kill timeout.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level=args.log_level,
            timeout_graceful_shutdown=5,
            # Explicitly off. Uvicorn trusts X-Forwarded-For from 127.0.0.1 by
            # default, so behind any same-host proxy every LAN client would
            # arrive as a loopback address and the update endpoint's peer check
            # would be satisfied by a header the client can send itself.
            proxy_headers=False,
        )
    )

    # The URLs are printed by a watcher on ``server.started`` rather than up
    # front, so they are never a promise the server has not yet kept: if the
    # bind fails or startup stalls, no address is ever advertised.
    finished = threading.Event()

    def announce() -> None:
        while not finished.wait(0.05):
            if not server.started:
                continue
            out = [f"  {'serving':<12}{url}" for url in local_addresses(settings.port)]
            if settings.host == "0.0.0.0":
                out.append("  (bound on all interfaces)")
            print("\n".join(out), flush=True)
            return

    threading.Thread(target=announce, name="ccm-announce", daemon=True).start()
    try:
        server.run()
    finally:
        finished.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccm", description=__doc__)
    parser.add_argument("--sessions", type=Path, help="override the sessions directory")
    parser.add_argument("--db", type=Path, help="override the database path")
    parser.add_argument("--pricing", type=Path, help="override the pricing table")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan the corpus and print a summary")
    p_scan.add_argument("-q", "--quiet", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    p_serve = sub.add_parser("serve", help="run the web UI")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.add_argument("--no-watch", action="store_true", help="disable live updates")
    p_serve.add_argument("--log-level", default="warning")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("models", help="per-model table").set_defaults(func=cmd_models)
    sub.add_parser("reset", help="delete derived state").set_defaults(func=cmd_reset)

    p_export = sub.add_parser("export", help="write a portable stats bundle")
    p_export.add_argument("-o", "--out", type=Path, help="write here instead of stdout")
    p_export.add_argument("--label", help="name this data carries into other machines")
    p_export.add_argument(
        "--origins",
        help="comma-separated machines to include (default: all; 'local' is this one)",
    )
    p_export.add_argument(
        "--summary", action="store_true", help="dump computed aggregates instead"
    )
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser("import", help="load a bundle from another machine")
    p_import.add_argument("file", type=Path)
    p_import.add_argument("--label", help="override the label carried in the bundle")
    p_import.set_defaults(func=cmd_import)

    sub.add_parser("machines", help="list local and imported data").set_defaults(
        func=cmd_machines
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")

    settings = default_settings
    if args.sessions:
        settings = replace(settings, sessions_dir=args.sessions)
    if args.db:
        settings = replace(settings, db_path=args.db)
    if args.pricing:
        settings = replace(settings, pricing_path=args.pricing)

    bootstrap(settings)
    return args.func(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
