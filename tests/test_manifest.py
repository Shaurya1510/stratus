import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.system.deployment import parse_manifest


class ManifestTests(unittest.TestCase):
    def test_static_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "stratus.yaml").write_text("name: demo\nruntime: static\nstart: python3 -m http.server $PORT\n")
            self.assertEqual(parse_manifest(root)["runtime"], "static")

    def test_missing_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError): parse_manifest(Path(folder))


if __name__ == "__main__": unittest.main()
