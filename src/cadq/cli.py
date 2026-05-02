"""`cadq` CLI — Typer-powered, JSON-first.

Every command supports ``--format json`` (default) and ``--format text``.
Outputs are stable so an AI harness can parse them deterministically.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from cadq import __version__
from cadq import config as user_config
from cadq.ingest import (
    _dwg_to_dxf,
    find_oda_converter_with_source,
    ingest as ingest_file,
)
from cadq.queries import (
    adjacent_features,
    elevation_at,
    elevation_extreme,
    elevation_profile,
    explain as explain_target,
    feature_area,
    feature_boundary,
    feature_to_dict,
    garden_area,
    get_feature,
    info as info_drawing,
    label_search,
    list_features,
    list_layers,
    nearest_features,
    plan as plan_query,
)
from cadq.store import find_default_cache


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Semantic query CLI for DWG/DXF drawings.",
)
console = Console()


# --- shared helpers --------------------------------------------------------


def _resolve_cache(cache: Path | None) -> Path:
    if cache:
        if not cache.exists():
            typer.echo(f"Cache not found: {cache}", err=True)
            raise typer.Exit(2)
        return cache
    found = find_default_cache(Path.cwd())
    if not found:
        typer.echo(
            "No .cadqcache found in the current folder. "
            "Run `cadq ingest <file.dwg|.dxf>` first, or pass --cache.",
            err=True,
        )
        raise typer.Exit(2)
    return found


def _emit(payload: Any, fmt: str) -> None:
    if fmt == "json":
        typer.echo(json.dumps(payload, indent=2, default=str))
    elif fmt == "text":
        _emit_text(payload)
    else:
        typer.echo(f"Unknown format: {fmt}", err=True)
        raise typer.Exit(2)


def _emit_text(payload: Any) -> None:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        keys = list(payload[0].keys())
        table = Table(*keys, show_lines=False)
        for row in payload:
            table.add_row(*[str(row.get(k, "")) for k in keys])
        console.print(table)
    elif isinstance(payload, dict):
        for k, v in payload.items():
            console.print(f"[bold]{k}[/bold]: {v}")
    else:
        console.print(payload)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """cadq — semantic query CLI for DWG/DXF drawings."""


# --- commands --------------------------------------------------------------


@app.command()
def ingest(
    source: Path = typer.Argument(..., exists=True, readable=True, help="DWG or DXF input."),
    rules: Path | None = typer.Option(None, help="Override ontology rules YAML."),
    cache: Path | None = typer.Option(None, help="Output cache path. Defaults to <source>.cadqcache."),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Parse a drawing and build a `.cadqcache` index."""
    out = ingest_file(source, rules=rules, cache=cache)
    payload = {"cache": str(out), "source": str(source)}
    _emit(payload, fmt)


@app.command()
def info(
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Show drawing metadata and entity counts."""
    cp = _resolve_cache(cache)
    _emit(asdict(info_drawing(cp)), fmt)


layers_app = typer.Typer(help="Layer inspection.")
app.add_typer(layers_app, name="layers")


@layers_app.command("list")
def layers_list(
    filter_: str | None = typer.Option(None, "--filter", help="Substring filter on layer name."),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """List layers and their ontology mappings."""
    cp = _resolve_cache(cache)
    _emit(list_layers(cp, name_filter=filter_), fmt)


features_app = typer.Typer(help="Semantic features.")
app.add_typer(features_app, name="features")


@features_app.command("list")
def features_list(
    type_: str | None = typer.Option(None, "--type", help="Ontology prefix, e.g. 'landscape.softscape.lawn'."),
    layer: str | None = typer.Option(None),
    geom_kind: str | None = typer.Option(
        None, "--geom-kind",
        help="Filter by geometry type: polygon | line | point.",
    ),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """List semantic features."""
    cp = _resolve_cache(cache)
    rows = [
        feature_to_dict(f)
        for f in list_features(
            cp, ontology_prefix=type_, layer=layer, geom_kind=geom_kind,
        )
    ]
    _emit(rows, fmt)


@app.command("feature")
def feature_get(
    feature_id: str = typer.Argument(...),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Get a single feature record (with evidence)."""
    cp = _resolve_cache(cache)
    f = get_feature(cp, feature_id)
    if not f:
        typer.echo(f"No such feature: {feature_id}", err=True)
        raise typer.Exit(1)
    _emit(feature_to_dict(f), fmt)


@app.command()
def area(
    feature: str = typer.Option(..., "--feature"),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Return the area of a feature."""
    cp = _resolve_cache(cache)
    out = feature_area(cp, feature)
    if out is None:
        typer.echo(f"No such feature: {feature}", err=True)
        raise typer.Exit(1)
    _emit(out, fmt)


@app.command()
def boundary(
    feature: str = typer.Option(..., "--feature"),
    out_format: str = typer.Option("geojson", "--format", "-f", help="geojson | wkt"),
    cache: Path | None = typer.Option(None),
) -> None:
    """Return the boundary of a feature as GeoJSON or WKT."""
    cp = _resolve_cache(cache)
    out = feature_boundary(cp, feature, fmt=out_format)
    if out is None:
        typer.echo(f"No such feature: {feature}", err=True)
        raise typer.Exit(1)
    if isinstance(out, str):
        typer.echo(out)
    else:
        typer.echo(json.dumps(out, indent=2, default=str))


elevation_app = typer.Typer(help="Elevation queries.")
app.add_typer(elevation_app, name="elevation")


@elevation_app.command("max")
def elevation_max(
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Return the highest elevation sample on the drawing."""
    cp = _resolve_cache(cache)
    out = elevation_extreme(cp, mode="max")
    if out is None:
        typer.echo("No elevation samples found.", err=True)
        raise typer.Exit(1)
    _emit(out, fmt)


@elevation_app.command("min")
def elevation_min(
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Return the lowest elevation sample on the drawing."""
    cp = _resolve_cache(cache)
    out = elevation_extreme(cp, mode="min")
    if out is None:
        typer.echo("No elevation samples found.", err=True)
        raise typer.Exit(1)
    _emit(out, fmt)


@elevation_app.command("at")
def elevation_at_cmd(
    x: float = typer.Argument(...),
    y: float = typer.Argument(...),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Estimate elevation at (x, y) via IDW on nearest samples."""
    cp = _resolve_cache(cache)
    out = elevation_at(cp, x, y)
    if out is None:
        typer.echo("No elevation samples found.", err=True)
        raise typer.Exit(1)
    _emit(out, fmt)


@elevation_app.command("profile")
def elevation_profile_cmd(
    x1: float = typer.Argument(...),
    y1: float = typer.Argument(...),
    x2: float = typer.Argument(...),
    y2: float = typer.Argument(...),
    samples: int = typer.Option(25, help="Number of profile samples."),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Sample elevation along a line (x1,y1) → (x2,y2)."""
    cp = _resolve_cache(cache)
    out = elevation_profile(cp, x1, y1, x2, y2, samples=samples)
    if out is None:
        typer.echo("No elevation samples found.", err=True)
        raise typer.Exit(1)
    _emit(out, fmt)


@app.command()
def nearest(
    to: str = typer.Option(..., "--to", help="Source feature id."),
    type_: str | None = typer.Option(None, "--type", help="Ontology prefix filter."),
    limit: int = typer.Option(5),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Find features nearest to a given feature."""
    cp = _resolve_cache(cache)
    _emit(nearest_features(cp, to=to, type_prefix=type_, limit=limit), fmt)


topology_app = typer.Typer(help="Spatial relationships.")
app.add_typer(topology_app, name="topology")


@topology_app.command("adjacent")
def topology_adjacent_cmd(
    to: str = typer.Option(..., "--to"),
    tolerance: float = typer.Option(1e-6, help="Buffer applied before intersect test."),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Features that touch / overlap / abut a given feature."""
    cp = _resolve_cache(cache)
    _emit(adjacent_features(cp, to=to, tolerance=tolerance), fmt)


label_app = typer.Typer(help="Text label search.")
app.add_typer(label_app, name="label")


@label_app.command("search")
def label_search_cmd(
    pattern: str = typer.Argument(..., help="Glob-style pattern, e.g. 'drive*'."),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Search drawing text for a pattern."""
    cp = _resolve_cache(cache)
    _emit(label_search(cp, pattern), fmt)


@app.command()
def plan(
    question: str = typer.Argument(..., help="Natural-language question."),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Suggest a tool sequence for a natural-language question.

    Does not execute anything — useful for AI harnesses to dry-run.
    """
    _emit(plan_query(question), fmt)


@app.command()
def explain(
    target_id: str = typer.Argument(..., help="Layer name or feature id."),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Show why a target was classified as it was."""
    cp = _resolve_cache(cache)
    _emit(explain_target(cp, target_id), fmt)


@app.command()
def garden(
    subtract_hedges: bool = typer.Option(
        False, "--subtract-hedges/--keep-hedges",
        help="Subtract hedge polygons from the garden area.",
    ),
    subtract_canopies: bool = typer.Option(
        False, "--subtract-canopies/--keep-canopies",
        help="Subtract tree canopy polygons (rarely wanted).",
    ),
    extra_subtract: list[str] = typer.Option(
        [], "--subtract",
        help="Additional ontology prefix to subtract (repeatable).",
    ),
    include_geometry: bool = typer.Option(
        False, "--with-geometry",
        help="Include the residual GeoJSON in the output.",
    ),
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Derive the garden / open ground area as `site - built features`.

    Site polygon = closed `site.boundary` if present, else polygonized
    `site.boundary` lines, else convex hull (with a warning).  Subtracts
    `building.*`, `landscape.hardscape.*`, `landscape.water`, and
    `services.drainage.manhole` by default.
    """
    cp = _resolve_cache(cache)
    out = garden_area(
        cp,
        subtract_hedges=subtract_hedges,
        subtract_canopies=subtract_canopies,
        extra_subtract=tuple(extra_subtract),
    )
    if out is None:
        typer.echo(
            "No site.boundary feature found. The drawing may not have a "
            "boundary layer (e.g. SiteBdy/AssumedBoundary/TitleLine). "
            "Either add one and re-ingest, or use `cadq features list "
            "--type site.boundary` to confirm.",
            err=True,
        )
        raise typer.Exit(1)
    if not include_geometry:
        out.pop("geometry_geojson", None)
    _emit(out, fmt)


@app.command()
def trees(
    cache: Path | None = typer.Option(None),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Count unique trees (after trunk-in-canopy de-duplication).

    Equivalent to `features list --type landscape.softscape.tree` but
    returns a one-line summary that's friendly to shell pipelines and AI
    tool calls.
    """
    cp = _resolve_cache(cache)
    rows = list_features(cp, ontology_prefix="landscape.softscape.tree")
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r.geom_kind] = by_kind.get(r.geom_kind, 0) + 1
    payload = {
        "count": len(rows),
        "by_geom_kind": by_kind,
        "feature_ids": [r.id for r in rows],
    }
    _emit(payload, fmt)


# --- ODA File Converter management ----------------------------------------


ODA_DOWNLOAD_URL = "https://www.opendesign.com/guestfiles/oda_file_converter"


oda_app = typer.Typer(
    help=(
        "Manage the ODA File Converter dependency.\n\n"
        "DWG input requires the free ODA File Converter (cadq parses DXF "
        "natively). Use these commands to detect, configure, or open the "
        "installer.  See `cadq oda install` for step-by-step instructions."
    ),
)
app.add_typer(oda_app, name="oda")


@oda_app.command("status")
def oda_status(
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Report whether the ODA File Converter is detected and where."""
    found, source = find_oda_converter_with_source()
    payload = {
        "available": bool(found),
        "path": found,
        "source": source,  # 'env' | 'config' | 'path' | 'auto' | None
        "config_file": str(user_config.config_path()),
        "download_url": ODA_DOWNLOAD_URL,
    }
    if not found:
        payload["hint"] = (
            "Run `cadq oda install` for download instructions, or "
            "`cadq oda set-path <full path to ODAFileConverter.exe>` "
            "if it is already installed in a custom location."
        )
    _emit(payload, fmt)


@oda_app.command("where")
def oda_where() -> None:
    """Print the resolved ODA converter path (or exit non-zero if not found)."""
    found, _ = find_oda_converter_with_source()
    if not found:
        typer.echo("ODA File Converter not found.", err=True)
        raise typer.Exit(1)
    typer.echo(found)


@oda_app.command("set-path")
def oda_set_path(
    path: Path = typer.Argument(
        ..., exists=True, readable=True,
        help="Full path to ODAFileConverter.exe.",
    ),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Persist a custom ODA File Converter path in user config."""
    saved = user_config.set_("oda_file_converter", str(path))
    _emit(
        {
            "ok": True,
            "oda_file_converter": str(path),
            "config_file": str(saved),
        },
        fmt,
    )


@oda_app.command("clear")
def oda_clear(
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Forget the persisted ODA File Converter path."""
    saved = user_config.unset("oda_file_converter")
    _emit({"ok": True, "config_file": str(saved)}, fmt)


@oda_app.command("install")
def oda_install(
    open_browser: bool = typer.Option(
        True, "--open/--no-open",
        help="Open the ODA download page in the default browser.",
    ),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Print install instructions and optionally open the download page.

    cadq cannot redistribute the ODA File Converter or accept its EULA on
    your behalf, so this command guides you through the manual install.
    """
    instructions = [
        f"1. Open the ODA download page: {ODA_DOWNLOAD_URL}",
        "2. Sign in or register a free account (required by ODA).",
        "3. Download the installer for your OS (Windows: 64-bit MSI).",
        "4. Run the installer and accept the ODA licence.",
        "5. Re-run `cadq oda status` to confirm cadq detects it.",
        "   If it installed to a non-standard folder, run "
        "`cadq oda set-path <path-to-ODAFileConverter.exe>` once.",
    ]
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(ODA_DOWNLOAD_URL)
        except Exception:
            # Headless or restricted env - just print the link below.
            pass
    _emit(
        {
            "download_url": ODA_DOWNLOAD_URL,
            "steps": instructions,
            "config_file": str(user_config.config_path()),
        },
        fmt,
    )


@oda_app.command("convert")
def oda_convert(
    source: Path = typer.Argument(..., exists=True, readable=True, help="Input .dwg"),
    output: Path | None = typer.Option(
        None, "--output", "-o",
        help="Output .dxf path (defaults to <source>.dxf next to the source).",
    ),
    fmt: str = typer.Option("json", "--format", "-f"),
) -> None:
    """Convert a DWG to DXF using the configured ODA File Converter.

    Useful as a standalone step when you want to keep the DXF and re-ingest
    it later without invoking ODA every time.
    """
    if source.suffix.lower() != ".dwg":
        typer.echo(f"Expected a .dwg file, got: {source.suffix}", err=True)
        raise typer.Exit(2)
    found, _ = find_oda_converter_with_source()
    if not found:
        typer.echo(
            "ODA File Converter not found. Run `cadq oda install` for "
            "instructions, or `cadq oda set-path <path>` if already installed.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        dxf_tmp = _dwg_to_dxf(source)
    except Exception as exc:  # surface the underlying error verbatim
        typer.echo(f"DWG -> DXF conversion failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    target = output or source.with_suffix(".dxf")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    Path(dxf_tmp).replace(target)
    _emit({"input": str(source), "output": str(target), "converter": found}, fmt)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app() or 0)
