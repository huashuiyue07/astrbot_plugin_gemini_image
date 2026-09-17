"""限流器（冷却 + 每日配额）测试。"""

import asyncio

from core.limiter import UsageLimiter


def test_first_call_passes_and_records(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=30, daily_quota=2)
        assert await limiter.check("u1") is None

        await limiter.consume("u1")
        used, quota = await limiter.usage("u1")
        assert (used, quota) == (1, 2)

    asyncio.run(scenario())


def test_cooldown_blocks_second_call(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=300, daily_quota=0)
        await limiter.consume("u1")
        denied = await limiter.check("u1")
        assert denied is not None
        assert "冷却" in denied

    asyncio.run(scenario())


def test_daily_quota_blocks(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=0, daily_quota=2)
        for _ in range(2):
            assert await limiter.check("u1") is None
            await limiter.consume("u1")

        denied = await limiter.check("u1")
        assert denied is not None
        assert "今日" in denied

    asyncio.run(scenario())


def test_quota_zero_means_unlimited(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=0, daily_quota=0)
        for _ in range(50):
            await limiter.consume("u1")
        assert await limiter.check("u1") is None

    asyncio.run(scenario())


def test_rollover_clears_usage_on_new_day(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=0, daily_quota=1)
        await limiter.consume("u1")
        assert await limiter.check("u1") is not None

        # 手动把记录的日期改成昨天，模拟跨天
        limiter._state["date"] = "2000-01-01"
        assert await limiter.check("u1") is None
        used, _ = await limiter.usage("u1")
        assert used == 0

    asyncio.run(scenario())


def test_state_persists_across_instances(tmp_path):
    async def scenario():
        path = tmp_path / "usage.json"
        limiter = UsageLimiter(path, cooldown_seconds=0, daily_quota=5)
        await limiter.consume("u1")

        reloaded = UsageLimiter(path, cooldown_seconds=0, daily_quota=5)
        used, _ = await reloaded.usage("u1")
        assert used == 1

    asyncio.run(scenario())


def test_reset_clears_selected_user(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=0, daily_quota=5)
        await limiter.consume("u1")
        await limiter.consume("u2")

        assert await limiter.reset("u1") == 1
        assert (await limiter.usage("u1"))[0] == 0
        assert (await limiter.usage("u2"))[0] == 1

        assert await limiter.reset() == 1
        assert (await limiter.usage("u2"))[0] == 0

    asyncio.run(scenario())


def test_global_quota_blocks_everyone(tmp_path):
    """单人的配额挡不住「多人轮流刷」，全局上限是最后一道闸。"""

    async def scenario():
        limiter = UsageLimiter(
            tmp_path / "usage.json", cooldown_seconds=0, daily_quota=0, global_daily_quota=3
        )
        for user in ("u1", "u2", "u3"):
            assert await limiter.check(user) is None
            await limiter.consume(user)

        assert await limiter.total_usage() == 3
        denied = await limiter.check("u4")
        assert denied is not None
        assert "总量" in denied

    asyncio.run(scenario())


def test_global_quota_zero_means_unlimited(tmp_path):
    async def scenario():
        limiter = UsageLimiter(tmp_path / "usage.json", cooldown_seconds=0, global_daily_quota=0)
        for i in range(10):
            await limiter.consume(f"u{i}")
        assert await limiter.check("someone-new") is None
        assert await limiter.total_usage() == 10

    asyncio.run(scenario())


def test_global_quota_resets_on_new_day(tmp_path):
    async def scenario():
        limiter = UsageLimiter(
            tmp_path / "usage.json", cooldown_seconds=0, daily_quota=0, global_daily_quota=1
        )
        await limiter.consume("u1")
        assert await limiter.check("u2") is not None

        limiter._state["date"] = "2000-01-01"
        assert await limiter.check("u2") is None
        assert await limiter.total_usage() == 0

    asyncio.run(scenario())
