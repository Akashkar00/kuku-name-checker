"""Unit tests on the synthetic demo show (samples/demo_show), whose planted
errors are known: Ravenscraft (x2, dialogue + logline), Victor Hail (dialogue),
and speaker tags MAYA vs MAYA THORNE. Mara is a real, different character
whose name looks like Maya's: the rules flag it, a human/LLM must dismiss it.

Run: python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from name_checker import _cell, analyse, read_lines, similar  # noqa: E402

DEMO = ROOT / "samples" / "demo_show"


class TestDemoShow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = analyse(DEMO)
        cls.variants = {v["spelling"]: (c["canonical"], v)
                        for c in cls.r["clusters"] for v in c["variants"]}

    def test_planted_place_typo_flagged_high(self):
        canonical, v = self.variants["Ravenscraft"]
        self.assertEqual(canonical, "Ravenscroft")
        self.assertEqual(v["severity"], "HIGH")  # it is spoken in ep 2
        self.assertEqual(v["count"], 2)

    def test_planted_surname_typo_flagged(self):
        canonical, v = self.variants["Hail"]
        self.assertEqual(canonical, "Hale")
        self.assertEqual(v["occurrences"][0]["episode"], 2)

    def test_speaker_tag_alias(self):
        pairs = {(a["short"], a["long"]) for a in self.r["tag_aliases"]}
        self.assertIn(("MAYA", "MAYA THORNE"), pairs)

    def test_lookalike_different_character_is_surfaced_for_review(self):
        # Mara vs Maya: rules cannot know they are sisters; the cluster must
        # exist so the LLM/human can dismiss it (precision is their job).
        names = {c["canonical"] for c in self.r["clusters"]} | set(self.variants)
        self.assertTrue({"Mara", "Maya"} <= names)

    def test_common_words_are_not_names(self):
        flagged = set(self.variants) | {c["canonical"] for c in self.r["clusters"]}
        self.assertNotIn("Close", flagged)
        self.assertNotIn("Then", flagged)


class TestParsing(unittest.TestCase):
    def test_line_kinds(self):
        lines = read_lines(DEMO / "episode-002-demo.txt")
        kinds = {line.kind for line in lines}
        self.assertEqual(kinds, {"notes", "description", "speaker_tag", "dialogue"})
        hail = next(line for line in lines if "Hail" in line.text)
        self.assertEqual((hail.kind, hail.speaker, hail.episode), ("dialogue", "MAYA", 2))

    def test_similarity_rules(self):
        self.assertTrue(similar("Ravenscroft", "Ravenscraft", 0.7))
        self.assertTrue(similar("Thorne", "Thornes", 0.7))      # plural = same stem
        self.assertFalse(similar("Victoria", "Vivienne", 0.7))
        self.assertFalse(similar("Hale", "Male", 0.7))           # first letter must match

    def test_csv_cells_cannot_become_formulas(self):
        self.assertEqual(_cell('=HYPERLINK("http://x")'), '\'=HYPERLINK("http://x")')
        self.assertEqual(_cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(_cell('"Ravenscraft first."'), '"Ravenscraft first."')


if __name__ == "__main__":
    unittest.main()
