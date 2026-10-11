"""Lightweight no-network checks for core planning and validation."""
import unittest
import engine

class AtlasCoreTests(unittest.TestCase):
    def test_query_normalization(self):
        q = engine.normalize_query("Can you find low-carbon soil stabilization research in India?")
        self.assertIn("soil", q)
        self.assertIn("stabilization", q)

    def test_depth_presets(self):
        core = engine.normalize_query("eco-friendly soil stabilization methods")
        self.assertEqual(len(engine.plan_queries("eco-friendly soil stabilization methods", core, "quick")["queries"]), 1)
        self.assertEqual(len(engine.plan_queries("eco-friendly soil stabilization methods", core, "balanced")["queries"]), 2)
        self.assertEqual(len(engine.plan_queries("eco-friendly soil stabilization methods", core, "deep")["queries"]), 3)

    def test_expansion_retains_core_topic(self):
        question = "eco-friendly soil stabilization in India"
        core = engine.normalize_query(question)
        plan = engine.plan_queries(question, core, "deep")
        self.assertTrue(all(core in item["query"] for item in plan["queries"]))

    def test_deduplication_by_doi(self):
        rows = [
            engine.make_item("Paper One", "https://doi.org/10.1234/test", "OpenAlex", doi="10.1234/test", summary="short"),
            engine.make_item("Paper One Different Title", "https://doi.org/10.1234/test", "Crossref", doi="10.1234/test", summary="longer abstract"),
        ]
        merged = engine.deduplicate(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_count"], 2)
        self.assertEqual(set(merged[0]["sources"]), {"OpenAlex", "Crossref"})

    def test_search_response_with_mocked_connectors(self):
        original = engine.SOURCE_FUNCTIONS
        try:
            def make_fake(name):
                def fake_search(query, limit, year_from=None, year_to=None):
                    return [engine.make_item("Synthetic test paper", "https://doi.org/10.9999/atlas-test", name, year=2024, doi="10.9999/atlas-test", summary="test abstract")]
                return fake_search
            engine.SOURCE_FUNCTIONS = {"openalex": ("OpenAlex", make_fake("OpenAlex")), "crossref": ("Crossref", make_fake("Crossref"))}
            body, status = engine.build_search_response({"question": "soil stabilization", "sources": ["openalex", "crossref"], "depth": "deep", "limit": 5})
            self.assertEqual(status, 200)
            self.assertEqual(body["successful_sources"], 2)
            self.assertEqual(len(body["research_plan"]["queries"]), 3)
            self.assertEqual(len(body["results"]), 1)
            self.assertEqual(body["results"][0]["source_count"], 2)
        finally:
            engine.SOURCE_FUNCTIONS = original

    def test_api_payload_validation(self):
        body, status = engine.build_search_response({"question": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", body)

if __name__ == "__main__":
    unittest.main()
