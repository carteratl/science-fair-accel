import json
import os
import unittest

from tests.helpers import tmpdir
from accellib import config


class TestNames(unittest.TestCase):
    def test_good_names(self):
        for n in ("Base", "Top", "Top2", "tower_top", "A"):
            self.assertIsNone(config.check_name(n), n)

    def test_bad_names(self):
        for n in ("", "1top", "top floor", "top-floor", "MAR1", "Sep2", "june15", "dec_3", "ABCDEFGHIJKLM"):
            self.assertIsNotNone(config.check_name(n), n)

    def test_labels_longer(self):
        self.assertIsNone(config.check_label("h8_braced_heavy"))
        self.assertIsNotNone(config.check_label("x" * 21))

    def test_name_taken(self):
        cfg = config.default_config()
        cfg["ports"]["A"]["name"] = "Base"
        self.assertTrue(config.name_taken(cfg, "base"))
        self.assertFalse(config.name_taken(cfg, "Base", except_port="A"))


class TestSaveLoad(unittest.TestCase):
    def test_roundtrip_and_backup(self):
        d = tmpdir()
        cfg = config.load(d)
        cfg["ports"]["A"]["name"] = "Base"
        config.save(cfg, d)
        cfg["ports"]["B"]["name"] = "Top"
        config.save(cfg, d)
        self.assertTrue(os.path.exists(os.path.join(d, "sensors.json.bak")))
        back = config.load(d)
        self.assertEqual(back["ports"]["A"]["name"], "Base")
        self.assertEqual(back["ports"]["B"]["name"], "Top")
        self.assertEqual(config.port_addr(back, "C"), 0x69)
        self.assertEqual(config.port_bus(back, "B"), 3)

    def test_corrupt_refused(self):
        d = tmpdir()
        with open(os.path.join(d, "sensors.json"), "w") as f:
            f.write("{not json")
        with self.assertRaises(config.ConfigError) as cm:
            config.load(d)
        self.assertIn(".bak", str(cm.exception))

    def test_partial_file_merged(self):
        d = tmpdir()
        with open(os.path.join(d, "sensors.json"), "w") as f:
            json.dump({"ports": {"A": {"name": "Base"}}}, f)
        cfg = config.load(d)
        self.assertEqual(cfg["ports"]["A"]["name"], "Base")
        self.assertEqual(cfg["ports"]["A"]["bus"], 1)
        self.assertEqual(cfg["rate_hz"], 100)

    def test_bad_setting_refused(self):
        d = tmpdir()
        with open(os.path.join(d, "sensors.json"), "w") as f:
            json.dump({"rate_hz": 123}, f)
        with self.assertRaises(config.ConfigError):
            config.load(d)


class TestLock(unittest.TestCase):
    def test_second_instance_refused(self):
        d = tmpdir()
        a = config.InstanceLock(d, "accel-setup").acquire()
        try:
            with self.assertRaises(config.LockError) as cm:
                config.InstanceLock(d, "accel-record").acquire()
            self.assertIn("accel-setup", str(cm.exception))
        finally:
            a.release()
        config.InstanceLock(d, "accel-record").acquire().release()


if __name__ == "__main__":
    unittest.main()
