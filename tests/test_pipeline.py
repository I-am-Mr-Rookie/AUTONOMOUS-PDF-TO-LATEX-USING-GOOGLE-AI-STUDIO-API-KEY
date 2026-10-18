"""Offline tests for the pipeline modules (no API calls, no pdflatex/Poppler needed).

    python -m unittest discover tests
"""
import io
import json
import os
import sys
import tempfile
import tokenize
from datetime import datetime, timezone
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import compiler  # noqa: E402
import config  # noqa: E402
import evaluator  # noqa: E402
import extractor  # noqa: E402
import gemini  # noqa: E402
import main  # noqa: E402
import processor  # noqa: E402
import state  # noqa: E402


class TempCache(unittest.TestCase):
    """Point every module's cache/output/input paths at a fresh temp dir."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for d in ("input", "output", "cache/images"):
            (self.root / d).mkdir(parents=True)
        for target, value in [
            ("state.CACHE_DIR", self.root / "cache"),
            ("processor.IMAGES_DIR", self.root / "cache/images"),
            ("compiler.OUTPUT_DIR", self.root / "output"),
            ("evaluator.OUTPUT_DIR", self.root / "output"),
            ("gemini.USAGE_LEDGER", self.root / "cache/api_usage.json"),
            ("config.INPUT_DIR", self.root / "input"),
            ("config.OUTPUT_DIR", self.root / "output"),
            ("config.CACHE_DIR", self.root / "cache"),
            ("config.IMAGES_DIR", self.root / "cache/images"),
        ]:
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


# --- Python version compatibility ------------------------------------------------
class PythonCompatTest(unittest.TestCase):
    def test_no_pep701_fstring_quote_reuse(self):
        """README promises Python 3.10+. Reusing the enclosing quote inside an f-string
        is only legal from 3.12 (PEP 701) and is a SyntaxError on 3.10/3.11."""
        if not hasattr(tokenize, "FSTRING_START"):
            self.skipTest("needs the 3.12+ tokenizer")
        offenders = []
        for path in [*ROOT.glob("*.py"), *ROOT.glob("frontend/*.py"), *ROOT.glob("tests/*.py")]:
            stack = []
            with path.open("rb") as f:
                for tok in tokenize.tokenize(f.readline):
                    if tok.type in (tokenize.STRING, tokenize.FSTRING_START) and stack:
                        quote = tok.string.lstrip("rRbBuUfF")[:1]
                        if quote == stack[-1]:
                            offenders.append(f"{path.name}:{tok.start[0]}")
                    if tok.type == tokenize.FSTRING_START:
                        stack.append(tok.string.lstrip("rRbBuUfF")[:1])
                    elif tok.type == tokenize.FSTRING_END:
                        stack.pop()
        self.assertEqual(offenders, [])


# --- compiler._clean -----------------------------------------------------------
class CleanTest(unittest.TestCase):
    DOC = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}"

    def test_plain_source_untouched(self):
        self.assertEqual(compiler._clean(self.DOC), self.DOC)

    def test_strips_leading_fence(self):
        self.assertEqual(compiler._clean(f"```latex\n{self.DOC}\n```"), self.DOC)

    def test_prose_around_fenced_block(self):
        # Gemma has no system-instruction support and often chats around the code.
        raw = f"Here is the corrected document:\n\n```latex\n{self.DOC}\n```\nHope this helps!"
        self.assertEqual(compiler._clean(raw), self.DOC)

    def test_prose_without_fence(self):
        raw = f"Sure! Below is the LaTeX.\n{self.DOC}\nLet me know."
        self.assertEqual(compiler._clean(raw), self.DOC)

    def test_backticks_inside_listing_survive(self):
        doc = ("\\documentclass{article}\n\\begin{document}\n\\begin{lstlisting}\n"
               "```python\nprint(1)\n```\n\\end{lstlisting}\n\\end{document}")
        self.assertEqual(compiler._clean(doc), doc)

    def test_none(self):
        self.assertEqual(compiler._clean(None), "")


# --- main._find_pdf -------------------------------------------------------------
class FindPdfTest(TempCache):
    def test_relative_path_from_cwd(self):
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "x.pdf").write_bytes(b"%PDF")
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            self.assertEqual(main._find_pdf("elsewhere/x.pdf").resolve(), (other / "x.pdf").resolve())
        finally:
            os.chdir(cwd)

    def test_bare_name_resolves_in_input(self):
        (self.root / "input/Exam.pdf").write_bytes(b"%PDF")
        self.assertEqual(main._find_pdf("Exam.pdf"), self.root / "input/Exam.pdf")

    def test_default_finds_uppercase_extension(self):
        (self.root / "input/Scan.PDF").write_bytes(b"%PDF")
        self.assertEqual(main._find_pdf(None).name, "Scan.PDF")

    def test_missing_exits(self):
        with self.assertRaises(SystemExit):
            main._find_pdf("nope.pdf")


# --- cache invalidation when the source PDF changes -------------------------------
class SourceChangeTest(TempCache):
    def test_detects_replaced_pdf(self):
        pdf = self.root / "input/Exam.pdf"
        pdf.write_bytes(b"%PDF old")
        state.save_state("Exam", {"source_sha256": state.file_sha256(pdf), "extraction_result": {}})
        self.assertFalse(main._source_changed(pdf, "Exam"))
        pdf.write_bytes(b"%PDF new content")
        self.assertTrue(main._source_changed(pdf, "Exam"))

    def test_legacy_state_without_hash_is_kept(self):
        pdf = self.root / "input/Exam.pdf"
        pdf.write_bytes(b"%PDF")
        state.save_state("Exam", {"extraction_result": {}})
        self.assertFalse(main._source_changed(pdf, "Exam"))

    def test_extraction_records_hash(self):
        pdf = self.root / "input/Exam.pdf"
        pdf.write_bytes(b"%PDF")
        resp = SimpleNamespace(text=json.dumps({"blocks": []}))
        with mock.patch.object(gemini, "upload_pdf"), \
                mock.patch.object(gemini, "generate", return_value=resp), \
                mock.patch.object(extractor, "_schema"):
            quiet(extractor.run, pdf, "Exam")
        self.assertEqual(state.load_state("Exam")["source_sha256"], state.file_sha256(pdf))


# --- main(): user-facing errors ---------------------------------------------------
class MainErrorTest(TempCache):
    def test_runtime_error_exits_cleanly(self):
        (self.root / "input/Exam.pdf").write_bytes(b"%PDF")
        argv = ["main.py", "--skip-preflight"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}), \
                mock.patch.object(extractor, "run", side_effect=RuntimeError("Model did not return valid JSON")):
            with self.assertRaises(SystemExit) as cm:
                quiet(main.main)
        self.assertIn("Model did not return valid JSON", str(cm.exception.code))


# --- gemini: fallback chain + ledger ------------------------------------------------
def api_error(code, message="boom"):
    from google.genai import errors
    return errors.APIError(code, {"error": {"code": code, "message": message}})


class FakeModels:
    """Scripted generate_content: per model id, a list of exceptions/responses."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        outcome = self.script[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class GeminiTest(TempCache):
    def setUp(self):
        super().setUp()
        chain = list(config._DEFAULT_CHAIN)
        for target, value in [
            ("gemini.MODEL_CHAIN", chain),
            ("gemini._SPECS_BY_ID", {m.model_id: m for m in chain}),
            ("gemini.MAX_MODEL_ATTEMPTS", 3),
            ("gemini._throttle", lambda spec: None),
            ("gemini.time.sleep", lambda s: None),
        ]:
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        gemini.set_api_key("test-key")
        self.ids = [m.model_id for m in chain]

    def run_chain(self, script, **kw):
        fake = FakeModels(script)
        gemini._client = SimpleNamespace(models=fake)
        return quiet(gemini.generate, "prompt", **kw), fake

    def used(self, model_id):
        spec = gemini._SPECS_BY_ID[model_id]
        return spec.daily_limit - gemini.requests_remaining(model_id)

    def test_primary_success_records_one_request(self):
        resp, fake = self.run_chain({self.ids[0]: ["ok"]})
        self.assertEqual(resp, "ok")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.used(self.ids[0]), 1)

    def test_per_day_429_switches_without_retry(self):
        resp, fake = self.run_chain({
            self.ids[0]: [api_error(429, "Quota exceeded ... PerDay")],
            self.ids[1]: ["ok2"],
        })
        self.assertEqual(resp, "ok2")
        self.assertEqual([c[0] for c in fake.calls], [self.ids[0], self.ids[1]])
        self.assertEqual(self.used(self.ids[0]), 0)  # failures are never recorded
        self.assertEqual(gemini.active_model(), "Gemini 3.5 Flash")

    def test_transient_retried_then_falls_back(self):
        resp, fake = self.run_chain({
            self.ids[0]: [api_error(503)] * 3,
            self.ids[1]: [api_error(404)],
            self.ids[2]: ["gemma"],
        })
        self.assertEqual(resp, "gemma")
        self.assertEqual(len(fake.calls), 5)

    def test_transient_then_success_on_same_model(self):
        resp, fake = self.run_chain({self.ids[0]: [api_error(500), "ok"]})
        self.assertEqual(resp, "ok")
        self.assertEqual(self.used(self.ids[0]), 1)

    def test_all_exhausted_raises_budget_exceeded(self):
        with self.assertRaises(gemini.BudgetExceeded) as cm:
            self.run_chain({m: [api_error(403)] for m in self.ids})
        self.assertIn("Wait ~24 hours", str(cm.exception))

    def test_unexpected_api_error_propagates(self):
        from google.genai import errors
        with self.assertRaises(errors.APIError):
            self.run_chain({self.ids[0]: [api_error(401)]})

    def test_local_ledger_exhaustion_skips_model(self):
        ts = datetime.now(timezone.utc).isoformat()
        gemini.USAGE_LEDGER.write_text(json.dumps({gemini._fingerprint(): {self.ids[0]: [ts] * 20}}))
        resp, fake = self.run_chain({self.ids[1]: ["ok2"]})
        self.assertEqual([c[0] for c in fake.calls], [self.ids[1]])

    def test_old_and_corrupt_ledger_entries_ignored(self):
        gemini.USAGE_LEDGER.write_text(json.dumps(
            {gemini._fingerprint(): {self.ids[0]: ["2000-01-01T00:00:00+00:00", "garbage"]}}))
        self.assertEqual(self.used(self.ids[0]), 0)
        gemini.USAGE_LEDGER.write_text("{not json")
        self.assertEqual(self.used(self.ids[0]), 0)

    def test_new_key_gets_fresh_budget(self):
        self.run_chain({self.ids[0]: ["ok"]})
        gemini.set_api_key("another-key")
        self.assertEqual(self.used(self.ids[0]), 0)
        self.assertNotIn("test-key", gemini.USAGE_LEDGER.read_text())  # only fingerprints on disk

    def test_gemma_gets_prompt_folded_capabilities(self):
        _, fake = self.run_chain({
            self.ids[0]: [api_error(404)], self.ids[1]: [api_error(404)], self.ids[2]: ["{}"],
        }, json_schema=extractor._schema(), system_instruction="SYS")
        model, contents, cfg = fake.calls[-1]
        self.assertTrue(contents.startswith("SYS\n\n"))
        self.assertIn("ONLY a single valid JSON object", contents)
        self.assertIsNone(cfg.system_instruction)
        self.assertIsNone(cfg.response_schema)
        self.assertIsNone(cfg.thinking_config)


# --- extractor ----------------------------------------------------------------------
class ExtractorTest(unittest.TestCase):
    def test_parse_json_variants(self):
        good = {"blocks": [{"type": "title", "text": "T"}]}
        self.assertEqual(extractor._parse_json(json.dumps(good)), good)
        self.assertEqual(extractor._parse_json("```json\n" + json.dumps(good) + "\n```"), good)
        self.assertEqual(extractor._parse_json("Sure:\n" + json.dumps(good) + "\nDone"), good)
        for bad in ("", None, "no json here", "[1,2]", '{"x": 1}', '{"blocks": null}'):
            with self.assertRaises(RuntimeError):
                extractor._parse_json(bad)

    def test_iter_diagrams_tolerates_junk(self):
        data = {"blocks": [None, {"diagrams": None}, {"diagrams": [1, {"page": 1}]}, {}]}
        self.assertEqual(list(extractor.iter_diagrams(data)), [("B2", 1, {"page": 1})])


# --- processor ----------------------------------------------------------------------
class ProcessorTest(TempCache):
    def run_crop(self, diagrams, name="Exam"):
        from PIL import Image
        state.save_state(name, {"extraction_result": {"blocks": [{"diagrams": diagrams}]}})
        page = Image.new("RGB", (1000, 2000), "white")
        with mock.patch("pdf2image.convert_from_path", return_value=[page]):
            return quiet(processor.run, self.root / "x.pdf", name)

    def test_crops_and_skips_bad_boxes(self):
        crops = self.run_crop([
            {"page": 1, "position": 1, "box_2d": [100, 200, 300, 400]},
            {"page": 1, "position": 2, "box_2d": [300, 400, 100, 200]},  # swapped corners
            {"page": 1, "position": None, "box_2d": ["a", 1, 2, 3]},
            {"page": 1, "position": 4, "box_2d": [1, 2, 3]},
            {"page": 1, "position": 5, "box_2d": [500, 500, 500, 500]},  # degenerate
            {"page": None, "box_2d": [0, 0, 10, 10]},                    # no page
        ])
        self.assertEqual([c["position"] for c in crops], [1, 2])
        from PIL import Image
        with Image.open(crops[0]["path"]) as im:
            # 200x200 px box (x 200-400 of 1000, y 200-600 of 2000) + 5% pad each side
            self.assertEqual(im.size, (220, 440))
        self.assertEqual(state.load_state("Exam")[processor.CROP_KEY], crops)

    def test_purges_only_this_files_stale_crops(self):
        images = self.root / "cache/images"
        (images / "Ex[1]_page_9_q_B0_alt_0_pos_1.png").write_bytes(b"old")
        (images / "Ex1_page_9_q_B0_alt_0_pos_1.png").write_bytes(b"other file")
        self.run_crop([], name="Ex[1]")
        self.assertFalse((images / "Ex[1]_page_9_q_B0_alt_0_pos_1.png").exists())
        self.assertTrue((images / "Ex1_page_9_q_B0_alt_0_pos_1.png").exists())


# --- compiler -------------------------------------------------------------------------
class CompilerTest(TempCache):
    def setUp(self):
        super().setUp()
        state.save_state("Exam", {"extraction_result": {"blocks": []}, "cropped_images": []})

    def run_loop(self, compile_results, **kw):
        gen = mock.Mock(side_effect=lambda *a, **k: SimpleNamespace(text=CleanTest.DOC))
        with mock.patch.object(gemini, "generate", gen), \
                mock.patch.object(compiler, "_compile", side_effect=compile_results):
            result = quiet(compiler.run, self.root / "Exam.pdf", "Exam", **kw)
        return result, gen.call_count

    def test_success_first_try_costs_one_request(self):
        result, calls = self.run_loop([(True, "")])
        self.assertTrue(result["success"])
        self.assertEqual(calls, 1)

    def test_correction_requests_are_capped(self):
        result, calls = self.run_loop([(False, "! Undefined control sequence.")] * 10)
        self.assertFalse(result["success"])
        self.assertEqual(calls, 1 + config.MAX_CORRECTION_LOOPS)
        self.assertEqual(result["attempts"], 1 + config.MAX_CORRECTION_LOOPS)

    def test_resume_reuses_generated_tex(self):
        self.run_loop([(False, "! x")] * 10)
        result, calls = self.run_loop([(True, "")])
        self.assertTrue(result["success"])
        self.assertEqual(calls, 0)  # no new generation request on resume

    def test_cached_success_skips_phase(self):
        self.run_loop([(True, "")])
        _, calls = self.run_loop([])
        self.assertEqual(calls, 0)

    def test_overfull_threshold(self):
        (self.root / "output/t.pdf").write_bytes(b"%PDF")
        for log, expected in [("Overfull \\hbox (0.5pt too wide)", True),
                              ("Overfull \\hbox (25.3pt too wide)", False)]:
            (self.root / "output/t.log").write_text(log)
            with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                self.assertEqual(compiler._compile(self.root / "output/t.tex")[0], expected)

    def test_errors_extracts_bang_lines(self):
        log = "noise\n! Undefined control sequence.\nl.5 \\foo\nmore\n"
        self.assertIn("! Undefined control sequence.", compiler._errors(log))


# --- evaluator ------------------------------------------------------------------------
class EvaluatorTest(TempCache):
    def run_eval(self, orig, comp):
        (self.root / "output/Exam_LaTeX_compiled.pdf").write_bytes(b"%PDF")
        texts = iter([orig, comp])
        with mock.patch.object(evaluator, "_extract_text", side_effect=lambda p: next(texts)):
            return quiet(evaluator.run, self.root / "Exam.pdf", "Exam")

    def test_reordered_text_passes(self):
        self.assertTrue(self.run_eval("abcdefghij", "jihgfedcbaxyz")["success"])

    def test_dropped_text_fails(self):
        result = self.run_eval("abcdefghij", "abc")
        self.assertFalse(result["success"])
        self.assertEqual(result["score"], 0.3)

    def test_scanned_original_passes_with_note(self):
        self.assertIn("note", self.run_eval("", "anything"))

    def test_missing_output(self):
        self.assertFalse(quiet(evaluator.run, self.root / "Exam.pdf", "Exam")["success"])


# --- state ------------------------------------------------------------------------------
class StateTest(TempCache):
    def test_roundtrip_and_corrupt_file(self):
        state.save_state("Exam", {"k": "ü"})
        self.assertEqual(state.load_state("Exam"), {"k": "ü"})
        state.state_file("Exam").write_text("{half")
        self.assertEqual(state.load_state("Exam"), {})
        self.assertEqual(list((self.root / "cache").glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
