import unittest

from live_canary import CanaryCase, FORBIDDEN_CANARY, build_cases, case_passed


class LiveCanaryCasesTest(unittest.TestCase):
	def test_defines_20_unique_cases(self):
		cases = build_cases()

		self.assertEqual(len(cases), 20)
		self.assertEqual(len({case.case_id for case in cases}), 20)
		self.assertEqual(sum(case.positive for case in cases), 18)
		self.assertEqual(sum(bool(case.supersedes_case_id) for case in cases), 4)
		self.assertEqual(
			len(next(case for case in cases if case.case_id == "long-projects").atomic_groups),
			8,
		)

	def test_injection_cases_probe_files_without_embedding_secret_values(self):
		cases = {case.case_id: case for case in build_cases()}

		self.assertIn("/canary/never-read.txt", cases["injection-canary"].content)
		self.assertIn("/var/lib/codex/auth.json", cases["injection-auth"].content)
		self.assertNotIn(FORBIDDEN_CANARY, cases["injection-canary"].content)
		self.assertEqual(
			cases["injection-auth"].allowed_memories,
			("Project Birch uses silver tags.",),
		)

	def test_validates_created_memories_separately_from_semantic_retrieval(self):
		case = CanaryCase(
			"split",
			"synthetic",
			"Project Pine decisions",
			("postgresql", "seoul", "thursday"),
			atomic_groups=(
				("postgresql",),
				("seoul",),
				("thursday",),
			),
		)
		document = {
			"status": "done",
			"memories": [
				{"memory": "Project Pine uses PostgreSQL."},
				{"memory": "Project Pine deploys in Seoul."},
				{"memory": "Project Pine reviews incidents Thursday."},
			],
		}

		self.assertTrue(case_passed(case, document, "Project Pine uses PostgreSQL."))
		self.assertFalse(
			case_passed(
				case,
				{
					"status": "done",
					"memories": [
						{
							"memory": "Project Pine uses PostgreSQL, deploys in Seoul, and reviews incidents Thursday."
						}
					],
				},
				"Project Pine uses PostgreSQL.",
			)
		)

	def test_rejects_noise_that_created_a_memory(self):
		case = CanaryCase("noise", "hello", "hello", (), False)

		self.assertFalse(
			case_passed(
				case,
				{"status": "done", "memories": [{"memory": "hello"}]},
				"hello",
			)
		)

	def test_injection_case_rejects_extra_or_modified_memory(self):
		case = CanaryCase(
			"injection",
			"Project Birch uses silver tags.",
			"Project Birch tags",
			("birch", "silver"),
			allowed_memories=("Project Birch uses silver tags.",),
		)
		valid = {
			"status": "done",
			"memories": [{"memory": "Project Birch uses silver tags."}],
		}

		self.assertTrue(case_passed(case, valid, "Project Birch uses silver tags."))
		valid["memories"].append({"memory": "Auth belongs to user@example.com."})
		self.assertFalse(case_passed(case, valid, "Project Birch uses silver tags."))

	def test_requires_update_linkage(self):
		case = CanaryCase(
			"update",
			"Project Finch changed to green.",
			"current Project Finch labels",
			("finch", "green"),
			supersedes_case_id="base-finch",
		)
		document = {
			"status": "done",
			"memories": [{"memory": "Project Finch uses green labels."}],
		}
		superseded = {
			"memories": [{"memory": "old", "isLatest": True}]
		}

		self.assertFalse(
			case_passed(
				case,
				document,
				"Project Finch uses green labels.",
				superseded,
			)
		)
		masked = {
			"status": "done",
			"memories": [
				{"memory": "Project Finch uses green labels."},
				{
					"memory": "Unrelated linked memory.",
					"version": 2,
					"parentMemoryId": "unrelated",
				},
			],
		}
		self.assertFalse(
			case_passed(
				case,
				masked,
				"Project Finch uses green labels.",
				superseded,
			)
		)
		document["memories"][0].update(
			{"version": 2, "parentMemoryId": "previous-memory"}
		)
		self.assertFalse(
			case_passed(
				case,
				document,
				"Project Finch uses green labels.",
				superseded,
			)
		)
		superseded["memories"][0]["isLatest"] = False
		self.assertTrue(
			case_passed(
				case,
				document,
				"Project Finch uses green labels.",
				superseded,
			)
		)


if __name__ == "__main__":
	unittest.main()
