from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.ffmpeg import build_filter_chain, probe_media, audio_args
from app.fs import resolve_output_path
from app.schemas import JobOptions, resolve_target

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo.mp4"


class FilterTests(unittest.TestCase):
    def test_interp_first_is_before_scale(self):
        chain = build_filter_chain(
            denoise="light",
            source_width=1920,
            source_height=1080,
            target_width=3840,
            target_height=2160,
            sharpen=0.35,
            interp_mode="balanced",
            target_fps=60,
            order="interp_first",
        )
        self.assertLess(chain.index("minterpolate="), chain.index("scale=3840"))
        self.assertIn("hqdn3d=", chain)
        self.assertIn("unsharp=", chain)

    def test_sr_first_is_after_scale(self):
        chain = build_filter_chain(
            source_width=1920,
            source_height=1080,
            target_width=3840,
            target_height=2160,
            sharpen=0.35,
            interp_mode="balanced",
            target_fps=60,
            order="sr_first",
        )
        self.assertLess(chain.index("scale=3840"), chain.index("minterpolate="))

    def test_blend_is_valid(self):
        chain = build_filter_chain(
            source_width=640,
            source_height=360,
            target_width=1280,
            target_height=720,
            interp_mode="fast",
            target_fps=60,
        )
        self.assertIn("mi_mode=blend", chain)

    def test_audio_copy_safety(self):
        self.assertEqual(audio_args("copy", "mkv", "pcm_s16le"), ["-c:a", "copy"])
        self.assertIn("aac", audio_args("copy", "mp4", "pcm_s16le"))


class TargetTests(unittest.TestCase):
    def setUp(self):
        if not DEMO.exists():
            self.skipTest("demo video missing")
        self.probe = probe_media(DEMO)

    def test_upscale_and_interp(self):
        options = JobOptions(sr_engine="fast", scale=2, interp="balanced", fps_mode="fixed", fps_value=60)
        target = resolve_target(self.probe, options)
        self.assertEqual((target["width"], target["height"]), (1920, 1080))
        self.assertEqual(target["fps"], 60.0)

    def test_interp_only_keeps_resolution(self):
        options = JobOptions(sr_engine="off", scale=4, interp="balanced", fps_mode="multiple", fps_value=2)
        target = resolve_target(self.probe, options)
        self.assertEqual((target["width"], target["height"]), (960, 540))
        self.assertEqual(target["fps"], 60.0)

    def test_noop_rejected(self):
        options = JobOptions(sr_engine="fast", scale=1, interp="off")
        with self.assertRaises(ValueError):
            resolve_target(self.probe, options)

    def test_low_target_fps_rejected(self):
        options = JobOptions(sr_engine="fast", scale=2, interp="balanced", fps_mode="fixed", fps_value=24)
        with self.assertRaises(ValueError):
            resolve_target(self.probe, options)


class OutputTests(unittest.TestCase):
    def test_output_never_overwrites_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"x")
            options = JobOptions(sr_engine="fast", scale=2, output_dir=tmp, output_name="clip")
            target = {"width": 3840, "height": 2160, "fps": 60}
            output = resolve_output_path(source, options, target)
            self.assertNotEqual(output.resolve(), source.resolve())
            self.assertTrue(output.name.startswith("clip"))

    def test_output_name_extension_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"x")
            options = JobOptions(sr_engine="fast", output_dir=tmp, output_name="movie.mp4")
            target = {"width": 3840, "height": 2160, "fps": 60}
            output = resolve_output_path(source, options, target)
            self.assertEqual(output.name, "movie.mp4")


if __name__ == "__main__":
    unittest.main()
