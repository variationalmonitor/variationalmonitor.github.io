from pathlib import Path
import tempfile
import unittest

from tools.package_site import stage_site


class BrowserAssetTests(unittest.TestCase):
    def test_entrypoint_and_imports_share_an_asset_version_independent_of_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "public"
            (source / "assets").mkdir(parents=True)
            (source / "index.html").write_text('<link href="assets/style.css"><script src="assets/app.js"></script>')
            (source / "assets/app.js").write_text("import {f} from './utils.js';")
            (source / "assets/utils.js").write_text('export const f=1;')
            (source / "assets/style.css").write_text('body{margin:0}')
            version = stage_site(source, root / "first")
            self.assertEqual((root / "first/index.html").read_text().count('?v='+version), 2)
            self.assertIn('./utils.js?v='+version, (root / "first/assets/app.js").read_text())
            (source / "data.json").write_text('{"updated":true}')
            self.assertEqual(stage_site(source, root / "second"), version)
            (source / "assets/utils.js").write_text('export const f=2;')
            self.assertNotEqual(stage_site(source, root / "third"), version)
            self.assertNotIn('?v=', (source / "index.html").read_text())
