"""生图编排测试：提示词构造、图片筛选、落盘与容错。"""

import asyncio
from pathlib import Path

import pytest

from core.errors import GeminiImageError
from core.generator import ImageGenerator


class FakeImage:
    """假的 gemini_webapi Image：save() 会真的写一个小文件，并记录调用情况。"""

    def __init__(self, name: str, fail: bool = False, fail_times: int = 0):
        self.name = name
        #: 永久失败
        self.fail = fail
        #: 前 N 次失败、之后成功（模拟瞬时故障）
        self.fail_times = fail_times
        self.save_calls = 0
        self.save_kwargs: dict | None = None

    async def save(self, **kwargs) -> str:
        self.save_calls += 1
        self.save_kwargs = kwargs
        if self.fail or self.save_calls <= self.fail_times:
            raise OSError("boom")
        target = Path(kwargs.get("path", "temp")) / f"{self.name}.png"
        target.write_bytes(b"fake-png")
        return str(target)


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeCandidate:
    def __init__(self, generated=(), web=()):
        self.generated_images = list(generated)
        self.web_images = list(web)


class FakeOutput:
    def __init__(self, candidate: FakeCandidate, text: str = "说明文字"):
        self.candidates = [candidate]
        self.chosen = 0
        self.text = text


class FakeClient:
    configured = True

    def __init__(self, output=None, exc=None, session=None):
        self.output = output
        self.exc = exc
        self.session = session
        self.calls: list[dict] = []
        self.download_sessions = 0

    async def generate(self, prompt, *, files=None, model=None, temporary=True):
        self.calls.append({"prompt": prompt, "files": files, "model": model, "temporary": temporary})
        if self.exc:
            raise self.exc
        return self.output

    async def create_download_session(self):
        self.download_sessions += 1
        return self.session


def make_generator(client, output_dir, **kwargs) -> ImageGenerator:
    return ImageGenerator(client, output_dir, **kwargs)


def test_text_to_image_adds_prefix(tmp_path):
    gen = make_generator(FakeClient(), tmp_path, prompt_prefix="Generate an image: ")
    assert gen.build_prompt("一只猫", editing=False) == "Generate an image: 一只猫"


def test_edit_uses_edit_prefix(tmp_path):
    gen = make_generator(
        FakeClient(),
        tmp_path,
        prompt_prefix="Generate an image: ",
        edit_prompt_prefix="Edit the provided image and generate a new image: ",
    )
    prompt = gen.build_prompt("换成夜景", editing=True)
    assert prompt.startswith("Edit the provided image")
    assert prompt.endswith("换成夜景")


def test_empty_prefix_keeps_prompt_raw(tmp_path):
    gen = make_generator(FakeClient(), tmp_path, prompt_prefix="")
    assert gen.build_prompt("一只猫", editing=False) == "一只猫"


def test_generate_returns_saved_images(tmp_path):
    async def scenario():
        output = FakeOutput(FakeCandidate(generated=[FakeImage("a"), FakeImage("b")]))
        client = FakeClient(output=output)
        result = await make_generator(client, tmp_path).generate("一只猫")

        assert result.has_images
        assert len(result.image_paths) == 2
        assert all(Path(p).is_file() for p in result.image_paths)
        assert result.text == "说明文字"

    asyncio.run(scenario())


def test_web_images_are_counted_but_not_sent(tmp_path):
    async def scenario():
        output = FakeOutput(FakeCandidate(generated=[], web=[FakeImage("w1"), FakeImage("w2")]))
        result = await make_generator(FakeClient(output=output), tmp_path).generate("猫")

        assert not result.has_images
        assert result.web_image_count == 2

    asyncio.run(scenario())


def test_max_images_truncates_and_reports_dropped(tmp_path):
    async def scenario():
        images = [FakeImage(f"img{i}") for i in range(5)]
        output = FakeOutput(FakeCandidate(generated=images))
        result = await make_generator(FakeClient(output=output), tmp_path, max_images=2).generate("猫")

        assert len(result.image_paths) == 2
        assert result.dropped == 3

    asyncio.run(scenario())


def test_single_save_failure_does_not_break_others(tmp_path):
    async def scenario():
        images = [FakeImage("ok1"), FakeImage("bad", fail=True), FakeImage("ok2")]
        output = FakeOutput(FakeCandidate(generated=images))
        result = await make_generator(FakeClient(output=output), tmp_path).generate("猫")

        assert len(result.image_paths) == 2

    asyncio.run(scenario())


def test_empty_prompt_without_images_raises(tmp_path):
    async def scenario():
        gen = make_generator(FakeClient(), tmp_path)
        with pytest.raises(GeminiImageError):
            await gen.generate("   ")

    asyncio.run(scenario())


def test_input_images_are_passed_as_files(tmp_path):
    async def scenario():
        ref = tmp_path / "input.png"
        ref.write_bytes(b"x")
        output = FakeOutput(FakeCandidate(generated=[FakeImage("a")]))
        client = FakeClient(output=output)

        await make_generator(client, tmp_path).generate("改成夜景", input_images=[str(ref)])

        assert client.calls[0]["files"] == [str(ref)]
        assert client.calls[0]["prompt"].startswith("Edit the provided image")

    asyncio.run(scenario())


def test_client_exception_propagates(tmp_path):
    async def scenario():
        client = FakeClient(exc=GeminiImageError("出错了"))
        with pytest.raises(GeminiImageError):
            await make_generator(client, tmp_path).generate("猫")

    asyncio.run(scenario())


def test_download_session_is_passed_to_save(tmp_path):
    """下载图片走自建 session，以绕开 gemini-webapi 写死的 CurlFollow.SAFE。"""

    async def scenario():
        session = FakeSession()
        image = FakeImage("a")
        client = FakeClient(output=FakeOutput(FakeCandidate(generated=[image])), session=session)
        result = await make_generator(client, tmp_path).generate("猫")

        assert result.image_paths
        assert client.download_sessions == 1
        assert image.save_kwargs.get("client") is session
        assert session.closed is True

    asyncio.run(scenario())


def test_download_failure_is_counted(tmp_path):
    """图生成成功但下载全失败 —— 必须能与「压根没出图」区分开。"""

    async def scenario():
        images = [FakeImage("bad1", fail=True), FakeImage("bad2", fail=True)]
        client = FakeClient(output=FakeOutput(FakeCandidate(generated=images)), session=FakeSession())
        result = await make_generator(client, tmp_path).generate("猫")

        assert result.image_paths == []
        assert result.download_failed == 2
        assert not result.has_images

    asyncio.run(scenario())


def test_partial_download_failure(tmp_path):
    async def scenario():
        images = [FakeImage("ok"), FakeImage("bad", fail=True)]
        client = FakeClient(output=FakeOutput(FakeCandidate(generated=images)), session=FakeSession())
        result = await make_generator(client, tmp_path).generate("猫")

        assert len(result.image_paths) == 1
        assert result.download_failed == 1

    asyncio.run(scenario())


def test_falls_back_when_download_session_unavailable(tmp_path):
    """建不出 session 时要退回原生下载方式，而不是整体失败。"""

    async def scenario():
        class NoSessionClient(FakeClient):
            async def create_download_session(self):
                raise RuntimeError("no session")

        client = NoSessionClient(output=FakeOutput(FakeCandidate(generated=[FakeImage("a")])))
        result = await make_generator(client, tmp_path).generate("猫")

        assert len(result.image_paths) == 1

    asyncio.run(scenario())


def test_download_is_retried_on_transient_failure(tmp_path):
    """下载失败要重建连接重试一次 —— 403 这类瞬时问题重试即可成功。"""

    async def scenario():
        flaky = FakeImage("flaky", fail_times=1)
        client = FakeClient(output=FakeOutput(FakeCandidate(generated=[flaky])), session=FakeSession())
        result = await make_generator(client, tmp_path).generate("猫")

        assert len(result.image_paths) == 1, "重试后应成功"
        assert result.download_failed == 0
        assert flaky.save_calls == 2

    asyncio.run(scenario())


def test_download_gives_up_after_retries(tmp_path):
    """重试仍失败时，如实计入失败张数（不再谎报成功了）。"""

    async def scenario():
        always = FakeImage("always", fail=True)
        client = FakeClient(output=FakeOutput(FakeCandidate(generated=[always])), session=FakeSession())
        result = await make_generator(client, tmp_path).generate("猫")

        assert result.image_paths == []
        assert result.download_failed == 1
        assert always.save_calls == 2, "应该试满两次才放弃"

    asyncio.run(scenario())


def test_retry_creates_fresh_session(tmp_path):
    """每次尝试都应新建 session，不能复用已出问题的连接。"""

    async def scenario():
        client = FakeClient(
            output=FakeOutput(FakeCandidate(generated=[FakeImage("a", fail_times=1)])),
            session=FakeSession(),
        )
        await make_generator(client, tmp_path).generate("猫")
        assert client.download_sessions == 2

    asyncio.run(scenario())
