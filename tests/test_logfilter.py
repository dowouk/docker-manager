from app.jobs import JobContext
from app.logfilter import ProgressFilter, is_pull_progress, strip_pull_progress

PULL_OUTPUT = """\
$ docker compose -p uptime -f /docker/uptime/docker-compose.yml pull uptime-kuma
 Image louislam/uptime-kuma:2 Pulling 
    c51afa4fcef3 Pulling fs layer 0B
    c51afa4fcef3 Waiting
    9e2d39a306b3 Already exists 0B
    c51afa4fcef3 Downloading 1.2MB/24.5MB
    c51afa4fcef3 Verifying Checksum
    c51afa4fcef3 Download complete 0B
    c51afa4fcef3 Extracting 5B
    c51afa4fcef3 Pull complete 0B
 Image louislam/uptime-kuma:2 Pulled 
$ docker compose -p uptime -f /docker/uptime/docker-compose.yml up -d --no-deps uptime-kuma
 Container uptime-uptime-kuma-1 Recreated 
"""

KEPT = """\
$ docker compose -p uptime -f /docker/uptime/docker-compose.yml pull uptime-kuma
 Image louislam/uptime-kuma:2 Pulling 
 Image louislam/uptime-kuma:2 Pulled 
$ docker compose -p uptime -f /docker/uptime/docker-compose.yml up -d --no-deps uptime-kuma
 Container uptime-uptime-kuma-1 Recreated 
"""


def test_strip_keeps_only_the_meaningful_steps():
    assert strip_pull_progress(PULL_OUTPUT) == KEPT


def test_lines_that_merely_mention_progress_words_are_kept():
    for line in ("Downloading 12 files", "error: c51afa4fcef3 Extracting failed: no space left",
                 "abc Pull complete", "C51AFA4FCEF3 Extracting 5B", "Image x Pulled"):
        assert not is_pull_progress(line), line
    assert is_pull_progress("c51afa4fcef3 Extracting 5B\r\n")


def test_chunked_output_split_mid_line():
    f = ProgressFilter()
    out = "".join(f.feed(PULL_OUTPUT[i:i + 7]) for i in range(0, len(PULL_OUTPUT), 7)) + f.flush()
    assert out == KEPT


def test_flush_passes_on_a_final_line_without_newline():
    f = ProgressFilter()
    assert f.feed("Done") == ""
    assert f.flush() == "Done"
    assert f.feed("c51afa4fcef3 Extracting 5B") == "" and f.flush() == ""


async def test_job_log_drops_progress_but_last_output_keeps_it(tmp_path):
    output = tmp_path / "pull.txt"
    output.write_text(PULL_OUTPUT.split("\n", 1)[1])
    logged: list[str] = []
    ctx = JobContext(1, logged.append)
    assert await ctx.run(["cat", str(output)]) == 0
    text = "".join(logged)
    assert "Extracting" not in text and "Downloading" not in text
    assert "Image louislam/uptime-kuma:2 Pulled" in text
    assert "c51afa4fcef3 Extracting 5B" in ctx.last_output  # still there for error detection
