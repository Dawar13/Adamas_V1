"""The verb registry: what a manifest may say, and what it may not.

NN-3's sentence is the subject of this file: *if adding one more verb, pattern,
rule or chip requires editing source and shipping a build, the design is wrong.*
Guard 4 proves the positive half -- a verb arrives as a file. This proves the
part Guard 4 cannot see: that the manifests are the ONLY place the vocabulary
is written down, and that a bad manifest is refused rather than half-loaded.

The section 10.7 checklist runs against every shipped verb here, so a verb
added later is checked by the same rules without anyone remembering to.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent
REPO_ROOT = HARNESS.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness import run_scenarios as rs          # noqa: E402

#: REACHED THROUGH THE ENGINE, NOT IMPORTED AGAIN.
#:
#: The engine puts harness/ on sys.path and imports its siblings as top-level
#: modules; a test doing `from harness import verb_registry` gets a SECOND
#: module object, with a second VerbError class -- so `assertRaises` would not
#: catch the exception the engine raises, and the failure reads as the refusal
#: not happening rather than as two definitions of it. NN-5, in miniature.
verb_registry = rs.verb_registry
REGISTRY = rs.REGISTRY

#: The refusal messages the engine emitted BEFORE the registry existed,
#: captured by provoking each one through the pre-migration compiler. The
#: manifests must reproduce them exactly: a migration that improved the wording
#: on the way past would be indistinguishable from one that lost it.
CAPTURED = json.loads(
    (HERE / "data" / "verb-refusals.json").read_text(encoding="utf-8"))


class TestTheVocabularyIsTheManifests(unittest.TestCase):

    def test_every_shipped_manifest_loaded(self):
        files = sorted((HARNESS / "verbs").glob("*.yml"))
        self.assertEqual(len(files), len(REGISTRY))
        self.assertEqual(sorted(p.stem for p in files), sorted(REGISTRY.names))

    def test_the_engine_exposes_it_under_the_names_consumers_use(self):
        """VERBS, STEP_KEYS and the two polarity tuples are now views."""
        self.assertEqual(set(rs.VERBS), set(REGISTRY.names))
        self.assertEqual(rs.STEP_KEYS, REGISTRY.step_keys)
        self.assertEqual(set(rs.EXPECT_VERBS),
                         set(REGISTRY.of_polarity(verb_registry.POLARITY_EXPECT)))
        self.assertEqual(set(rs.FORBID_VERBS),
                         set(REGISTRY.of_polarity(verb_registry.POLARITY_FORBID)))

    def test_the_order_is_sorted_and_not_the_filesystem_s(self):
        """The vocabulary appears in error messages. Directory order would make
        two machines refuse the same scenario with two different texts."""
        self.assertEqual(list(REGISTRY.names), sorted(REGISTRY.names))

    def test_the_exit_codes_match_the_engine_s(self):
        """Mirrored constants, pinned. Misreading one turns a refusal into a
        usage error or the other way round."""
        self.assertEqual(verb_registry.EXIT_USAGE, rs.EXIT_USAGE)
        self.assertEqual(verb_registry.EXIT_REFUSED, rs.EXIT_REFUSED)


class TestTheChecklistInSection107(unittest.TestCase):
    """Run against every shipped verb, so a verb added later is checked too."""

    def test_1_a_verb_narrowed_to_one_node_kind_says_why(self):
        for name in REGISTRY.names:
            verb = REGISTRY[name]
            if set(verb.applies_to) != set(verb_registry.KINDS):
                with self.subTest(verb=name):
                    self.assertTrue(
                        verb.refusals,
                        "%s applies only to %s and declares no refusal, so it "
                        "would reject the other kind with no stated reason"
                        % (name, ", ".join(verb.applies_to)))

    def test_2_a_verb_says_what_it_puts_in_the_event_log(self):
        """Except the ones that put nothing there, which say that by omission.

        `run_for` advances time and writes nothing; a verb claiming an event
        kind it does not emit would send the judge looking for a line that
        never arrives.
        """
        emitting = [n for n in REGISTRY.names if REGISTRY[n].emits]
        self.assertGreater(len(emitting), len(REGISTRY) // 2)
        for name in emitting:
            with self.subTest(verb=name):
                self.assertRegex(REGISTRY[name].emits, r"^[A-Z][A-Z_]*$")

    def test_4_no_verb_names_anything_project_specific(self):
        """The manifests are engine files. The whole-engine purity guard now
        scans them too; this is the same rule stated where a verb author will
        read it."""
        for name in REGISTRY.names:
            with self.subTest(verb=name):
                self.assertNotIn("0x", REGISTRY[name].summary)

    def test_5_every_verb_declares_its_arguments(self):
        for name in REGISTRY.names:
            with self.subTest(verb=name):
                self.assertTrue(REGISTRY[name].args,
                                "%s declares no arguments, so nothing checks "
                                "what a step of it may carry" % name)

    def test_every_verb_documents_itself(self):
        """The docs page is generated from these. A verb with no doc is a verb
        whose page is blank."""
        for name in REGISTRY.names:
            with self.subTest(verb=name):
                self.assertGreater(len(REGISTRY[name].doc), 80, name)


class TestTheRefusalsAreTheOnesTheEngineUsedToEmit(unittest.TestCase):
    """Byte-for-byte, against messages captured from the pre-registry engine."""

    def test_every_captured_refusal_is_declared_in_a_manifest(self):
        for verb, conditions in sorted(CAPTURED.items()):
            for condition in sorted(conditions):
                with self.subTest(verb=verb, condition=condition):
                    self.assertIn(condition, REGISTRY[verb].refusals)

    def test_no_manifest_invented_a_refusal_that_was_never_emitted(self):
        """The other direction. A refusal nothing raises is a message that has
        never been seen by anyone, and the first time it appears will be the
        first time it is read."""
        for name in REGISTRY.names:
            for condition in sorted(REGISTRY[name].refusals):
                with self.subTest(verb=name, condition=condition):
                    self.assertIn(condition, CAPTURED.get(name, {}))

    #: The thirteen verbs that EXISTED before the registry. Only these can have
    #: survived a migration; the power-class verbs were born as manifests, and
    #: their refusals are evidenced by having been provoked through the real
    #: compiler rather than by comparing two eras.
    MIGRATED = (
        "can_send", "expect_can", "expect_no_can", "expect_symbol", "flood",
        "mark", "node_freeze", "node_resume", "node_signal", "node_silence",
        "run_for", "wait_uart", "write_symbol",
    )

    def test_the_wording_survived_the_migration(self):
        prefix = "test-scenario.yml: step 1: "
        values = {
            "node": "motor", "verb": "node_freeze", "count": 0,
            "signal": "not_a_bound_signal", "bound": "drive_state",
            "message_id": 0x200, "board": "bms_ecu",
            "boards_file": "test-boards.yml",
        }
        for verb, conditions in sorted(CAPTURED.items()):
            if verb not in self.MIGRATED:
                continue
            for condition, captured in sorted(conditions.items()):
                with self.subTest(verb=verb, condition=condition):
                    local = dict(values, verb=verb)
                    if verb == "node_signal" and condition == "no_signal_symbols":
                        local["node"] = "bms"
                    elif verb == "node_signal" and condition == "signal_not_bound":
                        local["node"] = "vcu"
                    rendered = REGISTRY[verb].refusal(condition).render(**local)
                    self.assertEqual(prefix + rendered, captured)

    def test_a_refusal_message_names_the_fix_where_one_exists(self):
        """Section 10.2's own emphasis: the message names the fix, not just the
        problem. Not every refusal has a fix to name -- "count must be
        positive" is complete -- but the long ones do, and this pins that they
        keep saying so."""
        for verb in ("node_freeze", "node_resume"):
            text = REGISTRY[verb].refusal("node_is_scripted").message
            self.assertIn("node_silence", text)
            self.assertIn("type: real", text)
        text = REGISTRY["node_signal"].refusal("no_signal_symbols").message
        self.assertIn("signal_symbols", text)

    def test_a_condition_no_manifest_declares_is_refused_loudly(self):
        """A handler naming a condition its manifest does not carry would
        otherwise raise an error with no text."""
        with self.assertRaises(verb_registry.VerbError) as caught:
            REGISTRY["mark"].refusal("no_such_condition")
        self.assertIn("no refusal named", str(caught.exception))

    def test_a_missing_placeholder_is_refused_not_half_rendered(self):
        """A refusal printing `{node}` literally would be a defect surfacing at
        the moment an operator is already dealing with one."""
        refusal = REGISTRY["wait_uart"].refusal("node_is_scripted")
        with self.assertRaises(verb_registry.VerbError):
            refusal.render()


class TestTheHandlersNoLongerCarryTheWords(unittest.TestCase):
    """The grep. This is what makes the migration real rather than additive.

    A registry that declared refusals while the handlers went on raising their
    own hardcoded strings would be documentation, not mechanism: the manifest
    would say one thing, the engine another, and only the second would ever be
    seen.
    """

    def compiler_source(self):
        return (HARNESS / "run_scenarios.py").read_text(encoding="utf-8")

    def test_no_migrated_message_survives_in_the_engine_source(self):
        source = self.compiler_source()
        for verb, conditions in sorted(CAPTURED.items()):
            for condition in sorted(conditions):
                message = REGISTRY[verb].refusal(condition).message
                # The first clause is enough to find a copy and short enough to
                # be robust to the manifest being rewrapped.
                fragment = message.split("\n")[0].split("{")[0].strip()
                if len(fragment) < 12:
                    continue
                with self.subTest(verb=verb, condition=condition):
                    self.assertNotIn(
                        fragment, source,
                        "run_scenarios.py still spells the %s/%s refusal; the "
                        "manifest is supposed to be the only copy"
                        % (verb, condition))

    def test_every_declared_refusal_is_raised_through_the_registry(self):
        source = self.compiler_source()
        for verb, conditions in sorted(CAPTURED.items()):
            for condition in sorted(conditions):
                with self.subTest(verb=verb, condition=condition):
                    self.assertIn('"%s"' % condition, source,
                                  "nothing in the engine raises %s/%s"
                                  % (verb, condition))


class TestWhatALoaderRefuses(unittest.TestCase):
    """A bad manifest stops the whole vocabulary. Never a partial load."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verbs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = self.tmp / "verbs"
        shutil.copytree(HARNESS / "verbs", self.dir)
        self.addCleanup(verb_registry.forget)

    def write(self, name, text):
        (self.dir / ("%s.yml" % name)).write_text(text, encoding="utf-8",
                                                  newline="\n")

    def load(self):
        verb_registry.forget()
        return verb_registry.load(str(self.dir))

    GOOD = """
verb: probe
class: book
summary: a probe
args:
  text:
    type: text
    required: true
template: |
  bench_mark "{text}"
doc: |
  Long enough to satisfy the documentation check that every verb must carry,
  because a generated docs page with a blank entry is a stale docs page.
"""

    def test_the_fixture_loads_before_anything_is_broken(self):
        self.write("probe", self.GOOD)
        self.assertIn("probe", self.load())

    def test_an_unknown_class(self):
        self.write("probe", self.GOOD.replace("class: book", "class: sorcery"))
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("sorcery", str(c.exception))

    def test_an_assert_verb_with_no_polarity(self):
        """The judge cannot infer it, and inferring it wrongly turns a
        prohibition into a requirement."""
        self.write("probe", self.GOOD.replace("class: book", "class: assert"))
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("polarity", str(c.exception))

    def test_a_polarity_on_a_verb_that_arms_nothing(self):
        self.write("probe", self.GOOD.replace(
            "class: book", "class: book\npolarity: expect"))
        with self.assertRaises(verb_registry.VerbError):
            self.load()

    def test_an_argument_type_nothing_can_parse(self):
        self.write("probe", self.GOOD.replace("type: text", "type: colour"))
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("colour", str(c.exception))

    def test_both_a_handler_and_a_template(self):
        self.write("probe", self.GOOD.replace(
            "template: |", "handler: mark\ntemplate: |"))
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("exactly one", str(c.exception))

    def test_neither_a_handler_nor_a_template(self):
        self.write("probe", self.GOOD.replace(
            'template: |\n  bench_mark "{text}"\n', ""))
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("exactly one", str(c.exception))

    def test_a_refusal_exiting_something_that_is_not_a_refusal(self):
        self.write("probe", self.GOOD + """
refusals:
  - if: nope
    exit: 1
    message: this exits as though the firmware failed
""")
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("exit", str(c.exception))

    def test_two_manifests_defining_one_verb(self):
        """Choosing one by directory order would silently drop the other."""
        self.write("probe", self.GOOD)
        self.write("probe-again", self.GOOD)
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("both define", str(c.exception))

    def test_a_bare_arg_that_is_not_an_argument(self):
        self.write("probe", self.GOOD.replace(
            "template: |", "bare_arg: nowhere\ntemplate: |"))
        with self.assertRaises(verb_registry.VerbError):
            self.load()

    def test_an_unknown_manifest_key(self):
        self.write("probe", self.GOOD + "colour: blue\n")
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("colour", str(c.exception))

    def test_an_empty_registry_directory(self):
        for path in self.dir.glob("*.yml"):
            path.unlink()
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("refuses every scenario", str(c.exception))

    def test_a_narrowed_verb_with_no_refusal(self):
        self.write("probe", self.GOOD.replace(
            "template: |", "applies_to: [real]\ntemplate: |"))
        with self.assertRaises(verb_registry.VerbError) as c:
            self.load()
        self.assertIn("10.7", str(c.exception))


class TestTheGeneratedDocsPageIsNotStale(unittest.TestCase):
    """Section 10.3 promises a docs page that is generated and NEVER STALE.

    Generated is the easy half. A page regenerated only when someone remembers
    is stale by a different route, and a reference that disagrees with the
    engine is worse than none: it is the wording an operator will quote back.
    """

    def test_the_committed_page_matches_the_manifests(self):
        import subprocess
        done = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "verb-docs.py"), "--check"],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120)
        self.assertEqual(done.returncode, 0,
                         done.stdout.decode("utf-8", "replace"))

    def test_the_page_quotes_every_refusal_message(self):
        """The refusal words on the page are the words the engine emits, so an
        operator reading the reference is reading the same sentence."""
        page = (REPO_ROOT / "docs" / "VERBS.md").read_text(encoding="utf-8")
        for name in REGISTRY.names:
            for condition, refusal in sorted(REGISTRY[name].refusals.items()):
                with self.subTest(verb=name, condition=condition):
                    self.assertIn(refusal.message.split(chr(10))[0], page)

    def test_every_verb_has_a_section(self):
        page = (REPO_ROOT / "docs" / "VERBS.md").read_text(encoding="utf-8")
        for name in REGISTRY.names:
            with self.subTest(verb=name):
                self.assertIn("## %s" % name, page)


class TestHowManyVerbsAreAFileAndNothingElse(unittest.TestCase):
    """Reported, not aspired to.

    Section 10.1 budgets ~60% of the FULL 45-verb set for verbs needing no
    handler. The thirteen V1 built are not a sample of that set: they are the
    ones with masked matching, window arithmetic and token bookkeeping in them,
    which is why V1 built them first.

    So this asserts the honest number rather than a hoped-for one. If a later
    verb genuinely needs no logic, this test says so out loud when it lands.
    """

    def test_the_count_is_what_it_is(self):
        template_only = REGISTRY.template_only
        self.assertEqual(
            len(template_only), 0,
            "template-only verbs are now %s -- update this test and STATUS.md "
            "rather than leaving the count stale" % (template_only,))
        # Thirteen migrated from source, four power-class verbs added in 3.2,
        # two ordering verbs added in 3.3, and expect_latched in 3.3b.
        #
        # expect_latched was planned as the third ordering verb and REFUSED at
        # that gate: in the fixed-value form its negative control went red,
        # because expect_no_can catches the defect it was built for. It landed
        # in 3.3b in the VALUE-ANCHORED form, where the thing it adds is not
        # catching a defect at all -- an invariant naming the right value
        # catches that too, and was measured doing so -- but not FAILING
        # CORRECT FIRMWARE that resolves a contract-silent tie the other way.
        self.assertEqual(len(REGISTRY), 20)


if __name__ == "__main__":
    unittest.main()
