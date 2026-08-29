"""Authoritative local clock evidence for A-share qualification captures.

The probe reads the host's existing systemd-timesyncd state.  It never sends
an NTP packet itself and never accepts a caller-supplied offset.  Offset is
derived from the four timestamps in the most recent NTP message and rounded
away from zero before it enters the integer qualification contract.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeGuard

_TIMESYNCD_SIGNATURE = "(uuuuittayttttbtt)"
_TIMEDATECTL_COMMAND = [
    "timedatectl",
    "show",
    "--property=Timezone",
    "--property=NTP",
    "--property=NTPSynchronized",
]
_BUSCTL_COMMAND = [
    "busctl",
    "--json=short",
    "get-property",
    "org.freedesktop.timesync1",
    "/org/freedesktop/timesync1",
    "org.freedesktop.timesync1.Manager",
    "NTPMessage",
]
_BUSCTL_POLL_INTERVAL_COMMAND = [
    "busctl",
    "--json=short",
    "get-property",
    "org.freedesktop.timesync1",
    "/org/freedesktop/timesync1",
    "org.freedesktop.timesync1.Manager",
    "PollIntervalUSec",
]
_CLOCK_MESSAGE_GRACE_MS = 5_000

A_SHARE_CLOCK_PREFERRED_OFFSET_MS = 250
A_SHARE_CLOCK_MAX_OFFSET_MS = 2_000
A_SHARE_CLOCK_PREFERRED_JITTER_P95_MS = 100
A_SHARE_CLOCK_MAX_JITTER_P95_MS = 1_000

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], datetime]


def clock_corrected_time(value: datetime, offset_ms: int) -> datetime:
    """Project a local wall-clock timestamp onto the measured NTP timeline."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock-corrected timestamps must be timezone-aware")
    if isinstance(offset_ms, bool) or not isinstance(offset_ms, int):
        raise ValueError("clock offset must be an integer")
    return value + timedelta(milliseconds=offset_ms)


def accepted_clock_offset_ms(
    *,
    clock_sync_status: str,
    collector_clock_offset_ms: int | None,
    max_clock_offset_ms: int,
) -> int | None:
    """Return the offset only when the evidence is trusted by the hard policy."""
    if (
        clock_sync_status != "synchronized"
        or not isinstance(collector_clock_offset_ms, int)
        or isinstance(collector_clock_offset_ms, bool)
        or not isinstance(max_clock_offset_ms, int)
        or isinstance(max_clock_offset_ms, bool)
        or max_clock_offset_ms < 0
        or abs(collector_clock_offset_ms) > max_clock_offset_ms
    ):
        return None
    return collector_clock_offset_ms


def clock_jitter_quality_warning_codes(
    jitter_p95_ms: int,
    *,
    preferred_jitter_p95_ms: int = A_SHARE_CLOCK_PREFERRED_JITTER_P95_MS,
) -> tuple[str, ...]:
    """Return non-blocking quality notes for plan-bound clock history."""
    if jitter_p95_ms < 0 or preferred_jitter_p95_ms < 0:
        raise ValueError("clock jitter values must be non-negative")
    if jitter_p95_ms > preferred_jitter_p95_ms:
        return ("COLLECTOR_CLOCK_JITTER_ABOVE_PREFERRED",)
    return ()


class SystemClockEvidenceError(RuntimeError):
    """Raised when the host cannot produce trustworthy local clock evidence."""


@dataclass(frozen=True)
class SystemClockEvidence:
    """A sanitized projection of one systemd-timesyncd observation."""

    measured_at: datetime
    source: str
    timezone: str
    ntp_enabled: bool
    synchronized: bool
    collector_clock_offset_ms: int
    ntp_jitter_ms: int
    ntp_packet_count: int
    poll_interval_ms: int
    message_destination_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("measured_at", self.measured_at),
            ("message_destination_at", self.message_destination_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.source != "systemd-timesyncd-dbus.v1":
            raise ValueError("unsupported system clock evidence source")
        if self.timezone != "Asia/Shanghai":
            raise ValueError("system timezone must be Asia/Shanghai")
        if self.ntp_jitter_ms < 0 or self.ntp_packet_count <= 0 or self.poll_interval_ms <= 0:
            raise ValueError("invalid system clock evidence counters")

    @property
    def clock_sync_status(self) -> str:
        """Map host state to the existing qualification vocabulary."""
        return "synchronized" if self.ntp_enabled and self.synchronized else "unsynchronized"

    @property
    def message_age_ms(self) -> int:
        """Age of the NTP message on the same host wall clock, rounded outward."""
        delta = self.measured_at.astimezone(UTC) - self.message_destination_at.astimezone(UTC)
        microseconds = (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
        return _divide_round_away_from_zero(microseconds, 1_000)

    @property
    def accepted_clock_offset_ms(self) -> int | None:
        """Return the signed offset only when every default hard check accepts it."""
        if self.qualification_reason_codes():
            return None
        return accepted_clock_offset_ms(
            clock_sync_status=self.clock_sync_status,
            collector_clock_offset_ms=self.collector_clock_offset_ms,
            max_clock_offset_ms=A_SHARE_CLOCK_MAX_OFFSET_MS,
        )

    @property
    def corrected_measured_at(self) -> datetime | None:
        """Return canonical time only for a synchronized, hard-policy clock."""
        offset_ms = self.accepted_clock_offset_ms
        if offset_ms is None:
            return None
        return clock_corrected_time(self.measured_at, offset_ms)

    def qualification_reason_codes(
        self,
        *,
        max_clock_offset_ms: int = A_SHARE_CLOCK_MAX_OFFSET_MS,
    ) -> tuple[str, ...]:
        """Return capital-safe reasons that prevent this reading from qualifying."""
        if max_clock_offset_ms < 0:
            raise ValueError("max_clock_offset_ms must be non-negative")
        reasons: list[str] = []
        if self.clock_sync_status != "synchronized":
            reasons.append("COLLECTOR_CLOCK_UNSYNCHRONIZED")
        if self.message_age_ms < 0:
            reasons.append("COLLECTOR_CLOCK_MESSAGE_FROM_FUTURE")
        elif self.message_age_ms > self.poll_interval_ms + _CLOCK_MESSAGE_GRACE_MS:
            reasons.append("COLLECTOR_CLOCK_EVIDENCE_STALE")
        if abs(self.collector_clock_offset_ms) > max_clock_offset_ms:
            reasons.append("COLLECTOR_CLOCK_OFFSET_EXCEEDED")
        return tuple(reasons)

    def quality_warning_codes(
        self,
        *,
        preferred_clock_offset_ms: int = A_SHARE_CLOCK_PREFERRED_OFFSET_MS,
    ) -> tuple[str, ...]:
        """Return non-blocking quality notes for a synchronized usable clock."""
        if preferred_clock_offset_ms < 0:
            raise ValueError("preferred_clock_offset_ms must be non-negative")
        if self.qualification_reason_codes():
            return ()
        if abs(self.collector_clock_offset_ms) > preferred_clock_offset_ms:
            return ("COLLECTOR_CLOCK_OFFSET_ABOVE_PREFERRED",)
        return ()

    def to_dict(self) -> dict[str, object]:
        """Return stable JSON-safe evidence without raw command output."""
        reasons = self.qualification_reason_codes()
        warnings = self.quality_warning_codes()
        corrected = self.corrected_measured_at
        return {
            "schema_version": "system-clock-evidence.v2",
            "measured_at": self.measured_at.isoformat(),
            "corrected_measured_at": corrected.isoformat() if corrected is not None else None,
            "source": self.source,
            "timezone": self.timezone,
            "ntp_enabled": self.ntp_enabled,
            "synchronized": self.synchronized,
            "clock_sync_status": self.clock_sync_status,
            "collector_clock_offset_ms": self.collector_clock_offset_ms,
            "accepted_clock_offset_ms": self.accepted_clock_offset_ms,
            "clock_correction_applied": self.accepted_clock_offset_ms is not None,
            "preferred_clock_offset_ms": A_SHARE_CLOCK_PREFERRED_OFFSET_MS,
            "max_clock_offset_ms": A_SHARE_CLOCK_MAX_OFFSET_MS,
            "quality_status": "hard_block" if reasons else "warning" if warnings else "preferred",
            "quality_warning_codes": list(warnings),
            "hard_block_codes": list(reasons),
            "ntp_jitter_ms": self.ntp_jitter_ms,
            "ntp_packet_count": self.ntp_packet_count,
            "poll_interval_ms": self.poll_interval_ms,
            "message_age_ms": self.message_age_ms,
            "message_destination_at": self.message_destination_at.isoformat(),
        }


@dataclass(frozen=True)
class _NtpMessage:
    originate_us: int
    receive_us: int
    transmit_us: int
    destination_us: int
    packet_count: int
    jitter_us: int


class SystemdTimesyncClockEvidenceProbe:
    """Read clock truth from fixed local systemd commands with no shell or network call."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._runner = runner or subprocess.run
        self._clock = clock or _utc_now

    def measure(self) -> SystemClockEvidence:
        """Measure once or raise one sanitized fail-closed error."""
        try:
            status_output = self._run(_TIMEDATECTL_COMMAND)
            message_output = self._run(_BUSCTL_COMMAND)
            poll_interval_output = self._run(_BUSCTL_POLL_INTERVAL_COMMAND)
            status = _parse_timedate_status(status_output)
            message = _parse_ntp_message(message_output)
            poll_interval_us = _parse_uint64_property(poll_interval_output)
            measured_at = self._clock()
            _require_aware(measured_at)
            return SystemClockEvidence(
                measured_at=measured_at,
                source="systemd-timesyncd-dbus.v1",
                timezone=status["Timezone"],
                ntp_enabled=_yes_no(status["NTP"]),
                synchronized=_yes_no(status["NTPSynchronized"]),
                collector_clock_offset_ms=_offset_ms(message),
                ntp_jitter_ms=_ceil_non_negative_ms(message.jitter_us),
                ntp_packet_count=message.packet_count,
                poll_interval_ms=_ceil_non_negative_ms(poll_interval_us),
                message_destination_at=_from_epoch_microseconds(message.destination_us),
            )
        except SystemClockEvidenceError:
            raise
        except (OSError, subprocess.SubprocessError, TimeoutError) as error:
            raise SystemClockEvidenceError("SYSTEM_CLOCK_EVIDENCE_BACKEND_UNAVAILABLE") from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SystemClockEvidenceError("SYSTEM_CLOCK_EVIDENCE_INVALID") from error

    def _run(self, command: list[str]) -> str:
        env = dict(os.environ)
        env.update({"LANG": "C", "LC_ALL": "C"})
        completed = self._runner(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
            env=env,
        )
        return completed.stdout


def _parse_timedate_status(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"Timezone", "NTP", "NTPSynchronized"}:
            values[key] = value
    if set(values) != {"Timezone", "NTP", "NTPSynchronized"}:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_EVIDENCE_STATUS_INVALID")
    if values["Timezone"] != "Asia/Shanghai":
        raise SystemClockEvidenceError("SYSTEM_CLOCK_TIMEZONE_INVALID")
    _yes_no(values["NTP"])
    _yes_no(values["NTPSynchronized"])
    return values


def _parse_ntp_message(raw: str) -> _NtpMessage:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("type") != _TIMESYNCD_SIGNATURE:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 15:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    if not all(_is_int(data[index]) for index in (*range(7), *range(8, 12), 13, 14)):
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    if not isinstance(data[7], list) or not all(_is_int(item) for item in data[7]):
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    if not isinstance(data[12], bool) or data[12]:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_IGNORED")
    if data[0] not in {0, 1, 2} or data[1] not in {3, 4}:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    if data[2] != 4 or not 1 <= data[3] <= 15:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    if any(data[index] <= 0 for index in (8, 9, 10, 11, 13)) or data[14] < 0:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_MESSAGE_INVALID")
    return _NtpMessage(
        originate_us=data[8],
        receive_us=data[9],
        transmit_us=data[10],
        destination_us=data[11],
        packet_count=data[13],
        jitter_us=data[14],
    )


def _parse_uint64_property(raw: str) -> int:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("type") != "t":
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_PROPERTY_INVALID")
    value = payload.get("data")
    if not _is_int(value) or value <= 0:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_NTP_PROPERTY_INVALID")
    return value


def _offset_ms(message: _NtpMessage) -> int:
    # NTP offset = ((T2 - T1) + (T3 - T4)) / 2.  Values exposed by
    # systemd-timesyncd are integer microseconds since the Unix epoch.
    numerator = (message.receive_us - message.originate_us) + (
        message.transmit_us - message.destination_us
    )
    return _divide_round_away_from_zero(numerator, 2_000)


def _divide_round_away_from_zero(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return (numerator + denominator - 1) // denominator
    return -((-numerator + denominator - 1) // denominator)


def _ceil_non_negative_ms(microseconds: int) -> int:
    return (microseconds + 999) // 1_000


def _from_epoch_microseconds(value: int) -> datetime:
    seconds, microseconds = divmod(value, 1_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC) + timedelta(microseconds=microseconds)


def _yes_no(value: str) -> bool:
    if value == "yes":
        return True
    if value == "no":
        return False
    raise SystemClockEvidenceError("SYSTEM_CLOCK_EVIDENCE_STATUS_INVALID")


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SystemClockEvidenceError("SYSTEM_CLOCK_MEASUREMENT_TIME_INVALID")


def _utc_now() -> datetime:
    return datetime.now(UTC)
