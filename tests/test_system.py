from whiskers import system


def test_cpu_limit_reads_the_container_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "CGROUP", tmp_path)
    monkeypatch.setattr(system.os, "sched_getaffinity", lambda pid: set(range(32)), raising=False)
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    assert system.cpu_limit() == 2
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert system.cpu_limit() == 32
    (tmp_path / "cpu.max").write_text("50000 100000\n")
    assert system.cpu_limit() == 1


def test_memory_limit_uses_the_smaller_of_cgroup_and_ram(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "CGROUP", tmp_path)
    (tmp_path / "memory.max").write_text(str(4 * 1024 ** 3))
    assert system.memory_limit_bytes() <= 4 * 1024 ** 3
    (tmp_path / "memory.max").write_text("max")
    assert system.memory_limit_bytes() > 0
