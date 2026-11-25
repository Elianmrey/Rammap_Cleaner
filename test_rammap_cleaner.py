"""Testes automatizados do RAMMAP CLEANER.

Execute com:
    py -m pip install psutil pytest
    py -m pytest -v

Os testes usam relógio controlado e uma API simulada; não executam chamadas
reais de liberação de memória nem exigem Windows/Admin.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rammap_cleaner_gui import (
    MemoryPressurePolicy,
    WindowsMemoryCleaner,
    format_bytes,
)


class FakeApi:
    def __init__(self, process_outcomes=None, cache_result=(True, "")):
        self.process_outcomes = process_outcomes or {}
        self.cache_result = cache_result
        self.trimmed_pids = []
        self.cache_calls = 0

    def trim_process(self, pid):
        self.trimmed_pids.append(pid)
        return self.process_outcomes.get(pid, ("success", ""))

    def clear_system_file_cache(self):
        self.cache_calls += 1
        return self.cache_result


@dataclass
class FakeProcess:
    pid: int

    @property
    def info(self):
        return {"pid": self.pid, "name": f"process-{self.pid}"}


def test_no_cleanup_before_low_memory_duration():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(8.0, now=0) is False
    assert policy.evaluate(8.0, now=30) is False
    assert policy.evaluate(8.0, now=59.9) is False
    assert policy.evaluate(8.0, now=60) is True


def test_threshold_is_strictly_below_ten_percent():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(10.0, now=0) is False
    assert policy.evaluate(9.9, now=30) is False
    assert policy.evaluate(9.9, now=90) is True


def test_hysteresis_pauses_timer_between_ten_and_fifteen_percent():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(9.0, now=0) is False
    # Acumulou 30s abaixo de 10%; em 12%, o cronômetro é pausado.
    assert policy.evaluate(12.0, now=30) is False
    assert policy.low_duration(now=30) == pytest.approx(30.0)
    # Retorna abaixo de 10%; precisa acumular mais 30s, não contar o intervalo em 12%.
    assert policy.evaluate(9.0, now=100) is False
    assert policy.evaluate(9.0, now=129.9) is False
    assert policy.evaluate(9.0, now=130) is True


def test_recovery_above_fifteen_resets_pressure_episode():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(9.0, now=0) is False
    assert policy.evaluate(9.0, now=30) is False
    assert policy.evaluate(16.0, now=31) is False
    assert policy.low_duration(now=31) == 0
    assert policy.evaluate(9.0, now=100) is False
    assert policy.evaluate(9.0, now=160) is True


def test_only_one_automatic_cleanup_per_pressure_episode():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(7.0, now=0) is False
    assert policy.evaluate(7.0, now=60) is True
    assert policy.evaluate(7.0, now=600) is False
    assert policy.evaluate(7.0, now=900) is False
    # Um novo episódio só é permitido após recuperação acima de 15%.
    assert policy.evaluate(16.0, now=901) is False
    assert policy.evaluate(7.0, now=1000) is False
    assert policy.evaluate(7.0, now=1060) is True


def test_cooldown_blocks_new_episode_for_ten_minutes():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(7.0, now=0) is False
    assert policy.evaluate(7.0, now=60) is True
    assert policy.evaluate(16.0, now=61) is False
    assert policy.evaluate(7.0, now=62) is False
    assert policy.evaluate(7.0, now=122) is False
    # Completa 10 minutos desde a limpeza anterior.
    assert policy.evaluate(7.0, now=660) is True


def test_disabling_automatic_mode_resets_pressure_timer():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(7.0, now=0) is False
    assert policy.evaluate(7.0, now=30) is False
    assert policy.evaluate(7.0, now=40, enabled=False) is False
    assert policy.low_duration(now=40) == 0
    assert policy.evaluate(7.0, now=70) is False
    assert policy.evaluate(7.0, now=130) is True


def test_manual_cleanup_starts_cooldown_and_suppresses_current_episode():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(7.0, now=0) is False
    policy.record_manual_cleanup(now=30, available_percent=7.0)
    assert policy.cooldown_remaining(30) == pytest.approx(600.0)
    assert policy.evaluate(7.0, now=90) is False


def test_cooldown_remaining_never_negative():
    policy = MemoryPressurePolicy()
    policy.record_manual_cleanup(now=100, available_percent=50.0)
    assert policy.cooldown_remaining(200) == pytest.approx(500.0)
    assert policy.cooldown_remaining(900) == 0


def test_cleaner_skips_own_pid_and_system_reserved_pids_and_calls_cache_once():
    fake_api = FakeApi()
    fake_processes = [FakeProcess(0), FakeProcess(4), FakeProcess(100), FakeProcess(200), FakeProcess(999)]
    cleaner = WindowsMemoryCleaner(
        api=fake_api,
        process_iter=lambda _attrs: fake_processes,
        own_pid=999,
    )

    result = cleaner.clean_once()

    assert fake_api.trimmed_pids == [100, 200]
    assert result.processes_scanned == 5
    assert result.processes_trimmed == 2
    assert result.processes_skipped == 3
    assert result.processes_failed == 0
    assert result.cache_cleared is True
    assert fake_api.cache_calls == 1


def test_cleaner_counts_process_failures_and_cache_failure_without_crashing():
    fake_api = FakeApi(
        process_outcomes={
            101: ("failed", "access denied"),
            102: ("skipped", "process exited"),
        },
        cache_result=(False, "privilege missing"),
    )
    cleaner = WindowsMemoryCleaner(
        api=fake_api,
        process_iter=lambda _attrs: [FakeProcess(101), FakeProcess(102), FakeProcess(103)],
        own_pid=999,
    )

    result = cleaner.clean_once()

    assert result.processes_trimmed == 1
    assert result.processes_failed == 1
    assert result.processes_skipped == 1
    assert result.cache_cleared is False
    assert result.cache_error == "privilege missing"
    assert result.has_any_success is True


def test_cleaner_continues_if_one_process_api_call_raises():
    class RaisingApi(FakeApi):
        def trim_process(self, pid):
            if pid == 101:
                raise RuntimeError("simulated API failure")
            return super().trim_process(pid)

    fake_api = RaisingApi()
    cleaner = WindowsMemoryCleaner(
        api=fake_api,
        process_iter=lambda _attrs: [FakeProcess(101), FakeProcess(102)],
        own_pid=999,
    )

    result = cleaner.clean_once()
    assert result.processes_failed == 1
    assert result.processes_trimmed == 1
    assert result.cache_cleared is True


def test_format_bytes_handles_zero_and_gibibytes():
    assert format_bytes(0) == "0.0 B"
    assert format_bytes(1024) == "1.0 KB"
    assert format_bytes(1024**3) == "1.0 GB"


def test_policy_rejects_invalid_percentage():
    policy = MemoryPressurePolicy()
    with pytest.raises(ValueError):
        policy.evaluate(101.0, now=0)


def test_policy_rejects_invalid_threshold_configuration():
    with pytest.raises(ValueError):
        MemoryPressurePolicy(low_threshold_percent=15, recovery_threshold_percent=10)


def test_read_memory_snapshot_calculates_available_and_used(monkeypatch):
    from types import SimpleNamespace
    import rammap_cleaner_gui as app_module

    monkeypatch.setattr(
        app_module.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=16 * 1024, available=4 * 1024),
    )
    snapshot = app_module.read_memory_snapshot()
    assert snapshot.total_bytes == 16 * 1024
    assert snapshot.available_bytes == 4 * 1024
    assert snapshot.used_bytes == 12 * 1024
    assert snapshot.available_percent == pytest.approx(25.0)
    assert snapshot.used_percent == pytest.approx(75.0)


def test_cleaner_still_reports_cache_attempt_if_process_enumeration_fails():
    fake_api = FakeApi()

    def broken_process_iter(_attrs):
        raise RuntimeError("enumeration unavailable")

    cleaner = WindowsMemoryCleaner(
        api=fake_api,
        process_iter=broken_process_iter,
        own_pid=999,
    )
    result = cleaner.clean_once()
    assert result.processes_failed == 1
    assert fake_api.cache_calls == 1
    assert result.cache_cleared is True


def test_manual_cleanup_does_not_restart_pressure_without_recovery():
    policy = MemoryPressurePolicy()
    assert policy.evaluate(8.0, now=0) is False
    policy.record_manual_cleanup(now=30, available_percent=8.0)
    assert policy.evaluate(8.0, now=90) is False
    assert policy.episode_triggered is True


def test_pressure_episode_can_rearm_after_manual_cleanup_and_recovery():
    policy = MemoryPressurePolicy()
    policy.record_manual_cleanup(now=0, available_percent=8.0)
    assert policy.evaluate(16.0, now=1) is False
    assert policy.evaluate(8.0, now=601) is False
    assert policy.evaluate(8.0, now=661) is True
