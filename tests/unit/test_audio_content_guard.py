import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routers.scoring import (  # noqa: E402
    audio_plausible_fraction,
    audio_content_cap,
    MAX_VERBATIM_WPS,
)


class AudioPlausibleFractionTest(unittest.TestCase):
    def test_no_audio(self):
        self.assertEqual(audio_plausible_fraction(0, 10), 0.0)

    def test_no_reference(self):
        self.assertEqual(audio_plausible_fraction(3, 0), 0.0)

    def test_none_inputs(self):
        self.assertEqual(audio_plausible_fraction(None, 10), 0.0)
        self.assertEqual(audio_plausible_fraction(3, None), 0.0)

    def test_half_speech(self):
        # 10-word sentence: full ≈ 2.22 s at MAX_VERBATIM_WPS; half ≈ 1.11 s
        voiced = 10 / 2 / MAX_VERBATIM_WPS
        f = audio_plausible_fraction(voiced, 10)
        self.assertAlmostEqual(f, 0.5, places=2)

    def test_full_speech_normal_rate(self):
        # 10 words spoken in 3.5 s → voiced * 4.5 = 15.75 → clamped to 1.0
        f = audio_plausible_fraction(3.5, 10)
        self.assertEqual(f, 1.0)

    def test_slow_read_full(self):
        # 10 words in 8 s → plenty of time → 1.0
        f = audio_plausible_fraction(8.0, 10)
        self.assertEqual(f, 1.0)

    def test_fast_read_full(self):
        # 10 words in 2.2 s (≈273 wpm → just above MAX_VERBATIM_WPS)
        f = audio_plausible_fraction(2.2, 10)
        self.assertAlmostEqual(f, 0.99, places=2)

    def test_impossible_rate_clamped(self):
        # 10 words in 0.5 s → 0.5 * 4.5 = 2.25 words → 22.5% plausible
        f = audio_plausible_fraction(0.5, 10)
        self.assertAlmostEqual(f, 0.225, places=2)


class AudioContentCapTest(unittest.TestCase):
    def test_no_audio(self):
        self.assertEqual(audio_content_cap(0, 10), -1)

    def test_half(self):
        voiced = 10 / 2 / MAX_VERBATIM_WPS
        cap = audio_content_cap(voiced, 10)
        self.assertEqual(cap, 45)  # 0.5 * 90

    def test_full(self):
        cap = audio_content_cap(3.5, 10)
        self.assertEqual(cap, 90)

    def test_never_exceeds_90(self):
        cap = audio_content_cap(20.0, 10)
        self.assertLessEqual(cap, 90)

    def test_negative_reference(self):
        self.assertEqual(audio_content_cap(3.0, -1), -1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
