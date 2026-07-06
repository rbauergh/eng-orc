"""The logger must survive markup-hostile dynamic content (model ids like
[qwen3.5-4b], test output, unbalanced tags) — regression for the orc chat
crash on rich markup parsing."""

from rich.console import Console

from engorc.obs.console import Log

HOSTILE = "model [qwen3.5-4b] output with [/closing] and unbalanced [bold and [weird/tags]"


def _recording_log() -> Log:
    log = Log()
    log.set_level("debug")
    log.console = Console(record=True, width=200, force_terminal=False)
    return log


def test_every_log_method_survives_bracket_soup():
    log = _recording_log()
    log.debug(HOSTILE)
    log.info(HOSTILE)
    log.success(HOSTILE)
    log.warn(HOSTILE)
    log.error(HOSTILE)
    log.agent("rev[iewer]", HOSTILE)
    log.step("pro[ject]", HOSTILE)
    log.rule("ti[tle]")
    text = log.console.export_text()
    assert text.count("[qwen3.5-4b]") == 7  # printed verbatim, not eaten as markup


def test_chat_stats_line_regression():
    log = _recording_log()
    log.info("[qwen3.5-4b] 12+34 tokens in 1.2s")
    assert "[qwen3.5-4b] 12+34 tokens" in log.console.export_text()
