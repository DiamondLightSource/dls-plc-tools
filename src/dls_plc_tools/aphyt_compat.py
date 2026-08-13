"""Corrections applied to aphyt at import time.

aphyt sizes BOOL arrays wrongly, which mis-slices every member that follows one
inside a structure. ``CIPArray._get_size`` computes ``n // 8`` plus 2 bytes when
``n % 16``, where Omron packs a BOOL array into whole 16-bit words:
``ceil(n / 16) * 2``. The two agree only for 1..7, 16..23, 32..39 elements, and
DLS declarations sit outside that range regularly.

``DLS\\Temperature_External_Structure`` is the case that forced this. Its
``Status`` member is ``ARRAY[0..28] OF BOOL`` — 29 bits, 4 bytes on the
controller, sized 5 by aphyt. The odd size then trips aphyt's own
"make the offset even on a member type change" rule, so the two members after it
are sliced 2 bytes late and the last one runs past the end of a read that was
complete: 3484 bytes declared, 3484 received, 3486 consumed. That surfaces as
``struct.error: unpack requires a buffer of 2 bytes`` from inside the library.
With the size corrected the member walk ends on 3484 exactly.

The correction is monkeypatched rather than worked around at the call site
because ``_get_size`` runs inside ``from_items``/``from_instance`` while aphyt
builds the tag dictionary — there is no seam between "aphyt decided the size"
and "aphyt used it". It also fixes the paged read length of a *top-level* BOOL
array tag, which would otherwise ask the controller for more bytes than the tag
holds. Applying it is idempotent, and importing this module is enough.
"""

from aphyt.cip.cip_datatypes import CIPArray

_BOOL = b"\xc1"


def _get_size(self) -> int:
    """``CIPArray._get_size`` with Omron's BOOL packing.

    Non-BOOL arrays keep aphyt's own arithmetic (elements x element size).
    """
    elements = 1
    for number in self.number_of_elements:
        elements *= number
    if self.array_data_type == _BOOL:
        # Whole 16-bit words, so the size is always even -- which also keeps
        # the next member off aphyt's odd-offset padding path.
        return ((elements + 15) // 16) * 2
    return elements * self.array_data_type_size


def apply():
    """Install the corrected size calculation. Safe to call more than once."""
    CIPArray._get_size = _get_size  # noqa: SLF001


apply()
