"""``dls-plc-tools`` command line — a Typer app with ``gui`` and ``read``
subcommands.

The entry point is named to match the package, so it launches with e.g.::

    uvx dls-plc-tools gui 172.23.241.180
    uvx dls-plc-tools read 172.23.241.180 -l

The heavy modules (PySide6 for the GUI, aphyt for reads) are imported *inside*
each subcommand so ``--version`` / ``--help`` stay fast and don't drag those
dependencies in.
"""

from typing import Annotated

import typer

from . import __version__
from .portforward import DEFAULT_CLUSTER, EIP_PORT

__all__ = ["main"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Tools for interrogating Omron NX PLCs over EtherNet/IP at Diamond.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "-v",
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Tools for interrogating Omron NX PLCs over EtherNet/IP at Diamond."""


@app.command()
def gui(
    ip: Annotated[str | None, typer.Argument(help="PLC IP address to pre-fill")] = None,
    port_forward: Annotated[
        bool,
        typer.Option(
            "-f",
            "--port-forward",
            help="tick the Port forward box and enable the fields below",
        ),
    ] = False,
    node: Annotated[
        str | None,
        typer.Option(
            help="k8s node to pin the socat forward to "
            "(e.g. sr19c-k8s-serv-01.pri.diamond.ac.uk)"
        ),
    ] = None,
    service_ip: Annotated[
        str | None,
        typer.Option(
            "--service-ip",
            help="fixed-pool Service IP, or a DNS name to resolve (e.g. sr19c-pfwd)",
        ),
    ] = None,
    port: Annotated[int, typer.Option(help="forward / EtherNet-IP port")] = EIP_PORT,
    cluster: Annotated[
        str,
        typer.Option(
            help="k8s cluster for the port forward "
            "(only acastus has the forward set up)"
        ),
    ] = DEFAULT_CLUSTER,
) -> None:
    """Launch the Qt GUI for browsing and live-watching PLC tags."""
    from .gui import run_gui

    run_gui(
        ip=ip,
        port_forward=port_forward,
        node=node,
        service_ip=service_ip,
        port=port,
        cluster=cluster,
    )


@app.command()
def read(
    ip: Annotated[str, typer.Argument(help="PLC IP address")],
    list_tags: Annotated[
        bool,
        typer.Option(
            "-l",
            "--list",
            help="list every published user tag and its type "
            "(slow: walks the whole tag dictionary)",
        ),
    ] = False,
    detail: Annotated[
        str | None,
        typer.Option(
            "-d",
            "--detail",
            metavar="STRUCT",
            help="live-updating table of one structure's values, e.g. -d Seq",
        ),
    ] = None,
    show: Annotated[
        str | None,
        typer.Option(
            "-s",
            "--show",
            metavar="STRUCT",
            help="show one structure's members with types and array bounds, "
            "e.g. -s PID",
        ),
    ] = None,
    probe: Annotated[
        str | None,
        typer.Option(
            "-p",
            "--probe",
            metavar="STRUCT",
            help="diagnose a structure aphyt fails to decode: chunk-by-chunk "
            "byte counts, member layout and a per-member decode, e.g. -p Temp",
        ),
    ] = None,
    members: Annotated[
        str | None,
        typer.Option(
            "-m",
            "--members",
            help="comma-separated members to show with --detail; nested members "
            "take a dotted path, e.g. -m Desc,IMG.W "
            "(default: Desc,State,Status,Status_String,ILK_Status)",
        ),
    ] = None,
    expand_all: Annotated[
        bool,
        typer.Option(
            "-e",
            "--expand-all",
            help="with --detail, show every member including a column per "
            "nested-structure leaf, ignoring --members",
        ),
    ] = False,
    interval: Annotated[
        float, typer.Option(help="refresh period in seconds for --detail (min: 0.5)")
    ] = 2.0,
) -> None:
    """Interrogate a PLC from the terminal: summarise structures, list tags, or
    live-watch one structure's values."""
    from .interrogate import read_plc

    read_plc(
        ip,
        list_tags=list_tags,
        detail=detail,
        show=show,
        probe=probe,
        members=members,
        interval=interval,
        expand_all=expand_all,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
