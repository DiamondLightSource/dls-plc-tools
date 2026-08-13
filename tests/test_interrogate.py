"""CLI column selection and structure printing over nested members."""

import struct as _struct
from types import SimpleNamespace

from dls_plc_tools.interrogate import (
    _watch_columns,
    read_plc,
    show_struct,
    summarise_struct,
)
from dls_plc_tools.structure import flatten
from nested import make_struct


def fake_eip(struct):
    return SimpleNamespace(read_variable=lambda name: struct)


def paths(columns):
    return [column.member.path for column in columns]


def test_selecting_a_plain_member():
    members, _ = flatten(make_struct(), "Gauge")
    assert paths(_watch_columns(members, ["Desc", "State"], False)) == ["Desc", "State"]


def test_naming_a_branch_keeps_it_collapsed():
    members, _ = flatten(make_struct(), "Gauge")
    # -m Ilk gives the compact summary column rather than nothing.
    assert paths(_watch_columns(members, ["Ilk"], False)) == ["Ilk"]


def test_selecting_a_nested_path_expands_just_enough():
    members, _ = flatten(make_struct(), "Gauge")
    assert paths(_watch_columns(members, ["Desc", "Status.W"], False)) == [
        "Desc",
        "Status.W",
    ]


def test_expand_all_shows_every_leaf_and_ignores_the_selection():
    members, _ = flatten(make_struct(), "Gauge")
    # No branch summary columns: with everything expanded they would only
    # repeat the children printed beside them, and a terminal cannot collapse.
    assert paths(_watch_columns(members, ["Desc"], True)) == [
        "Desc",
        "State",
        "Ilk.Trip",
        "Ilk.Msg",
        "Ilk.IMG.W",
        "Ilk.IMG.B",
        "Status.W",
        "Status.Code",
    ]


def test_unknown_member_selects_nothing():
    members, _ = flatten(make_struct(), "Gauge")
    assert _watch_columns(members, ["Nope"], False) == []


def test_show_struct_prints_nested_members_indented(capsys):
    show_struct(fake_eip(make_struct()), "Gauge")
    out = capsys.readouterr().out
    # The dotted paths printed are exactly what -m accepts.
    assert "  Ilk  ARRAY[1..3] OF ILK_Type" in out
    assert "    Ilk.Trip  INT" in out
    assert "      Ilk.IMG.B  ARRAY[0..15] OF BOOL" in out
    assert "    Status.W  ARRAY[1..3] OF WORD" in out


def test_summarise_struct_calls_out_non_primitive_members(capsys):
    summarise_struct("Gauge", make_struct())
    out = capsys.readouterr().out
    assert "Ilk: ARRAY[1..3] OF ILK_Type  (nested structure)" in out
    assert "Status: Status_Type  (nested structure)" in out
    # The flat members are still counted the way they always were.
    assert "2 array members of ARRAY[1..3]" in out
    assert "instances in use (Desc non-empty): 2 -> [1, 2]" in out


def test_probe_reports_short_chunks_and_names_the_failing_member(capsys):
    """--probe reads the tag the way aphyt does but records what each chunk
    returned, so a controller that hands back less than it declared is visible
    rather than surfacing as a struct.error from inside the library."""
    import asyncio
    import threading

    from dls_plc_tools.interrogate import probe_struct

    struct = make_struct()
    struct.size = 84  # what the member walk adds up to

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class FakeSession:
        """The async session behind the sync wrapper. Its last chunk comes back
        short, which is one of the two ways the decode ends up off the end."""

        MAXIMUM_LENGTH = 48  # so 40-byte chunks, as MAXIMUM_LENGTH - 8

        def __init__(self):
            self.loop = loop

        async def _simple_data_segment_read(self, obj, offset, read_size):
            returned = min(read_size, max(0, 60 - offset))
            # CIP common format: type code, additional info (the struct CRC),
            # then the payload.
            reply = b"\xa2\x02\x00\x00" + bytes(returned)
            return SimpleNamespace(reply_data=reply)

    variables = {"Temp": struct}
    eip = SimpleNamespace(
        _instance=FakeSession(),
        update_variable_dictionary=lambda: None,
        connected_cip_dispatcher=SimpleNamespace(variables=variables),
    )
    try:
        probe_struct(eip, "Temp")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()

    out = capsys.readouterr().out
    assert "asked   40, got   40" in out
    assert "asked   40, got   20  <-- short chunk" in out
    assert "total returned: 60 bytes" in out
    assert "short read: 24 bytes missing" in out
    # Desc and State fit in the 60 bytes that arrived. Ilk does not, but decodes
    # anyway off a partial slice -- silently wrong values, so it is called out
    # separately from Status, which runs out of bytes altogether and raises.
    assert "Desc                         ok      24 of 24 bytes" in out
    assert "Ilk                          SHORT   30 of 42 bytes" in out
    assert "Status                       FAILED  0 of 12 bytes" in out


def test_a_tag_aphyt_cannot_decode_is_audited_not_fatal(capsys, monkeypatch):
    """aphyt raises a bare ``struct.error`` from its own member walk, naming
    neither the tag nor the member. That used to abort the whole walk; now the
    remaining tags are still summarised and the bad one gets a layout audit."""
    from dls_plc_tools import interrogate

    good = make_struct()
    bad = make_struct()
    bad.size = 84  # what its members occupy; the read came back short
    bad.data = bytes(60)

    def read_or_raise(name):
        if name == "Temp":
            raise _struct.error("unpack requires a buffer of 2 bytes")
        return good

    variables = {"Gauge": good, "Temp": bad}

    class FakeSession:
        """Stands in for the aphyt session, which read_plc uses as a context
        manager (its __enter__ installs a SIGINT handler on the real class)."""

        update_variable_dictionary = staticmethod(lambda: None)

        def read_variable(self, name):
            return read_or_raise(name)

        connected_cip_dispatcher = SimpleNamespace(
            user_variables=variables, variables=variables, system_variables={}
        )

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(interrogate.omron, "NSeries", lambda ip: FakeSession())

    read_plc("192.168.0.100")
    out = capsys.readouterr().out
    assert "Temp: could not decode (error: unpack requires a buffer of 2 bytes)" in out
    assert "short read: 24 bytes missing" in out
    assert "first member past the end of the data: Ilk" in out
    # The walk carried on, so the tags after the bad one are still summarised.
    assert "Gauge: Gauge_Type" in out
