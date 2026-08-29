"""Tests for authoritative local clock evidence used by live qualification."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from fin_analyse.market.system_clock_evidence import (
    SystemClockEvidence,
    SystemClockEvidenceError,
    SystemdTimesyncClockEvidenceProbe,
)


def _ntp_message(
    *,
    leap: int = 0,
    originate_us: int = 1_000_000,
    receive_us: int = 1_010_500,
    transmit_us: int = 1_011_000,
    destination_us: int = 1_001_000,
    ignored: bool = False,
    jitter_us: int = 23_301,
) -> str:
    return json.dumps(
        {
            "type": "(uuuuittayttttbtt)",
            "data": [
                leap,
                4,
                4,
                2,
                -25,
                5_752,
                900,
                [146, 131, 121, 246],
                originate_us,
                receive_us,
                transmit_us,
                destination_us,
                ignored,
                17_076,
                jitter_us,
            ],
        }
    )


def _runner(
    *,
    status: str,
    message: str,
    poll_interval_us: int = 32_000_000,
    fail: bool = False,
):
    def run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if fail:
            raise subprocess.TimeoutExpired(command, timeout=3)
        if command[0] == "timedatectl":
            return subprocess.CompletedProcess(command, 0, status, "")
        if command[0] == "busctl":
            output = (
                message
                if command[-1] == "NTPMessage"
                else json.dumps({"type": "t", "data": poll_interval_us})
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        raise AssertionError(f"unexpected command: {command}")

    return run


def test_probe_derives_signed_offset_from_ntp_four_timestamps_conservatively() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    evidence = SystemdTimesyncClockEvidenceProbe(
        runner=_runner(
            status="Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=yes\n",
            message=_ntp_message(),
        ),
        clock=lambda: now,
    ).measure()

    # ((T2-T1) + (T3-T4)) / 2 = +10.25ms; qualification rounds away
    # from zero so the persisted integer never understates absolute skew.
    assert evidence.collector_clock_offset_ms == 11
    assert evidence.ntp_jitter_ms == 24
    assert evidence.clock_sync_status == "synchronized"
    assert evidence.timezone == "Asia/Shanghai"
    assert evidence.ntp_enabled is True
    assert evidence.ntp_packet_count == 17_076
    assert evidence.poll_interval_ms == 32_000
    assert evidence.measured_at == now
    assert evidence.source == "systemd-timesyncd-dbus.v1"


def test_probe_rounds_negative_sub_millisecond_offset_away_from_zero() -> None:
    evidence = SystemdTimesyncClockEvidenceProbe(
        runner=_runner(
            status="Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=yes\n",
            message=_ntp_message(
                originate_us=1_000_000,
                receive_us=999_900,
                transmit_us=1_000_000,
                destination_us=1_000_100,
            ),
        )
    ).measure()

    assert evidence.collector_clock_offset_ms == -1


def test_probe_preserves_unsynchronized_state_instead_of_claiming_ready() -> None:
    evidence = SystemdTimesyncClockEvidenceProbe(
        runner=_runner(
            status="Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=no\n",
            message=_ntp_message(),
        )
    ).measure()

    assert evidence.clock_sync_status == "unsynchronized"
    assert evidence.collector_clock_offset_ms == 11


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            "Timezone=UTC\nNTP=yes\nNTPSynchronized=yes\n",
            _ntp_message(),
        ),
        (
            "Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=yes\n",
            _ntp_message(ignored=True),
        ),
        (
            "Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=yes\n",
            _ntp_message(leap=3),
        ),
        (
            "Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=yes\n",
            '{"type":"wrong","data":[]}',
        ),
    ],
)
def test_probe_rejects_wrong_timezone_ignored_or_malformed_ntp_evidence(
    status: str,
    message: str,
) -> None:
    with pytest.raises(SystemClockEvidenceError):
        SystemdTimesyncClockEvidenceProbe(runner=_runner(status=status, message=message)).measure()


def test_probe_sanitizes_command_failure() -> None:
    with pytest.raises(SystemClockEvidenceError) as caught:
        SystemdTimesyncClockEvidenceProbe(
            runner=_runner(status="", message="", fail=True)
        ).measure()

    assert str(caught.value) == "SYSTEM_CLOCK_EVIDENCE_BACKEND_UNAVAILABLE"


def test_old_ntp_message_is_not_qualification_ready() -> None:
    measured_at = datetime(2026, 7, 19, 12, 1, tzinfo=UTC)
    destination_us = int(datetime(2026, 7, 19, 12, 0, tzinfo=UTC).timestamp() * 1_000_000)
    evidence = SystemdTimesyncClockEvidenceProbe(
        runner=_runner(
            status="Timezone=Asia/Shanghai\nNTP=yes\nNTPSynchronized=yes\n",
            message=_ntp_message(
                originate_us=destination_us - 10_000,
                receive_us=destination_us,
                transmit_us=destination_us,
                destination_us=destination_us,
            ),
            poll_interval_us=32_000_000,
        ),
        clock=lambda: measured_at,
    ).measure()

    assert evidence.message_age_ms == 60_000
    assert evidence.qualification_reason_codes() == ("COLLECTOR_CLOCK_EVIDENCE_STALE",)
    assert evidence.accepted_clock_offset_ms is None
    assert evidence.corrected_measured_at is None


def test_future_ntp_message_never_projects_a_corrected_time() -> None:
    measured_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    evidence = SystemClockEvidence(
        measured_at=measured_at,
        source="systemd-timesyncd-dbus.v1",
        timezone="Asia/Shanghai",
        ntp_enabled=True,
        synchronized=True,
        collector_clock_offset_ms=20,
        ntp_jitter_ms=25,
        ntp_packet_count=20,
        poll_interval_ms=32_000,
        message_destination_at=measured_at + timedelta(milliseconds=1),
    )

    assert evidence.qualification_reason_codes() == ("COLLECTOR_CLOCK_MESSAGE_FROM_FUTURE",)
    assert evidence.accepted_clock_offset_ms is None
    assert evidence.corrected_measured_at is None


def test_moderate_clock_offset_is_a_quality_warning_not_a_hard_block() -> None:
    measured_at = datetime(2026, 7, 19, 12, 0, 1, tzinfo=UTC)
    evidence = SystemClockEvidence(
        measured_at=measured_at,
        source="systemd-timesyncd-dbus.v1",
        timezone="Asia/Shanghai",
        ntp_enabled=True,
        synchronized=True,
        collector_clock_offset_ms=595,
        ntp_jitter_ms=25,
        ntp_packet_count=20,
        poll_interval_ms=32_000,
        message_destination_at=measured_at,
    )

    assert evidence.qualification_reason_codes() == ()
    assert evidence.quality_warning_codes() == ("COLLECTOR_CLOCK_OFFSET_ABOVE_PREFERRED",)
    assert evidence.corrected_measured_at == measured_at.replace(microsecond=595_000)
    payload = evidence.to_dict()
    assert payload["quality_status"] == "warning"
    assert payload["quality_warning_codes"] == ["COLLECTOR_CLOCK_OFFSET_ABOVE_PREFERRED"]
    assert payload["preferred_clock_offset_ms"] == 250
    assert payload["max_clock_offset_ms"] == 2_000


def test_hard_blocked_clock_never_projects_a_corrected_time_or_warning() -> None:
    measured_at = datetime(2026, 7, 19, 12, 0, 1, tzinfo=UTC)
    evidence = SystemClockEvidence(
        measured_at=measured_at,
        source="systemd-timesyncd-dbus.v1",
        timezone="Asia/Shanghai",
        ntp_enabled=True,
        synchronized=True,
        collector_clock_offset_ms=2_001,
        ntp_jitter_ms=25,
        ntp_packet_count=20,
        poll_interval_ms=32_000,
        message_destination_at=measured_at,
    )

    assert evidence.corrected_measured_at is None
    payload = evidence.to_dict()
    assert payload["corrected_measured_at"] is None
    assert payload["accepted_clock_offset_ms"] is None
    assert payload["clock_correction_applied"] is False
    assert payload["quality_status"] == "hard_block"
    assert payload["quality_warning_codes"] == []
