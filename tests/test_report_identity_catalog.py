"""Synthetic missing-share provenance, hashed registry and real plan/publish."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import campaign
from quarter_catalog import compose_quarter_catalog, freeze_catalog, validate_catalog_lock, copy_frozen_catalog
from report_identity_catalog import supplement_catalog, load_registry, report_sources, pack_sources, encoded
from test_quarter_catalog import current, overlay
import test_frozen_catalog_campaign as frozen_tests
from traversal import atomic_json, read_json

NOW = "2026-10-26T12:00:00Z"
FIXTURE = json.loads((ROOT / "tests/fixtures/portfolio-identity.json").read_text())


def proof(main="018774", share="018775"):
    # Reserved example source and synthetic code rows, not a live-source claim.
    serialized = json.dumps(FIXTURE["proof"], ensure_ascii=False).replace("000001", main).replace("000002", share)
    return json.loads(serialized)


def envelope(value=None, code="018774"):
    value = value or proof()
    report = {key: value[key] for key in ("sourceUrl", "periodEnd", "publishedAt", "kind")}
    report.update(fundCode=code, portfolioIdentityEvidence=value)
    return {"schemaVersion": 1, "sourceMode": "actions", "targetPeriodEnd": "2026-06-30",
        "asOf": "2026-10-26", "retrievedAt": "2026-10-26T01:00:00Z",
        "funds": [{"code": code, "selectedReport": report, "reports": [report],
                   "sourcePortfolioCode": "999999", "shareClassCodes": ["999998", "999999"]}]}


def save_reports(directory, document=None):
    directory = Path(directory); document = document or envelope()
    atomic_json(directory / "companies/co1.json", document)
    atomic_json(directory / "latest.json", {**document, "funds": [],
        "companyFiles": [{"path": "./data/companies/co1.json"}]})


def base_catalog():
    value = current()
    value["funds"][0]["code"] = "018774"
    return compose_quarter_catalog(value, "2026Q2", now=NOW)


class IdentityCatalogTests(unittest.TestCase):
    def test_only_missing_codes_get_shared_proof_registry_without_owner_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); save_reports(root)
            source = base_catalog(); before = copy.deepcopy(source)
            result, parts = supplement_catalog(source, root, "2026Q2", now=NOW)
            self.assertEqual(source, before)
            self.assertEqual([row["code"] for row in result["funds"]], ["018774", "018775"])
            self.assertNotIn("reportIdentityPresenceEvidence", result["funds"][0])
            added = result["funds"][1]
            self.assertIsNone(added["companyCode"])
            self.assertEqual(added["name"], "报告列示份额 018775")
            self.assertEqual(len(added["reportIdentityPresenceEvidence"]), 1)
            self.assertEqual(len(parts), 1)
            item = next(iter(json.loads(raw)["sources"][0] for raw in parts.values()))
            self.assertEqual(item["proof"]["retrievedAt"], FIXTURE["proof"]["retrievedAt"])
            self.assertEqual(item["proof"]["pdfSha256"], FIXTURE["proof"]["pdfSha256"])
            self.assertEqual(item["proof"]["rows"][1]["pages"], [2])
            self.assertFalse(result["quarterUniverse"]["historicalUniverseVerified"])

    def test_existing_current_and_historical_codes_do_not_duplicate_all_pdf_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); save_reports(root, envelope(proof("000001", "000003"), "000001"))
            base = compose_quarter_catalog(current(), "2026Q2", overlay(), now=NOW)
            result, parts = supplement_catalog(base, root, "2026Q2", now=NOW)
            self.assertEqual(parts, {})
            self.assertEqual(result["quarterUniverse"]["reportIdentityRegistry"]["sourceCount"], 0)
            self.assertEqual(result["fundCount"], 2)

    def test_global_aliases_wrong_period_unknown_page_and_unrelated_fund_never_add_codes(self):
        variants = []
        for mutate in [
            lambda x: x["funds"][0]["selectedReport"].pop("portfolioIdentityEvidence"),
            lambda x: x["funds"][0]["selectedReport"]["portfolioIdentityEvidence"]["identitySection"].update(pageNumbersVerified=False),
            lambda x: x["funds"][0]["selectedReport"].update(periodEnd="2026-09-30"),
            lambda x: x["funds"][0].update(code="999997"),
        ]:
            raw = envelope(); mutate(raw); variants.append(raw)
        for raw in variants:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                save_reports(temporary, raw)
                result, parts = supplement_catalog(base_catalog(), temporary, "2026Q2", now=NOW)
                self.assertEqual(result["fundCount"], 1); self.assertEqual(parts, {})
        self.assertEqual(list(report_sources(envelope(), "2026-09-30", now=NOW)), [])

    def test_cached_original_proof_survives_no_reports_and_current_directory_catching_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); directory = root / "published"; save_reports(directory)
            first, parts = supplement_catalog(base_catalog(), directory, "2026Q2", now=NOW)
            binding = freeze_catalog(first, "2026Q2", root / "campaign", identity_parts=parts)
            copy_frozen_catalog(root / "campaign" / binding["path"], directory, "2026Q2")
            (directory / "latest.json").unlink()
            updated = copy.deepcopy(first)
            updated["funds"][1].update(companyCode="official-current", name="当前目录的正式名字")
            base = compose_quarter_catalog(updated, "2026Q2", now=NOW)
            second, retained = supplement_catalog(base, directory, "2026Q2", now=NOW)
            self.assertEqual(retained, parts)
            self.assertEqual(second["funds"][1]["name"], "当前目录的正式名字")
            self.assertEqual(second["funds"][1]["companyCode"], "official-current")
            self.assertEqual(second["funds"][1]["reportIdentityPresenceEvidence"],
                             first["funds"][1]["reportIdentityPresenceEvidence"])

    def test_corrupt_cached_registry_keeps_missing_code_as_unverified_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); save_reports(root)
            first, parts = supplement_catalog(base_catalog(), root, "2026Q2", now=NOW)
            binding = freeze_catalog(first, "2026Q2", root / "campaign", identity_parts=parts)
            copy_frozen_catalog(root / "campaign" / binding["path"], root, "2026Q2")
            (root / "latest.json").unlink()
            path = root / next(iter(parts))[2:]; path.write_text("{}")
            result, replacement = supplement_catalog(base_catalog(), root, "2026Q2", now=NOW)
            added = result["funds"][1]
            self.assertEqual(added["code"], "018775"); self.assertTrue(added["historicalCandidate"])
            self.assertNotIn("reportIdentityPresenceEvidence", added)
            self.assertEqual(replacement, {})
            self.assertEqual(result["quarterUniverse"]["reportIdentityRegistry"]["status"], "partial")

    def test_partitioning_and_strict_freeze_reject_missing_digest_or_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); save_reports(root)
            catalog, parts = supplement_catalog(base_catalog(), root, "2026Q2", now=NOW)
            binding = freeze_catalog(catalog, "2026Q2", root / "campaign", identity_parts=parts)
            config = {"requireHistoricalUniverse": True, "targetPeriodEnd": "2026-06-30", "catalogLockVersion": 1,
                      "catalogPath": binding["path"], "catalogDigest": binding["sha256"]}
            validate_catalog_lock("2026Q2", config, binding, root / "campaign")
            part = next((root / "campaign/catalogs/2026Q2-identities").glob("*.json"))
            part.write_bytes(part.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "DIGEST_MISMATCH"):
                validate_catalog_lock("2026Q2", config, binding, root / "campaign")
            part.unlink()
            with self.assertRaisesRegex(ValueError, "MISSING"):
                validate_catalog_lock("2026Q2", config, binding, root / "campaign")
        sources = {}
        for index in range(3):
            item = proof(f"{100000+index*2:06}", f"{100001+index*2:06}")
            identity = hashlib.sha256(encoded(item)).hexdigest()
            sources[identity] = {"id": identity, "proof": item}
        with patch("report_identity_catalog.MAX_PART_BYTES", 2000):
            parts = pack_sources(sources, "2026-06-30")
            self.assertEqual(len(parts), 3)
            self.assertTrue(all(len(raw) <= 2000 for raw in parts.values()))
        for bound in ("MAX_PARTS", "MAX_TOTAL_BYTES"):
            with patch("report_identity_catalog." + bound, 0):
                with self.assertRaisesRegex(ValueError, "TOO_LARGE"): pack_sources(sources, "2026-06-30")

    def test_one_proof_for_several_missing_codes_and_unbound_reference_rejected(self):
        value = proof()
        value["shareClassCodes"].append("018776")
        value["rows"][1]["codes"].append("018776")
        value["rows"][1]["text"] += " 018776"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary); document = envelope(value)
            document["funds"].append(copy.deepcopy(document["funds"][0]))
            save_reports(directory, document)
            result, parts = supplement_catalog(base_catalog(), directory, "2026Q2", now=NOW)
            self.assertEqual(result["quarterUniverse"]["reportIdentityRegistry"]["sourceCount"], 1)
            self.assertEqual(result["quarterUniverse"]["reportIdentityRegistry"]["codeCount"], 2)
            self.assertEqual(result["funds"][1]["reportIdentityPresenceEvidence"], result["funds"][2]["reportIdentityPresenceEvidence"])
            binding = freeze_catalog(result, "2026Q2", directory / "campaign", identity_parts=parts)
            copy_frozen_catalog(directory / "campaign" / binding["path"], directory, "2026Q2")
            changed = copy.deepcopy(result); changed["funds"][2]["code"] = "999999"
            with self.assertRaisesRegex(ValueError, "REFERENCE"):
                load_registry(changed, directory, "2026Q2", now=NOW)
            sources, candidates, state = load_registry(changed, directory, "2026Q2", now=NOW, strict=False)
            self.assertEqual(candidates, {"999999"})
            self.assertGreater(state["cacheIssueCount"], 0)
            self.assertEqual(next(iter(sources.values()))["codes"], ["018775"])


class IdentityCampaignTests(unittest.TestCase):
    setUp = frozen_tests.FrozenCampaignTests.setUp
    tearDown = frozen_tests.FrozenCampaignTests.tearDown

    def test_real_plan_freeze_publish_retains_pdf_code_provenance_and_root_bytes(self):
        root = current(); root["funds"][0]["code"] = "018774"
        atomic_json("web/data/catalog.json", root)
        original = Path("web/data/catalog.json").read_bytes()
        save_reports("web/data/periods/2026Q2")
        untouched = Path("web/data/periods/2026Q3/catalog.json")
        untouched.parent.mkdir(parents=True); untouched.write_text('{"unchanged":true}\n')
        campaign.plan("2026Q2", "2026-10-26", shards=1, batch_size=2, refresh=False, historical_overlays={})
        frozen = read_json(".cache/campaign/catalogs/2026Q2.json")
        self.assertEqual([row["code"] for row in frozen["funds"]], ["018774", "018775"])
        planned = read_json(".cache/plan/batch-0.json")
        self.assertIn("018775", [row["code"] for row in planned["funds"]])
        self.assertEqual(Path("web/data/catalog.json").read_bytes(), original)
        atomic_json("raw/observed.json", {"asOf": "2026-10-26", "retrievedAt": "2026-10-26T02:00:00Z",
            "errors": [], "barsByStock": {}, "funds": [{"code": "018775", "name": "合成份额", "managers": [],
            "reports": [], "noEquitiesEvidence": {"confirmed": True, "kind": "quarterly", "periodEnd": "2026-06-30",
                "publishedAt": "2026-07-21", "sourceUrl": "https://example.org/quarter.pdf", "page": 8,
                "text": "本基金本报告期末未持有股票。"}}]})
        campaign.subprocess.run(["node", "scripts/build-data.js", "--input", "raw/observed.json",
            "--output", ".cache/parts/part-0/part.json", "--config", ".cache/plan/batch-0.json"], check=True)
        campaign.publish()
        published = read_json("web/data/periods/2026Q2/catalog.json")
        self.assertEqual(published, frozen)
        sources, candidates, status = load_registry(published, "web/data/periods/2026Q2", "2026Q2", now=NOW)
        self.assertEqual(len(sources), 1); self.assertEqual(candidates, set())
        self.assertEqual(status["cacheIssueCount"], 0)
        self.assertEqual(Path("web/data/catalog.json").read_bytes(), original)
        self.assertEqual(untouched.read_text(), '{"unchanged":true}\n')
        self.assertFalse(published["quarterUniverse"]["historicalUniverseVerified"])

    def test_registry_tampering_stops_publish_before_any_subprocess(self):
        save_reports("web/data/periods/2026Q2", envelope(proof("000001", "000002"), "000001"))
        campaign.plan("2026Q2", "2026-10-26", shards=1, batch_size=2, refresh=False, historical_overlays={})
        file = next(Path(".cache/campaign/catalogs/2026Q2-identities").glob("*.json"))
        file.write_bytes(file.read_bytes() + b" ")
        with patch("campaign.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "DIGEST_MISMATCH"): campaign.publish()
            run.assert_not_called()

    def test_quarter_interleaving_preserves_every_original_id_and_config(self):
        matrix = [{"id": str(index), "config": f"batch-{index}.json", "quarter": "2026Q2" if index < 16 else "2026Q3"}
                  for index in range(32)]
        before = copy.deepcopy(matrix)
        result = campaign.interleave_matrix(matrix, ["2026Q2", "2026Q3"])
        self.assertEqual([row["id"] for row in result[:6]], ["0", "16", "1", "17", "2", "18"])
        self.assertEqual(matrix, before)
        self.assertEqual(sorted(result, key=lambda row: int(row["id"])), before)
        self.assertEqual(campaign.interleave_matrix(matrix[:3], ["2026Q2", "2026Q3"]), matrix[:3])


if __name__ == "__main__":
    unittest.main()
