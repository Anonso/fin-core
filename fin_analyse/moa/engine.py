"""MoA deliberation engine."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from fin_analyse.moa._utils import clamp_float, list_str
from fin_analyse.moa.contract import MOA_CONTRACT_VERSION, MoAKernelContract
from fin_analyse.moa.models import (
    MoAReferenceOutput,
    MoAReferenceRole,
    MoARequest,
    MoAResult,
    TextCompletionBackend,
)
from fin_analyse.utils.llm_text import strip_markdown_fences

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ResolvedTimeout:
    effective_seconds: float
    policy: str
    floor_seconds: float | None = None
    cap_seconds: float | None = None
    inputs: dict[str, Any] | None = None


class MoAEngine:
    """Run internal MoA deliberation behind a small interface.

    Supports configurable reference role concurrency, per-completion timeouts,
    and latency observability.
    """

    def __init__(
        self,
        *,
        aggregator_backend: TextCompletionBackend | None = None,
        aggregator_backend_name: str = "aggregator",
        reference_backends: dict[str, TextCompletionBackend] | None = None,
        max_reference_workers: int = 1,
        reference_timeout_seconds: float | None = None,
        aggregator_timeout_seconds: float | None = None,
    ) -> None:
        self.aggregator_backend = aggregator_backend
        self.aggregator_backend_name = aggregator_backend_name
        self.reference_backends = reference_backends or {}
        self.max_reference_workers = max(1, max_reference_workers)
        self.reference_timeout_seconds = reference_timeout_seconds
        self.aggregator_timeout_seconds = aggregator_timeout_seconds

    def deliberate(self, request: MoARequest) -> MoAResult:
        t_start = time.monotonic()

        # ── MoA Kernel Contract: validate request before any backend call ──
        contract_check = MoAKernelContract.validate_request(request)
        if not contract_check.ok:
            return self._fallback(
                request,
                [],
                reason=f"moa_request_contract_invalid: {contract_check.reason}",
                warnings=[],
                latency_summary=self._empty_latency_summary(time.monotonic() - t_start),
            )

        reference_outputs = list(request.precomputed_references)
        warnings: list[str] = []

        # ── Run reference roles (parallel when max_reference_workers > 1) ──
        role_outputs = self._run_references_parallel(request)
        reference_outputs.extend(role_outputs)

        for output in role_outputs:
            if not output.ok:
                warnings.append(f"{output.role} failed: {output.error}")

        successful_outputs = [
            output for output in reference_outputs if output.ok and output.content.strip()
        ]
        if len(successful_outputs) < request.min_reference_success:
            return self._fallback(
                request,
                reference_outputs,
                reason=(
                    "reference_success_below_threshold: "
                    f"{len(successful_outputs)} < {request.min_reference_success}"
                ),
                warnings=warnings,
                latency_summary=self._build_latency_summary(
                    t_start, role_outputs, aggregator_latency_ms=0.0
                ),
            )

        # ── Resolve recipe-owned or engine-default aggregator timeout ──
        effective_timeout, timeout_meta = self._resolve_aggregator_timeout(request)

        if self.aggregator_backend is None:
            return self._fallback(
                request,
                reference_outputs,
                reason="aggregator_backend_not_configured",
                warnings=warnings,
                latency_summary=self._build_latency_summary(
                    t_start,
                    role_outputs,
                    aggregator_latency_ms=0.0,
                    effective_timeout=effective_timeout,
                    timeout_meta=timeout_meta,
                ),
            )

        prompt = self._compose_aggregator_prompt(request, successful_outputs)
        t_agg_start = time.monotonic()
        try:
            raw = self._run_with_timeout(
                self.aggregator_backend.complete,
                prompt,
                effective_timeout,
                "aggregator",
            )
        except TimeoutError as exc:
            logger.warning("Aggregator timed out")
            return self._fallback(
                request,
                reference_outputs,
                reason="aggregator_timeout",
                warnings=warnings + [str(exc)],
                latency_summary=self._build_latency_summary(
                    t_start,
                    role_outputs,
                    aggregator_latency_ms=(time.monotonic() - t_agg_start) * 1000,
                    effective_timeout=effective_timeout,
                    timeout_meta=timeout_meta,
                ),
            )
        except Exception:
            logger.exception("Aggregator backend failed")
            return self._fallback(
                request,
                reference_outputs,
                reason="aggregator_error",
                warnings=warnings,
                latency_summary=self._build_latency_summary(
                    t_start,
                    role_outputs,
                    aggregator_latency_ms=(time.monotonic() - t_agg_start) * 1000,
                    effective_timeout=effective_timeout,
                    timeout_meta=timeout_meta,
                ),
            )
        aggregator_latency_ms = (time.monotonic() - t_agg_start) * 1000

        try:
            final = self._parse_json_object(raw)
        except ValueError:
            logger.exception("Aggregator returned invalid JSON")
            return self._fallback(
                request,
                reference_outputs,
                reason="aggregator_json_error",
                warnings=warnings,
                latency_summary=self._build_latency_summary(
                    t_start,
                    role_outputs,
                    aggregator_latency_ms,
                    effective_timeout=effective_timeout,
                    timeout_meta=timeout_meta,
                ),
            )

        # ── MoA Kernel Contract: validate required fields ──────────────
        contract_check = MoAKernelContract.validate_final(final, request)
        if not contract_check.ok:
            logger.warning("Contract validation failed: %s", contract_check.reason)
            return self._fallback(
                request,
                reference_outputs,
                reason=f"contract_validation_failed: {contract_check.reason}",
                warnings=warnings + contract_check.missing_required,
                latency_summary=self._build_latency_summary(
                    t_start,
                    role_outputs,
                    aggregator_latency_ms,
                    effective_timeout=effective_timeout,
                    timeout_meta=timeout_meta,
                ),
            )

        # ── Normalize boundary fields ──────────────────────────────────
        final = MoAKernelContract.normalize_boundary(final)

        result_warnings = warnings + list_str(final.get("warnings", []))
        latency_summary = self._build_latency_summary(
            t_start,
            role_outputs,
            aggregator_latency_ms,
            effective_timeout=effective_timeout,
            timeout_meta=timeout_meta,
        )

        return MoAResult(
            task_id=request.task_id,
            task_type=request.task_type,
            status="ok",
            final=final,
            reference_outputs=reference_outputs,
            consensus=list_str(final.get("consensus", [])),
            disagreements=list_str(final.get("disagreements", [])),
            blind_spots=list_str(final.get("blind_spots", [])),
            confidence=clamp_float(final.get("confidence", 0.0), 0.0, 1.0),
            warnings=result_warnings,
            data_gaps=list_str(final.get("data_gaps", [])),
            source_boundary=final.get("source_boundary", {}),
            advisory_only=bool(final.get("source_boundary", {}).get("advisory_only", True)),
            risk_boundary=final.get("risk_boundary", {}),
            metadata={
                "aggregator_backend": self.aggregator_backend_name,
                "moa_contract_version": MOA_CONTRACT_VERSION,
                "latency_summary": latency_summary,
                **request.metadata,
            },
        )

    # ── Reference execution ──────────────────────────────────────────────

    def _run_references_parallel(self, request: MoARequest) -> list[MoAReferenceOutput]:
        """Run reference roles, parallel when max_reference_workers > 1.

        Returns outputs in the same order as request.reference_roles.
        Each role receives the recipe-owned request timeout when present.
        """
        roles = request.reference_roles
        if not roles:
            return []

        # Compute per-role effective timeouts
        role_timeouts = [self._resolve_reference_timeout(request) for _ in roles]

        if self.max_reference_workers <= 1 or len(roles) <= 1:
            # Serial path
            return [
                self._run_reference(role, effective_timeout=timeout)
                for role, timeout in zip(roles, role_timeouts, strict=True)
            ]

        # Parallel path — preserve input order via index map
        indexed: dict[int, MoAReferenceOutput] = {}
        with ThreadPoolExecutor(max_workers=self.max_reference_workers) as executor:
            future_to_idx = {
                executor.submit(
                    self._run_reference, role, effective_timeout=role_timeouts[idx]
                ): idx
                for idx, role in enumerate(roles)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    indexed[idx] = future.result()
                except Exception:
                    role = roles[idx]
                    resolved_timeout = role_timeouts[idx]
                    logger.exception("Reference role %s crashed", role.name)
                    indexed[idx] = MoAReferenceOutput(
                        role=role.name,
                        backend_name=role.backend_name or role.name,
                        content="",
                        ok=False,
                        error="Reference execution crashed",
                        timeout_seconds=resolved_timeout.effective_seconds
                        if resolved_timeout is not None
                        else self.reference_timeout_seconds,
                    )

        return [indexed[i] for i in range(len(roles))]

    def _run_reference(
        self, role: MoAReferenceRole, *, effective_timeout: Any = None
    ) -> MoAReferenceOutput:
        backend_name = role.backend_name or role.name
        backend = self.reference_backends.get(backend_name)

        # Resolve effective timeout
        if effective_timeout is not None and hasattr(effective_timeout, "effective_seconds"):
            timeout_s: float | None = effective_timeout.effective_seconds
            timeout_policy = effective_timeout.policy
            timeout_floor = effective_timeout.floor_seconds
            timeout_cap = effective_timeout.cap_seconds
            timeout_inputs = effective_timeout.inputs
        else:
            timeout_s = self.reference_timeout_seconds
            timeout_policy = None
            timeout_floor = None
            timeout_cap = None
            timeout_inputs = None

        if backend is None:
            return MoAReferenceOutput(
                role=role.name,
                backend_name=backend_name,
                content="",
                ok=False,
                error=f"reference backend not configured: {backend_name}",
                timeout_seconds=timeout_s,
                timeout_policy=timeout_policy,
                timeout_floor_seconds=timeout_floor,
                timeout_cap_seconds=timeout_cap,
                timeout_inputs=timeout_inputs,
            )

        t0 = time.monotonic()
        try:
            content = self._run_with_timeout(backend.complete, role.prompt, timeout_s, role.name)
            latency_ms = (time.monotonic() - t0) * 1000
            if not self._reference_content_looks_usable(content):
                return MoAReferenceOutput(
                    role=role.name,
                    backend_name=backend_name,
                    content=content,
                    ok=False,
                    error="empty_or_unusable_response",
                    latency_ms=latency_ms,
                    timeout_seconds=timeout_s,
                    timeout_policy=timeout_policy,
                    timeout_floor_seconds=timeout_floor,
                    timeout_cap_seconds=timeout_cap,
                    timeout_inputs=timeout_inputs,
                )
            return MoAReferenceOutput(
                role=role.name,
                backend_name=backend_name,
                content=content,
                ok=True,
                latency_ms=latency_ms,
                timeout_seconds=timeout_s,
                timeout_policy=timeout_policy,
                timeout_floor_seconds=timeout_floor,
                timeout_cap_seconds=timeout_cap,
                timeout_inputs=timeout_inputs,
            )
        except TimeoutError:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.warning("Reference role %s timed out after %.1fs", role.name, latency_ms / 1000)
            return MoAReferenceOutput(
                role=role.name,
                backend_name=backend_name,
                content="",
                ok=False,
                error=f"timed out after {latency_ms / 1000:.1f}s",
                latency_ms=latency_ms,
                timed_out=True,
                timeout_seconds=timeout_s,
                timeout_policy=timeout_policy,
                timeout_floor_seconds=timeout_floor,
                timeout_cap_seconds=timeout_cap,
                timeout_inputs=timeout_inputs,
            )
        except Exception:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.exception("Reference backend %s failed", backend_name)
            return MoAReferenceOutput(
                role=role.name,
                backend_name=backend_name,
                content="",
                ok=False,
                error="Backend execution failed",
                latency_ms=latency_ms,
                timeout_seconds=timeout_s,
                timeout_policy=timeout_policy,
                timeout_floor_seconds=timeout_floor,
                timeout_cap_seconds=timeout_cap,
                timeout_inputs=timeout_inputs,
            )

    @staticmethod
    def _reference_content_looks_usable(content: str) -> bool:
        stripped = str(content or "").strip()
        return bool(stripped) and stripped != "[]"

    # ── Timeout helper ───────────────────────────────────────────────────

    @staticmethod
    def _run_with_timeout(
        fn: Any,
        prompt: str,
        timeout_seconds: float | None,
        label: str,
    ) -> str:
        """Run fn(prompt) with an optional timeout via threading.

        Raises TimeoutError if the call exceeds timeout_seconds.
        Note: this cancels at the thread level; underlying HTTP connections
        may not be terminated unless the backend client has its own timeout.
        """
        if timeout_seconds is None or timeout_seconds <= 0:
            return str(fn(prompt))

        import threading

        result_container: list[str] = []
        error_container: list[Exception] = []

        def _target() -> None:
            try:
                result_container.append(fn(prompt))
            except Exception as exc:
                error_container.append(exc)

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        t.join(timeout=timeout_seconds)

        if t.is_alive():
            raise TimeoutError(f"{label} timed out after {timeout_seconds}s")

        if error_container:
            raise error_container[0]

        if result_container:
            return result_container[0]

        raise RuntimeError(f"{label} returned no result")

    # ── Prompt composition ───────────────────────────────────────────────

    def _compose_aggregator_prompt(
        self,
        request: MoARequest,
        successful_outputs: list[MoAReferenceOutput],
    ) -> str:
        context_json = json.dumps(request.context, ensure_ascii=False, indent=2, default=str)
        schema_json = json.dumps(
            request.expected_schema or {},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        references_text = "\n\n".join(
            f"### {output.role} ({output.backend_name})\n{output.content}"
            for output in successful_outputs
        )
        return (
            f"{request.aggregator_prompt}\n\n"
            f"## Context\n```json\n{context_json}\n```\n\n"
            f"## Expected JSON schema\n```json\n{schema_json}\n```\n\n"
            f"## Reference outputs\n{references_text}\n\n"
            "Return only one JSON object. Do not wrap it in markdown."
        )

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        # Step 1: Try standard fenced JSON parse
        clean = strip_markdown_fences(raw)
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            pass
        else:
            return _unwrap_json_object(data)

        # Step 2: Extract first balanced JSON object from text (handles
        # models that wrap the JSON in explanatory prose).
        obj_text = _extract_first_json_object(clean)
        if obj_text is None:
            raise ValueError("aggregator output is not an object")
        try:
            data = json.loads(obj_text)
        except json.JSONDecodeError:
            raise ValueError("aggregator output is not an object") from None
        return _unwrap_json_object(data)

    # ── Bounded timeout resolution ────────────────────────────────────────

    def _resolve_aggregator_timeout(
        self,
        request: MoARequest,
    ) -> tuple[float, dict[str, Any]]:
        """Prefer the bounded recipe timeout; retain engine default for other callers."""
        if request.aggregator_timeout_seconds is not None:
            return request.aggregator_timeout_seconds, {
                "aggregator_timeout_policy": "request_bounded"
            }
        return self.aggregator_timeout_seconds or 300.0, {}

    def _resolve_reference_timeout(
        self,
        request: MoARequest,
    ) -> _ResolvedTimeout | None:
        """Resolve one request-owned reference budget without level metadata."""
        if request.reference_timeout_seconds is None:
            return None
        return _ResolvedTimeout(
            effective_seconds=request.reference_timeout_seconds,
            policy="request_bounded",
        )

    # ── Latency summary ──────────────────────────────────────────────────

    def _build_latency_summary(
        self,
        t_start: float,
        role_outputs: list[MoAReferenceOutput],
        aggregator_latency_ms: float,
        *,
        effective_timeout: float | None = None,
        timeout_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        total_latency_ms = (time.monotonic() - t_start) * 1000
        ref_entries: list[dict[str, Any]] = [
            {
                "role": o.role,
                "backend_name": o.backend_name,
                "ok": o.ok,
                "latency_ms": o.latency_ms,
                "timed_out": o.timed_out,
                "error": o.error if not o.ok else "",
                "timeout_seconds": o.timeout_seconds,
                "timeout_policy": o.timeout_policy,
                **(
                    {"timeout_floor_seconds": o.timeout_floor_seconds}
                    if o.timeout_floor_seconds is not None
                    else {}
                ),
                **(
                    {"timeout_cap_seconds": o.timeout_cap_seconds}
                    if o.timeout_cap_seconds is not None
                    else {}
                ),
                **({"timeout_inputs": o.timeout_inputs} if o.timeout_inputs is not None else {}),
            }
            for o in role_outputs
        ]
        success_count = sum(1 for o in role_outputs if o.ok)
        timeout_count = sum(1 for o in role_outputs if o.timed_out)

        summary: dict[str, Any] = {
            "reference_roles": ref_entries,
            "aggregator_latency_ms": round(aggregator_latency_ms, 2),
            "total_latency_ms": round(total_latency_ms, 2),
            "max_reference_workers": self.max_reference_workers,
            "reference_timeout_seconds": self.reference_timeout_seconds,
            "aggregator_timeout_seconds": (
                effective_timeout
                if effective_timeout is not None
                else self.aggregator_timeout_seconds
            ),
            "reference_success_count": success_count,
            "reference_total_count": len(role_outputs),
            "reference_timeout_count": timeout_count,
        }

        if timeout_meta:
            summary.update(timeout_meta)

        return summary

    def _empty_latency_summary(
        self, elapsed_s: float, *, timeout_meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "reference_roles": [],
            "aggregator_latency_ms": 0.0,
            "total_latency_ms": round(elapsed_s * 1000, 2),
            "max_reference_workers": self.max_reference_workers,
            "reference_timeout_seconds": self.reference_timeout_seconds,
            "aggregator_timeout_seconds": self.aggregator_timeout_seconds,
            "reference_success_count": 0,
            "reference_total_count": 0,
            "reference_timeout_count": 0,
        }
        if timeout_meta:
            summary.update(timeout_meta)
        return summary

    # ── Fallback ─────────────────────────────────────────────────────────

    @staticmethod
    def _fallback(
        request: MoARequest,
        reference_outputs: list[MoAReferenceOutput],
        *,
        reason: str,
        warnings: list[str],
        latency_summary: dict[str, Any] | None = None,
    ) -> MoAResult:
        meta = dict(request.metadata)
        meta.setdefault("moa_contract_version", MOA_CONTRACT_VERSION)
        if latency_summary is not None:
            meta["latency_summary"] = latency_summary
        return MoAResult(
            task_id=request.task_id,
            task_type=request.task_type,
            status="fallback",
            final={},
            reference_outputs=reference_outputs,
            consensus=[],
            disagreements=[],
            blind_spots=[],
            confidence=0.0,
            warnings=[*warnings, reason],
            fallback_reason=reason,
            data_gaps=[reason],
            source_boundary={"advisory_only": True},
            advisory_only=True,
            risk_boundary={"human_confirmation_required": True},
            metadata=meta,
        )


# ── JSON parsing helpers ───────────────────────────────────────────────────────


def _unwrap_json_object(data: Any) -> dict[str, Any]:
    """Accept a single dict; also accept [single_dict] by unwrapping.

    Rejects empty arrays ([]), multi-element arrays, and non-dict values.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        if len(data) == 0:
            raise ValueError("aggregator output is an empty array")
        if len(data) == 1 and isinstance(data[0], dict):
            return data[0]
    raise ValueError("aggregator output is not an object")


def _extract_first_json_object(text: str) -> str | None:
    """Extract the first balanced ``{...}`` JSON object from *text*.

    Returns ``None`` when no balanced object can be found.
    """
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None
