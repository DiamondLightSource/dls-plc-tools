"""CLI column selection and structure printing over nested members."""

from types import SimpleNamespace

from dls_plc_tools.interrogate import _watch_columns, show_struct, summarise_struct
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
