"""Unit tests for harness/catalog.py.

Test files may contain project literals -- they are not the engine. The engine
itself is checked for literals by TestR1EngineHasNoProjectData below.

Most semantics are exercised against small synthetic contracts written to a
temp directory, so that a change to the shipped catalog.yml cannot turn a
semantic test green or red for the wrong reason. A handful of tests do load the
real contract, on purpose: the engine has to work on the file the product ships.
"""

import io
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import catalog as catmod  # noqa: E402
from harness import project as project_paths  # noqa: E402

# The PROJECT under test, resolved the way the engine resolves it, so a test
# and the code it exercises can never disagree about which project they mean.
# PROJECT-V2 §8.1: project data lives in projects/<name>/, not at the root.
PROJECT_ROOT = project_paths.project_root()
from harness.catalog import CatalogError  # noqa: E402

REAL_CATALOG = PROJECT_ROOT / "catalog.yml"
ENGINE_SOURCE = REPO_ROOT / "harness" / "catalog.py"


# ---------------------------------------------------------------------------
# Synthetic contracts
# ---------------------------------------------------------------------------

FIXTURE = """
messages:
  - id: 0x123
    name: frame_a
    dlc: 8
    sender: node_a
    signals:
      - { name: wide_le,         start_bit: 0,  length: 24 }
      - { name: signed_field,    start_bit: 24, length: 16, signed: true }
      - { name: mode_code,       start_bit: 40, length: 8 }
      - { name: rolling_counter, start_bit: 48, length: 8 }
  - id: 0x124
    name: frame_b
    dlc: 2
    sender: node_b
    signals:
      - { name: lamp_a,   start_bit: 0, length: 1 }
      - { name: lamp_b,   start_bit: 1, length: 1 }
      - { name: nibble,   start_bit: 4, length: 4 }
      - { name: byte_two, start_bit: 8, length: 8 }
  - id: 0x125
    name: frame_c
    dlc: 3
    sender: node_a
    signals:
      - { name: straddler,   start_bit: 4,  length: 12 }
      - { name: signed_byte, start_bit: 16, length: 8, signed: true }

enums:
  mode_code:
    0: OFF
    1: ON
    2: STANDBY
  lamp_a:
    0: DARK
    1: LIT
"""


def write_contract(text, name="contract.yml"):
    """Write a contract into a fresh temp dir and return its path."""
    tmp = Path(tempfile.mkdtemp(prefix="bench-catalog-"))
    path = tmp / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def load_quiet(text):
    """Load a synthetic contract, returning (catalog, warning_text)."""
    buf = io.StringIO()
    cat = catmod.load(write_contract(text), warn_stream=buf)
    return cat, buf.getvalue()


class FixtureCase(unittest.TestCase):
    """Base class giving every test the synthetic contract, warnings captured."""

    @classmethod
    def setUpClass(cls):
        cls.cat, cls.warnings = load_quiet(FIXTURE)


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


class TestLoading(FixtureCase):
    def test_fixture_loads_clean(self):
        self.assertEqual(self.warnings, "")
        self.assertEqual(len(self.cat.messages()), 3)

    def test_missing_file_refuses(self):
        with self.assertRaises(CatalogError) as ctx:
            catmod.load(Path(tempfile.gettempdir()) / "no-such-contract-42.yml")
        self.assertIn("not found", str(ctx.exception))

    def test_overlapping_signals_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 2
                    signals:
                      - { name: a, start_bit: 0, length: 8 }
                      - { name: b, start_bit: 4, length: 8 }
                """
            )
        self.assertIn("overlap", str(ctx.exception))

    def test_signal_past_end_of_payload_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 2
                    signals:
                      - { name: a, start_bit: 8, length: 16 }
                """
            )
        self.assertIn("past the end", str(ctx.exception))

    def test_duplicate_message_id_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 1
                    signals: [{ name: a, start_bit: 0, length: 8 }]
                  - id: 0x1
                    name: n
                    dlc: 1
                    signals: [{ name: b, start_bit: 0, length: 8 }]
                """
            )
        self.assertIn("0x1", str(ctx.exception))

    def test_duplicate_signal_in_message_refused(self):
        with self.assertRaises(CatalogError):
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 2
                    signals:
                      - { name: a, start_bit: 0, length: 8 }
                      - { name: a, start_bit: 8, length: 8 }
                """
            )

    def test_duplicate_symbolic_name_in_enum_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 1
                    signals: [{ name: a, start_bit: 0, length: 8 }]
                enums:
                  a: { 0: SAME, 1: SAME }
                """
            )
        self.assertIn("ambiguous", str(ctx.exception))

    def test_reversed_enum_row_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 1
                    signals: [{ name: a, start_bit: 0, length: 8 }]
                enums:
                  a: { NONE: 0 }
                """
            )
        self.assertIn("malformed", str(ctx.exception))

    def test_yaml_1_1_boolean_spellings_stay_symbolic(self):
        """ON/OFF/YES/NO are enum names, not booleans (a silent-corruption trap)."""
        cat, _ = load_quiet(
            """
            messages:
              - id: 0x1
                name: m
                dlc: 1
                signals: [{ name: a, start_bit: 0, length: 8 }]
            enums:
              a: { 0: OFF, 1: ON, 2: NO, 3: YES, 4: N, 5: Y }
            """
        )
        for raw, symbol in ((0, "OFF"), (1, "ON"), (2, "NO"), (3, "YES")):
            self.assertEqual(cat.resolve_enum("a", symbol), raw)
            self.assertEqual(cat.decode(0x1, bytes([raw]))["a"], symbol)

    def test_signed_flag_still_parses_as_boolean(self):
        cat, _ = load_quiet(
            """
            messages:
              - id: 0x1
                name: m
                dlc: 2
                signals:
                  - { name: a, start_bit: 0, length: 8, signed: true }
                  - { name: b, start_bit: 8, length: 8, signed: false }
            """
        )
        self.assertTrue(cat.message(0x1).signal("a").signed)
        self.assertFalse(cat.message(0x1).signal("b").signed)


# ---------------------------------------------------------------------------
# R2 -- enum resolution by signal name only
# ---------------------------------------------------------------------------


ORPHAN_CONTRACT = """
messages:
  - id: 0x1
    name: m
    dlc: 1
    signals:
      - { name: fault_code, start_bit: 0, length: 8 }
enums:
  fault_codes:
    0: NONE
    1: OVERTEMP
"""


class TestR2EnumBySignalName(FixtureCase):
    def test_resolve_uses_signal_name_as_table_key(self):
        self.assertEqual(self.cat.resolve_enum("mode_code", "STANDBY"), 2)
        self.assertEqual(self.cat.resolve_enum("lamp_a", "LIT"), 1)

    def test_ints_pass_through(self):
        self.assertEqual(self.cat.resolve_enum("mode_code", 7), 7)
        self.assertEqual(self.cat.resolve_enum("wide_le", 12345), 12345)

    def test_unknown_symbolic_raises_clear_error(self):
        with self.assertRaises(CatalogError) as ctx:
            self.cat.resolve_enum("mode_code", "SPINNING")
        msg = str(ctx.exception)
        self.assertIn("SPINNING", msg)
        self.assertIn("STANDBY", msg)  # lists what IS valid

    def test_case_sensitive_with_a_hint(self):
        with self.assertRaises(CatalogError) as ctx:
            self.cat.resolve_enum("mode_code", "standby")
        self.assertIn("case-sensitive", str(ctx.exception))

    def test_symbolic_for_signal_without_table_raises(self):
        with self.assertRaises(CatalogError) as ctx:
            self.cat.resolve_enum("wide_le", "SOMETHING")
        self.assertIn("no enum table", str(ctx.exception))

    def test_differently_keyed_table_does_not_resolve(self):
        """The whole point of R2: near-miss table names are not consulted."""
        cat, _ = load_quiet(ORPHAN_CONTRACT)
        self.assertIsNone(cat.enum_for("fault_code"))
        with self.assertRaises(CatalogError):
            cat.resolve_enum("fault_code", "OVERTEMP")
        # ...and the signal stays numeric on decode instead of failing loudly.
        self.assertEqual(cat.decode(0x1, b"\x01")["fault_code"], 1)

    def test_orphan_table_warns_loudly_on_stderr(self):
        buf = io.StringIO()
        catmod.load(write_contract(ORPHAN_CONTRACT), warn_stream=buf)
        out = buf.getvalue()
        self.assertIn("ORPHAN ENUM TABLE", out)
        self.assertIn("fault_codes", out)
        self.assertIn("BY SIGNAL NAME ONLY", out)
        self.assertIn("=" * 40, out)  # banner, impossible to miss in a log
        self.assertGreaterEqual(len(out.splitlines()), 8)

    def test_orphan_warning_defaults_to_stderr(self):
        buf = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = buf
        try:
            catmod.load(write_contract(ORPHAN_CONTRACT))
        finally:
            sys.stderr = real_stderr
        self.assertIn("ORPHAN ENUM TABLE", buf.getvalue())

    def test_every_orphan_gets_its_own_warning(self):
        cat, warnings = load_quiet(
            """
            messages:
              - id: 0x1
                name: m
                dlc: 1
                signals: [{ name: a, start_bit: 0, length: 8 }]
            enums:
              a:   { 0: X }
              b:   { 0: Y }
              c:   { 0: Z }
            """
        )
        self.assertEqual(set(cat.orphan_enums()), {"b", "c"})
        self.assertEqual(warnings.count("ORPHAN ENUM TABLE"), 2)


# ---------------------------------------------------------------------------
# R3 -- masks
# ---------------------------------------------------------------------------


class TestR3Masks(FixtureCase):
    def test_mask_marks_only_the_signals_passed(self):
        value, mask = self.cat.encode(0x123, {"mode_code": "STANDBY"})
        self.assertEqual(len(value), 8)
        self.assertEqual(len(mask), 8)
        self.assertEqual(mask, bytes([0, 0, 0, 0, 0, 0xFF, 0, 0]))
        self.assertEqual(value, bytes([0, 0, 0, 0, 0, 0x02, 0, 0]))

    def test_unspecified_bits_are_zero_in_both(self):
        value, mask = self.cat.encode(0x123, {"rolling_counter": 0xAB})
        self.assertEqual(mask, bytes([0, 0, 0, 0, 0, 0, 0xFF, 0]))
        self.assertEqual(value, bytes([0, 0, 0, 0, 0, 0, 0xAB, 0]))
        for i, (v, m) in enumerate(zip(value, mask)):
            if m == 0:
                self.assertEqual(v, 0, f"byte {i} outside the mask must be zero")

    def test_empty_selection_yields_all_zero_value_and_mask(self):
        value, mask = self.cat.encode(0x123, {})
        self.assertEqual(value, bytes(8))
        self.assertEqual(mask, bytes(8))

    def test_sub_byte_mask_is_bit_exact(self):
        value, mask = self.cat.encode(0x124, {"lamp_b": 1})
        self.assertEqual(mask, bytes([0x02, 0x00]))
        self.assertEqual(value, bytes([0x02, 0x00]))

        value, mask = self.cat.encode(0x124, {"nibble": 0xD})
        self.assertEqual(mask, bytes([0xF0, 0x00]))
        self.assertEqual(value, bytes([0xD0, 0x00]))

    def test_mask_makes_assertions_immune_to_a_rolling_counter(self):
        """The reason masks exist: the counter changes every transmission."""
        expect, mask = self.cat.encode(0x123, {"mode_code": "ON", "wide_le": 0x010203})
        for counter in (0x00, 0x5A, 0xFF):
            frame, _ = self.cat.encode(
                0x123,
                {"mode_code": "ON", "wide_le": 0x010203, "rolling_counter": counter},
            )
            masked_frame = bytes(f & m for f, m in zip(frame, mask))
            masked_expect = bytes(e & m for e, m in zip(expect, mask))
            self.assertEqual(masked_frame, masked_expect)
            if counter:  # unmasked comparison would have been wrong
                self.assertNotEqual(frame, expect)

    def test_mask_of_two_signals_is_the_union(self):
        _, mask = self.cat.encode(0x124, {"lamp_a": 1, "byte_two": 0})
        self.assertEqual(mask, bytes([0x01, 0xFF]))


# ---------------------------------------------------------------------------
# Bit layout
# ---------------------------------------------------------------------------


class TestBitLayout(FixtureCase):
    def test_little_endian_multi_byte_signal(self):
        value, mask = self.cat.encode(0x123, {"wide_le": 0xABCDEF})
        self.assertEqual(value[:3], bytes([0xEF, 0xCD, 0xAB]))
        self.assertEqual(mask[:3], bytes([0xFF, 0xFF, 0xFF]))
        self.assertEqual(value[3:], bytes(5))

    def test_signal_straddling_byte_boundaries(self):
        # 12 bits starting at bit 4: nibble of byte 0 plus all of byte 1.
        value, mask = self.cat.encode(0x125, {"straddler": 0xABC})
        self.assertEqual(value, bytes([0xC0, 0xAB, 0x00]))
        self.assertEqual(mask, bytes([0xF0, 0xFF, 0x00]))
        self.assertEqual(self.cat.decode(0x125, value)["straddler"], 0xABC)

    def test_bit_zero_is_lsb_of_byte_zero(self):
        value, _ = self.cat.encode(0x124, {"lamp_a": 1})
        self.assertEqual(value[0] & 0x01, 0x01)

    def test_decode_is_the_inverse_of_encode(self):
        payload = {
            "wide_le": 0x123456,
            "signed_field": -2,
            "mode_code": "STANDBY",
            "rolling_counter": 0xFE,
        }
        value, _ = self.cat.encode(0x123, payload)
        self.assertEqual(self.cat.decode(0x123, value), payload)


# ---------------------------------------------------------------------------
# Signedness
# ---------------------------------------------------------------------------


class TestSignedSignals(FixtureCase):
    def test_negative_round_trip(self):
        for probe in (-1, -2, -1000, -32768, 0, 1, 32767):
            value, _ = self.cat.encode(0x123, {"signed_field": probe})
            self.assertEqual(self.cat.decode(0x123, value)["signed_field"], probe)

    def test_minus_one_is_all_ones_on_the_wire(self):
        value, mask = self.cat.encode(0x123, {"signed_field": -1})
        self.assertEqual(value[3:5], b"\xff\xff")
        self.assertEqual(mask[3:5], b"\xff\xff")

    def test_decode_sign_extends(self):
        payload = bytearray(8)
        payload[3:5] = b"\x00\x80"  # 0x8000
        self.assertEqual(self.cat.decode(0x123, bytes(payload))["signed_field"], -32768)
        payload[3:5] = b"\xff\x7f"  # 0x7fff
        self.assertEqual(self.cat.decode(0x123, bytes(payload))["signed_field"], 32767)

    def test_declared_range_is_enforced(self):
        for bad in (32768, -32769, 70000):
            with self.assertRaises(CatalogError, msg=f"{bad} should not fit"):
                self.cat.encode(0x123, {"signed_field": bad})

    def test_signed_byte_sign_extends(self):
        value, _ = self.cat.encode(0x125, {"signed_byte": -3})
        self.assertEqual(value[2], 0xFD)
        self.assertEqual(self.cat.decode(0x125, value)["signed_byte"], -3)

    def test_unsigned_signal_refuses_negative_input(self):
        """A field the contract does not mark signed has range 0..max.

        This previously stored the two's-complement bits and decoded back as a
        large positive, so encode/decode did not round-trip and an assertion
        could pass against a value nobody wrote. The refusal must be symmetric
        with the positive-overflow refusal below.
        """
        with self.assertRaises(CatalogError) as ctx:
            self.cat.encode(0x123, {"wide_le": -1})
        # The message has to tell the author how to express what they meant.
        self.assertIn("signed: true", str(ctx.exception))

    def test_unsigned_out_of_range_refused_in_both_directions(self):
        for bad in (1 << 24, -1, -(1 << 23), -(1 << 23) - 1):
            with self.subTest(value=bad), self.assertRaises(CatalogError):
                self.cat.encode(0x123, {"wide_le": bad})

    def test_signed_flag_needs_room_for_a_magnitude(self):
        with self.assertRaises(CatalogError):
            load_quiet(
                """
                messages:
                  - id: 0x1
                    name: m
                    dlc: 1
                    signals: [{ name: a, start_bit: 0, length: 1, signed: true }]
                """
            )


# ---------------------------------------------------------------------------
# Encode / decode surface
# ---------------------------------------------------------------------------


class TestEncodeDecodeSurface(FixtureCase):
    def test_symbolic_and_raw_may_be_mixed(self):
        value, mask = self.cat.encode(
            0x123, {"mode_code": "ON", "rolling_counter": 3}
        )
        self.assertEqual(value[5], 1)
        self.assertEqual(value[6], 3)
        self.assertEqual(mask, bytes([0, 0, 0, 0, 0, 0xFF, 0xFF, 0]))

    def test_unknown_signal_refused_and_lists_what_exists(self):
        with self.assertRaises(CatalogError) as ctx:
            self.cat.encode(0x123, {"not_a_signal": 1})
        msg = str(ctx.exception)
        self.assertIn("not_a_signal", msg)
        self.assertIn("mode_code", msg)

    def test_signal_of_another_message_refused(self):
        with self.assertRaises(CatalogError):
            self.cat.encode(0x123, {"lamp_a": 1})

    def test_unknown_message_refused(self):
        with self.assertRaises(CatalogError):
            self.cat.encode(0x7FF, {"mode_code": 1})
        with self.assertRaises(CatalogError):
            self.cat.decode(0x7FF, bytes(8))

    def test_unknown_symbolic_on_encode_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            self.cat.encode(0x123, {"mode_code": "NOPE"})
        self.assertIn("NOPE", str(ctx.exception))

    def test_decode_returns_symbolic_for_enum_signals_only(self):
        value, _ = self.cat.encode(0x123, {"mode_code": 2, "wide_le": 5})
        decoded = self.cat.decode(0x123, value)
        self.assertEqual(decoded["mode_code"], "STANDBY")
        self.assertEqual(decoded["wide_le"], 5)
        self.assertIsInstance(decoded["rolling_counter"], int)

    def test_decode_of_undefined_enum_value_stays_numeric(self):
        value, _ = self.cat.encode(0x123, {"mode_code": 99})
        self.assertEqual(self.cat.decode(0x123, value)["mode_code"], 99)

    def test_decode_raw_is_always_numeric(self):
        value, _ = self.cat.encode(0x123, {"mode_code": "STANDBY", "signed_field": -1})
        raw = self.cat.decode_raw(0x123, value)
        self.assertEqual(raw["mode_code"], 2)
        self.assertEqual(raw["signed_field"], -1)  # sign-extended, still an int

    def test_decode_covers_every_signal_of_the_message(self):
        decoded = self.cat.decode(0x123, bytes(8))
        self.assertEqual(
            set(decoded), {s.name for s in self.cat.signals_of(0x123)}
        )

    def test_short_payload_refused(self):
        with self.assertRaises(CatalogError) as ctx:
            self.cat.decode(0x123, bytes(7))
        self.assertIn("at least", str(ctx.exception))

    def test_longer_payload_refused(self):
        """An over-long frame is a mis-sized or mis-routed message, not padding.

        Truncating it silently decodes plausible values and the caller never
        learns the frame was wrong. A short payload was always refused; this
        makes the long case symmetric.
        """
        padded = bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0xFF, 0xFF])
        with self.assertRaises(CatalogError) as ctx:
            self.cat.decode(0x123, padded)
        self.assertIn("10 bytes", str(ctx.exception))

    def test_bytearray_accepted(self):
        self.assertEqual(self.cat.decode(0x124, bytearray(b"\x01\x00"))["lamp_a"], "LIT")

    def test_non_bytes_payload_refused(self):
        with self.assertRaises(CatalogError):
            self.cat.decode(0x124, "0102")

    def test_values_must_be_a_mapping(self):
        with self.assertRaises(CatalogError):
            self.cat.encode(0x123, [("mode_code", 1)])

    def test_boolean_value_refused(self):
        """A bool must not become 0/1.

        _as_int() already refuses booleans and the loader exists to stop YAML
        1.1 turning bare words like `on` and `no` into them. Coercing one here
        would reintroduce exactly that silent 0/1 through the other door.
        """
        for bad in (True, False):
            with self.subTest(value=bad), self.assertRaises(CatalogError):
                self.cat.encode(0x123, {"mode_code": bad})

    def test_float_value_refused(self):
        with self.assertRaises(CatalogError):
            self.cat.encode(0x123, {"rolling_counter": 1.5})


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


class TestIntrospection(FixtureCase):
    def test_message_by_id_and_by_name(self):
        self.assertIs(self.cat.message(0x123), self.cat.message("frame_a"))
        self.assertEqual(self.cat.message(0x123).name, "frame_a")

    def test_message_by_hex_string(self):
        self.assertIs(self.cat.message("0x123"), self.cat.message(0x123))

    def test_messages_preserves_file_order(self):
        self.assertEqual(
            [m.name for m in self.cat.messages()], ["frame_a", "frame_b", "frame_c"]
        )

    def test_signals_of_preserves_file_order(self):
        self.assertEqual(
            [s.name for s in self.cat.signals_of("frame_b")],
            ["lamp_a", "lamp_b", "nibble", "byte_two"],
        )

    def test_message_fields(self):
        message = self.cat.message(0x124)
        self.assertEqual(message.dlc, 2)
        self.assertEqual(message.sender, "node_b")
        self.assertEqual(message.id, 0x124)

    def test_signal_geometry(self):
        sig = self.cat.message(0x125).signal("straddler")
        self.assertEqual((sig.start_bit, sig.length, sig.end_bit), (4, 12, 15))
        self.assertEqual(sig.bit_mask, 0xFFF0)
        self.assertEqual((sig.min_value, sig.max_value), (0, 4095))

    def test_unknown_message_name_refused(self):
        with self.assertRaises(CatalogError):
            self.cat.message("frame_z")


# ---------------------------------------------------------------------------
# The real shipped contract
# ---------------------------------------------------------------------------


class TestRealCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.buf = io.StringIO()
        cls.cat = catmod.load(REAL_CATALOG, warn_stream=cls.buf)

    def test_default_path_is_the_shipped_contract(self):
        buf = io.StringIO()
        cat = catmod.load(warn_stream=buf)
        self.assertEqual(cat.source, REAL_CATALOG)
        self.assertEqual(len(cat.messages()), len(self.cat.messages()))

    def test_shipped_contract_has_no_orphan_enum_tables(self):
        self.assertEqual(self.cat.orphan_enums(), ())
        self.assertEqual(self.buf.getvalue(), "")

    def test_shipped_contract_documents_the_enum_rule(self):
        """R2 requires catalog.yml itself to state the rule."""
        text = REAL_CATALOG.read_text(encoding="utf-8")
        self.assertIn("BY SIGNAL NAME", text.upper())

    def test_every_enum_table_is_reachable_from_some_signal(self):
        signal_names = set(self.cat.signal_names())
        for table in self.cat.enum_tables():
            self.assertIn(table, signal_names)

    def test_encode_decode_round_trip_on_a_real_message(self):
        value, mask = self.cat.encode(0x604, {"fault_code": "OVERTEMP"})
        self.assertEqual(len(value), 8)
        self.assertEqual(mask, bytes([0xFF] + [0] * 7))
        self.assertEqual(value, bytes([0x01] + [0] * 7))
        self.assertEqual(self.cat.decode(0x604, value)["fault_code"], "OVERTEMP")

    def test_rolling_counter_is_excluded_from_the_mask(self):
        value, mask = self.cat.encode(0x604, {"fault_code": "OVERTEMP"})
        counter = self.cat.message(0x604).signal("fault_counter")
        self.assertEqual(mask[counter.start_bit // 8], 0x00)
        self.assertEqual(value[counter.start_bit // 8], 0x00)

    def test_twenty_four_bit_signal_is_little_endian(self):
        value, _ = self.cat.encode(0x600, {"pack_voltage_mv": 72000})
        self.assertEqual(value[:3], (72000).to_bytes(3, "little"))
        self.assertEqual(self.cat.decode(0x600, value)["pack_voltage_mv"], 72000)

    def test_symbolic_names_from_the_shipped_tables_resolve(self):
        self.assertEqual(self.cat.resolve_enum("contactor_state", "WELDED"), 3)
        self.assertEqual(self.cat.resolve_enum("drive_state", "LIMP"), 4)
        self.assertEqual(self.cat.resolve_enum("inverter_state", "OFF"), 0)

    def test_sub_byte_telltale_block(self):
        value, mask = self.cat.encode(0x500, {"telltale_overtemp": 1})
        self.assertEqual(value, bytes([0x04, 0, 0, 0]))
        self.assertEqual(mask, bytes([0x04, 0, 0, 0]))


# ---------------------------------------------------------------------------
# R1 -- the engine holds no project data
# ---------------------------------------------------------------------------


class TestR1EngineHasNoProjectData(unittest.TestCase):
    """harness/catalog.py must survive replacing catalog.yml wholesale."""

    @classmethod
    def setUpClass(cls):
        cls.source = ENGINE_SOURCE.read_text(encoding="utf-8")
        # Read the contract through the engine, not through stock safe_load:
        # YAML 1.1 would turn the symbolic name OFF into the boolean False and
        # this test would then hunt the engine for the string "False".
        cls.cat = catmod.load(REAL_CATALOG, warn_stream=io.StringIO())

    def test_no_message_or_signal_or_node_names_in_the_engine(self):
        names = set()
        for message in self.cat.messages():
            names.add(message.name)
            names.add(message.sender)
            for sig in message.signals:
                names.add(sig.name)
        lowered = self.source.lower()
        offenders = sorted(n for n in names if n.lower() in lowered)
        self.assertEqual(offenders, [], f"project names leaked into the engine: {offenders}")

    def test_no_message_ids_in_the_engine(self):
        ids = [m.id for m in self.cat.messages()]
        lowered = self.source.lower()
        hex_hits = [hex(i) for i in ids if hex(i) in lowered]
        dec_hits = [i for i in ids if re.search(r"\b%d\b" % i, self.source)]
        self.assertEqual(hex_hits, [])
        self.assertEqual(dec_hits, [])

    def test_no_enum_spellings_in_the_engine(self):
        symbols = set()
        for key in self.cat.enum_tables():
            symbols.update(self.cat.enum_for(key).names())
        offenders = sorted(
            s for s in symbols if re.search(r"\b%s\b" % re.escape(s), self.source)
        )
        self.assertEqual(offenders, [])

    def test_engine_works_on_a_completely_different_contract(self):
        """Onboarding a new customer must not require touching harness/."""
        cat, warnings = load_quiet(
            """
            messages:
              - id: 0x7A0
                name: some_other_customer_frame
                dlc: 3
                sender: some_other_node
                signals:
                  - { name: widget_state, start_bit: 0,  length: 4 }
                  - { name: widget_count, start_bit: 8,  length: 16 }
            enums:
              widget_state: { 0: IDLE_STATE, 1: BUSY_STATE }
            """
        )
        self.assertEqual(warnings, "")
        value, mask = cat.encode(0x7A0, {"widget_state": "BUSY_STATE"})
        self.assertEqual((value, mask), (b"\x01\x00\x00", b"\x0f\x00\x00"))
        self.assertEqual(cat.decode(0x7A0, value)["widget_state"], "BUSY_STATE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
