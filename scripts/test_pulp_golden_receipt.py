#!/usr/bin/env python3
from __future__ import annotations
import datetime as dt, importlib.util, json, tempfile, unittest
from pathlib import Path
P=Path(__file__).resolve().parents[1]/"providers/common/pulp-golden-receipt.py"
spec=importlib.util.spec_from_file_location("receipt",P); receipt=importlib.util.module_from_spec(spec); spec.loader.exec_module(receipt)
class ReceiptTests(unittest.TestCase):
 def good(self, path, baked="2026-10-01T00:00:00Z"):
  path.write_text(json.dumps({"schema":1,"manifest_sha256":"a"*64,"source_commit":"b"*40,"provider_digests":{"skia":"c"*64},"image_digest":"d"*64,"baked_at":baked,"observed_versions":{"cmake":"4.4.3","python":"3.12"}}))
 def test_fresh_receipt_is_advisory_green(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/"r.json"; self.good(p); result,code=receipt.inspect(p,now=dt.datetime(2026,10,9,tzinfo=dt.timezone.utc),max_age_days=14); self.assertEqual(code,0); self.assertEqual(result["status"],"fresh")
 def test_stale_receipt_only_refuses_promotion(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/"r.json"; self.good(p); now=dt.datetime(2026,10,20,tzinfo=dt.timezone.utc)
   result,code=receipt.inspect(p,now=now,max_age_days=14); self.assertEqual(code,0); self.assertEqual(result["status"],"stale")
   _,code=receipt.inspect(p,now=now,max_age_days=14,promotion=True); self.assertEqual(code,2)
 def test_manifest_mismatch_is_negative_control(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/"r.json"; self.good(p); _,code=receipt.inspect(p,now=dt.datetime(2026,10,9,tzinfo=dt.timezone.utc),max_age_days=14,expected_manifest="e"*64,promotion=True); self.assertEqual(code,2)
 def test_missing_field_refuses(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/"r.json"; self.good(p); value=json.loads(p.read_text()); del value["image_digest"]; p.write_text(json.dumps(value))
   with self.assertRaises(ValueError): receipt.load(p)
if __name__ == "__main__": unittest.main()
