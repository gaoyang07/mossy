"""The settings schema: parsing, bounds and the user's settings file."""

import stat
import tempfile
import unittest
from pathlib import Path

from dotenv import dotenv_values

from mossy import config


class ParsingTests(unittest.TestCase):
    def test_defaults_apply_when_nothing_is_set(self):
        settings = config.load({})
        self.assertEqual(set(settings), set(config.FIELDS))
        self.assertEqual(settings["MOSS_LIVE_URL"], "https://api.mosi.cn")
        self.assertIs(settings["MOSS_HALF_DUPLEX"], True)
        self.assertEqual(settings["MOSS_LIVE_OUTPUT_RATE"], 16000)
        self.assertEqual(settings["MOSS_API_KEY"], "")

    def test_every_default_is_itself_a_valid_value(self):
        for key, field in config.FIELDS.items():
            with self.subTest(key=key):
                self.assertEqual(config.coerce(key, field.default), field.default)
                text = config.as_text(key, field.default)
                self.assertEqual(config.coerce(key, text), field.default)

    def test_environment_text_is_typed(self):
        settings = config.load(
            {
                "MOSS_HALF_DUPLEX": "off",
                "MOSS_CAMERA": "1",
                "MOSS_CAMERA_FPS": "2.5",
                "MOSS_WEB_UI_PORT": "8001",
                "MOSS_LIVE_VOICE_ID": "  voice-1  ",
                "MOSS_LIVE_PROMPT": " keep my spacing\n",
            }
        )
        self.assertIs(settings["MOSS_HALF_DUPLEX"], False)
        self.assertIs(settings["MOSS_CAMERA"], True)
        self.assertEqual(settings["MOSS_CAMERA_FPS"], 2.5)
        self.assertEqual(settings["MOSS_WEB_UI_PORT"], 8001)
        self.assertEqual(settings["MOSS_LIVE_VOICE_ID"], "voice-1")
        self.assertEqual(settings["MOSS_LIVE_PROMPT"], " keep my spacing\n")

    def test_out_of_range_and_malformed_values_name_the_variable(self):
        cases = {
            "MOSS_CAMERA_FPS": ["0", "10", "fast", "nan"],
            "MOSS_LIVE_SPEED": ["0.4", "1.6"],
            "MOSS_LIVE_VOLUME_DB": ["-21", "17"],
            "MOSS_WEB_UI_PORT": ["0", "70000", "78.5"],
            "MOSS_LIVE_OUTPUT_RATE": ["24000", "44100"],
            "MOSS_POSTURE": ["handstand"],
            "MOSS_EXPRESSION_INTENSITY": ["1.5", "-0.1"],
            "MOSS_LIVE_PROMPT": ["长" * 8001],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value[:12]):
                    with self.assertRaisesRegex(ValueError, key):
                        config.load({key: value})

    def test_page_values_are_validated_like_environment_text(self):
        self.assertEqual(config.coerce("MOSS_CAMERA_MAX_EDGE", 512.0), 512)
        self.assertIs(config.coerce("MOSS_EXPRESSIONS", False), False)
        with self.assertRaises(ValueError):
            config.coerce("MOSS_CAMERA_MAX_EDGE", 512.5)
        with self.assertRaises(ValueError):
            config.coerce("MOSS_LIVE_SPEED", None)


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "mossy" / ".env"

    def reload(self):
        return config.load(dotenv_values(self.path))

    def test_values_round_trip_through_the_file(self):
        prompt = "你是小安。\n第二行有 \"引号\"、\\反斜杠\\ 和 '单引号'。\n#不是注释 $HOME"
        saved = {
            "MOSS_LIVE_PROMPT": prompt,
            "MOSS_LIVE_SPEED": 1.2,
            "MOSS_HALF_DUPLEX": False,
            "MOSS_CAMERA_MAX_EDGE": 512,
            "MOSS_LIVE_VOICE_ID": "6c3152b1-1d4f-4e49-ade3-09f447712fec",
        }
        config.save(saved, self.path)
        loaded = self.reload()
        for key, value in saved.items():
            self.assertEqual(loaded[key], value, key)

    def test_saving_keeps_other_lines_and_private_permissions(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            "# my notes\nMOSS_API_KEY=test-only-key\nNO_PROXY=localhost\nMOSS_LIVE_SPEED=0.8\n"
        )
        self.path.chmod(0o644)
        config.save({"MOSS_LIVE_SPEED": 1.1}, self.path)
        text = self.path.read_text()
        self.assertIn("# my notes\n", text)
        self.assertIn("NO_PROXY=localhost\n", text)
        self.assertEqual(text.count("MOSS_LIVE_SPEED"), 1)
        self.assertEqual(self.reload()["MOSS_API_KEY"], "test-only-key")
        self.assertEqual(self.reload()["MOSS_LIVE_SPEED"], 1.1)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_removing_a_key_restores_its_default(self):
        config.save({"MOSS_LIVE_NAME": "小安"}, self.path)
        self.assertEqual(self.reload()["MOSS_LIVE_NAME"], "小安")
        config.save({"MOSS_LIVE_NAME": None}, self.path)
        self.assertEqual(self.reload()["MOSS_LIVE_NAME"], "MOSS")

    def test_removing_from_a_missing_file_creates_nothing(self):
        config.save({"MOSS_LIVE_NAME": None}, self.path)
        self.assertFalse(self.path.exists())


class TemplateTests(unittest.TestCase):
    def test_env_example_lists_every_setting_at_its_default(self):
        template = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
        settings = {key: value for key, value in template.items() if key.startswith("MOSS_")}
        self.assertEqual(set(settings), set(config.FIELDS))
        self.assertEqual(config.load(settings), config.load({}))
        # The SDK's local connections must not go through a proxy.
        self.assertIn("127.0.0.1", template["NO_PROXY"])


class DescriptionTests(unittest.TestCase):
    def test_the_api_key_is_never_described(self):
        secret = "sk-" + "a" * 40
        described = config.describe(config.load({"MOSS_API_KEY": secret}))
        self.assertNotIn(secret, repr(described))
        key = next(item for item in described if item["key"] == "MOSS_API_KEY")
        self.assertTrue(key["is_set"])
        self.assertEqual(key["value"], "")

    def test_every_field_has_a_known_group_and_scope(self):
        for item in config.describe(config.load({})):
            with self.subTest(key=item["key"]):
                self.assertIn(item["group"], config.GROUPS)
                self.assertIn(item["scope"], ("live", "reconnect", "restart"))
                self.assertTrue(item["label"])


if __name__ == "__main__":
    unittest.main()
