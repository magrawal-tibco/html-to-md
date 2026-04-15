"""
reporter.py — Structured logging and JSON report writing for all pipeline steps.

Each step receives a Reporter instance and calls:
  - reporter.info / .warning / .error  for console + run.log
  - reporter.skip(url, reason)          for skipped.log
  - reporter.fail(url, step, error)     for errors.log
  - reporter.finish()                   writes the step JSON report
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path


class Reporter:
    def __init__(self, run_dir: Path, step_name: str, dry_run: bool = False):
        self.run_dir = run_dir
        self.step_name = step_name
        self.dry_run = dry_run
        self.started_at = time.time()

        self._counts: dict[str, int] = {}
        self._errors: list[dict] = []
        self._skipped: list[dict] = []

        run_dir.mkdir(parents=True, exist_ok=True)

        # Root logger writes to run.log (shared across all steps in a run)
        self._logger = logging.getLogger(step_name)
        if not self._logger.handlers:
            self._logger.setLevel(logging.DEBUG)

            fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s",
                                    datefmt="%H:%M:%S")

            # Console handler
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            self._logger.addHandler(ch)

            # run.log — all messages
            fh = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            self._logger.addHandler(fh)

            # errors.log — ERROR and above only
            eh = logging.FileHandler(run_dir / "errors.log", encoding="utf-8")
            eh.setLevel(logging.ERROR)
            eh.setFormatter(fmt)
            self._logger.addHandler(eh)

        self._skipped_log = open(run_dir / "skipped.log", "a", encoding="utf-8")

    # ── logging shortcuts ──────────────────────────────────────────────────

    def info(self, msg: str):
        self._logger.info(msg)

    def debug(self, msg: str):
        self._logger.debug(msg)

    def warning(self, msg: str):
        self._logger.warning(msg)

    def error(self, msg: str):
        self._logger.error(msg)

    # ── structured events ──────────────────────────────────────────────────

    def count(self, key: str, n: int = 1):
        """Increment a named counter."""
        self._counts[key] = self._counts.get(key, 0) + n

    def skip(self, url: str, reason: str):
        """Record a skipped URL with reason."""
        self._skipped.append({"url": url, "reason": reason})
        self._skipped_log.write(f"SKIP\t{reason}\t{url}\n")
        self._skipped_log.flush()
        self._logger.debug(f"SKIP [{reason}] {url}")

    def fail(self, url: str, error: str, step: str = None):
        """Record a failed URL with error message."""
        entry = {"url": url, "step": step or self.step_name, "error": error}
        self._errors.append(entry)
        self._logger.error(f"FAIL {url} — {error}")

    # ── report writing ─────────────────────────────────────────────────────

    def finish(self) -> dict:
        """Write the step JSON report and return the stats dict."""
        elapsed = round(time.time() - self.started_at, 1)
        report = {
            "step": self.step_name,
            "dry_run": self.dry_run,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(),
            "duration_seconds": elapsed,
            "counts": self._counts,
            "error_count": len(self._errors),
            "skip_count": len(self._skipped),
            "errors": self._errors,
        }
        report_path = self.run_dir / f"{self.step_name}.json"
        if not self.dry_run:
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        self._skipped_log.close()
        self._logger.info(
            f"Step {self.step_name} done in {elapsed}s - "
            f"counts={self._counts} errors={len(self._errors)} skipped={len(self._skipped)}"
        )
        return report


def write_summary(run_dir: Path, phase: str, step_reports: list[dict], dry_run: bool = False):
    """Write the final summary.json rolling up all step reports."""
    total_errors = sum(r.get("error_count", 0) for r in step_reports)
    total_skipped = sum(r.get("skip_count", 0) for r in step_reports)
    total_duration = sum(r.get("duration_seconds", 0) for r in step_reports)

    # Flatten all errors from all steps
    all_errors = []
    for r in step_reports:
        all_errors.extend(r.get("errors", []))

    # Aggregate all counts across steps
    combined_counts: dict[str, int] = {}
    for r in step_reports:
        for k, v in r.get("counts", {}).items():
            combined_counts[k] = combined_counts.get(k, 0) + v

    summary = {
        "phase": phase,
        "run_dir": str(run_dir),
        "dry_run": dry_run,
        "total_duration_seconds": round(total_duration, 1),
        "total_errors": total_errors,
        "total_skipped": total_skipped,
        "counts": combined_counts,
        "steps": [
            {
                "step": r["step"],
                "duration_seconds": r.get("duration_seconds"),
                "counts": r.get("counts", {}),
                "error_count": r.get("error_count", 0),
                "skip_count": r.get("skip_count", 0),
            }
            for r in step_reports
        ],
        "errors": all_errors,
    }

    summary_path = run_dir / "summary.json"
    if not dry_run:
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                encoding="utf-8")

    return summary
