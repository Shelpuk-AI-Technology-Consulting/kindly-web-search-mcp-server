from __future__ import annotations

import sys
from pathlib import Path
import os
import unittest

import anyio
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestStackExchangeApiClient(unittest.TestCase):
    def test_fetch_question_and_answers_paginated(self) -> None:
        async def run() -> None:
            os.environ.pop("STACKEXCHANGE_KEY", None)
            os.environ["STACKEXCHANGE_FILTER"] = "test_filter"

            from kindly_web_search_mcp_server.content.stackexchange import (
                StackExchangeApiClient,
                StackExchangeTarget,
            )

            calls: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                calls.append(str(request.url))

                if str(request.url).startswith(
                    "https://api.stackexchange.com/2.3/questions/11227809?"
                ):
                    self.assertIn("site=stackoverflow", str(request.url))
                    self.assertIn("filter=test_filter", str(request.url))
                    return httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "question_id": 11227809,
                                    "title": "T",
                                    "link": "https://stackoverflow.com/questions/11227809/t",
                                    "score": 1,
                                    "creation_date": 1700000000,
                                    "owner": {"display_name": "asker", "link": "x"},
                                    "body_markdown": "Q",
                                }
                            ],
                            "has_more": False,
                        },
                    )

                if str(request.url).startswith(
                    "https://api.stackexchange.com/2.3/questions/11227809/answers?"
                ):
                    self.assertIn("pagesize=100", str(request.url))
                    self.assertIn("page=1", str(request.url))
                    return httpx.Response(
                        200,
                        json={
                            "items": [
                                {
                                    "answer_id": 1,
                                    "score": 1,
                                    "is_accepted": True,
                                    "creation_date": 1700000001,
                                    "owner": {"display_name": "a1"},
                                    "body_markdown": "A1",
                                }
                            ],
                            "has_more": False,
                        },
                    )

                raise AssertionError(f"Unexpected request URL: {request.url}")

            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http_client:
                api = StackExchangeApiClient(http_client=http_client)
                target = StackExchangeTarget(site="stackoverflow", question_id=11227809, answer_id=None)
                question = await api.fetch_question(target)
                answers = await api.fetch_all_answers(target)

            self.assertEqual(question["question_id"], 11227809)
            self.assertEqual(len(answers), 1)
            self.assertTrue(answers[0]["is_accepted"])
            self.assertEqual(len(calls), 2)

        anyio.run(run)


if __name__ == "__main__":
    unittest.main()
