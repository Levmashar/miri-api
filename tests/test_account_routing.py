"""Account-level routing and stateless request failover regression tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from src.api.failover import retry_across_accounts
from src.api.openai_routes import _can_retry_unpinned_response
from src.api.worker_pool import MultiAccountPool, Worker


class AccountRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The tests exercise routing only. Do not let a user's runtime privacy
        # setting perform provider UI work on synthetic workers.
        self._fresh_chat = patch(
            "src.api.worker_pool.fresh_chat_required", return_value=False
        )
        self._temporary_chat = patch(
            "src.api.worker_pool.temporary_required", return_value=False
        )
        self._fresh_chat.start()
        self._temporary_chat.start()
        self.addCleanup(self._fresh_chat.stop)
        self.addCleanup(self._temporary_chat.stop)

    async def _pool(self, accounts) -> MultiAccountPool:
        pool = MultiAccountPool(acquire_timeout=0.05)
        for order, (account_id, tab_count) in enumerate(accounts):
            await pool.register_account(account_id, order, "qwen")
            workers = [
                Worker(
                    index,
                    SimpleNamespace(new_chat=AsyncMock()),
                    SimpleNamespace(),
                    None,
                    account_id,
                    "qwen",
                )
                for index in range(tab_count)
            ]
            await pool.add_workers(account_id, workers)
        return pool

    async def _checkout(self, pool, **kwargs) -> str:
        async with pool.acquire(**kwargs) as worker:
            return worker.account_id

    async def test_round_robin_shares_sequential_requests_between_accounts(self):
        pool = await self._pool((("a", 1), ("b", 1), ("c", 1)))

        selected = [
            await self._checkout(pool, provider="qwen", model="qwen3-max")
            for _ in range(4)
        ]

        self.assertEqual(selected, ["a", "b", "c", "a"])

    async def test_extra_tabs_do_not_receive_extra_sequential_share(self):
        pool = await self._pool((("a", 3), ("b", 1)))

        selected = [
            await self._checkout(pool, provider="qwen", model="qwen3-max")
            for _ in range(6)
        ]

        self.assertEqual(selected, ["a", "b", "a", "b", "a", "b"])

    async def test_busy_or_excluded_next_account_is_skipped(self):
        pool = await self._pool((("a", 1), ("b", 1), ("c", 1)))

        # First selection advances the cursor from a to b.
        self.assertEqual(
            await self._checkout(pool, provider="qwen", model="qwen3-max"), "a"
        )
        # b is the next account but is occupied, so c is used instead.
        async with pool.acquire(
            account_id="b", provider="qwen", model="qwen3-max"
        ):
            self.assertEqual(
                await self._checkout(pool, provider="qwen", model="qwen3-max"),
                "c",
            )

        # The cursor now points to a. Excluding a advances past it to b.
        self.assertEqual(
            await self._checkout(
                pool,
                provider="qwen",
                model="qwen3-max",
                excluded_accounts={"a"},
            ),
            "b",
        )

    async def test_provider_model_cursors_are_independent(self):
        pool = await self._pool((("a", 1), ("b", 1)))

        self.assertEqual(
            await self._checkout(pool, provider="qwen", model="qwen3-max"), "a"
        )
        self.assertEqual(
            await self._checkout(pool, provider="qwen", model="qwen3-coder"), "a"
        )
        self.assertEqual(
            await self._checkout(pool, provider="qwen", model="qwen3-max"), "b"
        )

    async def test_retry_uses_the_next_account_and_offlines_the_failed_one(self):
        pool = await self._pool((("a", 1), ("b", 1)))
        attempts = []

        @retry_across_accounts()
        async def endpoint():
            async with pool.acquire(provider="qwen", model="qwen3-max") as worker:
                attempts.append(worker.account_id)
                if worker.account_id == "a":
                    raise HTTPException(status_code=500, detail="provider failed")
                return worker.account_id

        self.assertEqual(await endpoint(), "b")
        self.assertEqual(attempts, ["a", "b"])
        self.assertFalse(pool._slots["a"].schedulable)

    async def test_chained_responses_are_not_retried_on_another_account(self):
        attempts = 0

        @retry_across_accounts(can_retry=_can_retry_unpinned_response)
        async def endpoint(request):
            nonlocal attempts
            attempts += 1
            raise HTTPException(status_code=500, detail="provider failed")

        with self.assertRaises(HTTPException):
            await endpoint(SimpleNamespace(previous_response_id="resp_previous"))
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
