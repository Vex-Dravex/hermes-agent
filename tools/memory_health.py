import re
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = '\n§\n'
FRESH_DAYS = 30
AGING_DAYS = 90

NEGATION_PATTERN = re.compile(
    r"(?:does not|doesn't|don't|NOT|no longer|never|stopped|removed|deprecated)",
    re.IGNORECASE,
)
DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(
        r"\b("
        r"January|February|March|April|May|June|July|August|September|October|November|December"
        r")\s+(\d{4})\b",
        re.IGNORECASE,
    ),
)
VERSION_PATTERN = re.compile(r"\bv?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?\b")
VALUE_PATTERN = re.compile(
    r"\b([A-Z][\w./ -]{1,60}?|[a-z][\w./ -]{1,60}?)\s+is\s+([A-Z0-9][\w./ -]{0,60})",
    re.IGNORECASE,
)
USES_PATTERN = re.compile(
    r"\b([A-Z][\w./ -]{1,60}?|[a-z][\w./ -]{1,60}?)\s+uses\s+([A-Z0-9][\w./ -]{0,60})",
    re.IGNORECASE,
)


class MemoryHealthScanner:
    def __init__(self):
        self.memory_dir = get_hermes_home() / 'memory'

    def scan(self) -> Dict[str, Any]:
        memory_entries = self._load_entries("MEMORY.md")
        user_entries = self._load_entries("USER.md")

        contradictions_memory = self._find_contradictions(memory_entries)
        contradictions_user = self._find_contradictions(user_entries)
        staleness_memory = self._check_staleness(memory_entries)
        staleness_user = self._check_staleness(user_entries)
        report = self._generate_report(
            memory_entries,
            user_entries,
            contradictions_memory,
            contradictions_user,
            staleness_memory,
            staleness_user,
        )

        return {
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "memory_dir": str(self.memory_dir),
            "memory_entries": memory_entries,
            "user_entries": user_entries,
            "contradictions": {
                "memory": contradictions_memory,
                "user": contradictions_user,
            },
            "staleness": {
                "memory": staleness_memory,
                "user": staleness_user,
            },
            "report": report,
            "summary": {
                "memory_entries": len(memory_entries),
                "user_entries": len(user_entries),
                "contradictions": len(contradictions_memory) + len(contradictions_user),
                "stale_entries": len(staleness_memory["stale"]) + len(staleness_user["stale"]),
                "aging_entries": len(staleness_memory["aging"]) + len(staleness_user["aging"]),
            },
        }

    def _load_entries(self, filename: str) -> List[str]:
        path = self.memory_dir / filename
        if not path.exists():
            logger.debug("Memory file not found: %s", path)
            return []

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return []

        return [entry.strip() for entry in content.split(ENTRY_DELIMITER) if entry.strip()]

    def _find_contradictions(self, entries: List[str]) -> List[Dict]:
        contradictions: List[Dict[str, str]] = []

        for i, entry_a in enumerate(entries):
            for entry_b in entries[i + 1:]:
                reason = self._detect_conflict(entry_a, entry_b)
                if reason:
                    contradictions.append(
                        {
                            "entry_a": entry_a,
                            "entry_b": entry_b,
                            "reason": reason,
                        }
                    )

        return contradictions

    def _check_staleness(self, entries: List[str]) -> Dict[str, List[str]]:
        buckets = {"fresh": [], "aging": [], "stale": []}
        now = datetime.utcnow()

        for entry in entries:
            inferred_date = self._extract_entry_date(entry, now)
            if inferred_date is None:
                buckets["aging"].append(entry)
                continue

            age = now - inferred_date
            if age <= timedelta(days=FRESH_DAYS):
                buckets["fresh"].append(entry)
            elif age <= timedelta(days=AGING_DAYS):
                buckets["aging"].append(entry)
            else:
                buckets["stale"].append(entry)

        return buckets

    def _generate_report(
        self,
        memory_entries,
        user_entries,
        contradictions_memory,
        contradictions_user,
        staleness_memory,
        staleness_user,
    ) -> str:
        summary = {
            "memory_entries": len(memory_entries),
            "user_entries": len(user_entries),
            "contradictions": len(contradictions_memory) + len(contradictions_user),
            "stale_entries": len(staleness_memory["stale"]) + len(staleness_user["stale"]),
            "aging_entries": len(staleness_memory["aging"]) + len(staleness_user["aging"]),
        }

        lines = [
            "# Memory Health Report",
            "",
            f"Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z",
            f"Memory directory: `{self.memory_dir}`",
            "",
            "## Summary",
            f"- Memory entries: {summary['memory_entries']}",
            f"- User entries: {summary['user_entries']}",
            f"- Potential contradictions: {summary['contradictions']}",
            f"- Aging entries: {summary['aging_entries']}",
            f"- Stale entries: {summary['stale_entries']}",
            "",
            "## Contradictions",
            "",
            "### MEMORY.md",
        ]

        lines.extend(self._format_contradictions(contradictions_memory))
        lines.extend(["", "### USER.md"])
        lines.extend(self._format_contradictions(contradictions_user))
        lines.extend(["", "## Staleness", "", "### MEMORY.md"])
        lines.extend(self._format_staleness(staleness_memory))
        lines.extend(["", "### USER.md"])
        lines.extend(self._format_staleness(staleness_user))
        lines.extend(
            [
                "",
                "## Raw Summary",
                "```json",
                json.dumps(summary, indent=2, sort_keys=True),
                "```",
            ]
        )
        return "\n".join(lines)

    def _detect_conflict(self, entry_a: str, entry_b: str) -> Optional[str]:
        normalized_a = " ".join(entry_a.split())
        normalized_b = " ".join(entry_b.split())
        topic_a = self._entry_topic(entry_a).lower()
        topic_b = self._entry_topic(entry_b).lower()

        uses_a = self._extract_uses_claim(normalized_a)
        uses_b = self._extract_uses_claim(normalized_b)
        if uses_a and uses_b and uses_a[0] == uses_b[0] and uses_a[1] == uses_b[1]:
            if uses_a[2] != uses_b[2]:
                return f"Negation conflict on '{uses_a[0]} uses {uses_a[1]}'"

        value_a = self._extract_value_claim(normalized_a)
        value_b = self._extract_value_claim(normalized_b)
        if value_a and value_b and value_a[0] == value_b[0] and value_a[1] != value_b[1]:
            return f"Value conflict on '{value_a[0]} is ...'"

        if topic_a == topic_b:
            neg_a = bool(NEGATION_PATTERN.search(normalized_a))
            neg_b = bool(NEGATION_PATTERN.search(normalized_b))
            if neg_a != neg_b:
                return f"Topic '{self._entry_topic(entry_a)}' has opposing negation patterns"

        return None

    def _entry_topic(self, entry: str) -> str:
        first_line = entry.strip().splitlines()[0].strip()
        return first_line or entry.strip()[:80]

    def _extract_uses_claim(self, entry: str) -> Optional[Tuple[str, str, bool]]:
        match = USES_PATTERN.search(entry)
        if not match:
            return None
        subject = self._normalize_phrase(match.group(1))
        obj = self._normalize_phrase(match.group(2))
        is_negative = bool(NEGATION_PATTERN.search(entry))
        return subject, obj, is_negative

    def _extract_value_claim(self, entry: str) -> Optional[Tuple[str, str]]:
        match = VALUE_PATTERN.search(entry)
        if not match:
            return None
        subject = self._normalize_phrase(match.group(1))
        value = self._normalize_phrase(match.group(2))
        return subject, value

    def _normalize_phrase(self, phrase: str) -> str:
        return re.sub(r"\s+", " ", phrase.strip(" .,:;!-")).lower()

    def _extract_entry_date(self, entry: str, now: datetime) -> Optional[datetime]:
        dates: List[datetime] = []

        for year, month, day in DATE_PATTERNS[0].findall(entry):
            try:
                dates.append(datetime(int(year), int(month), int(day)))
            except ValueError:
                continue

        for month_name, year in DATE_PATTERNS[1].findall(entry):
            try:
                dates.append(datetime.strptime(f"{month_name} {year}", "%B %Y"))
            except ValueError:
                continue

        if dates:
            return max(dates)

        version_match = VERSION_PATTERN.search(entry)
        if version_match:
            major = int(version_match.group(1))
            minor = int(version_match.group(2))
            if 2000 <= major <= now.year + 1 and 1 <= minor <= 12:
                return datetime(major, minor, 1)

        return None

    def _format_contradictions(self, contradictions: List[Dict[str, str]]) -> List[str]:
        if not contradictions:
            return ["No contradictions found."]

        lines: List[str] = []
        for item in contradictions:
            lines.append(f"- Reason: {item['reason']}")
            lines.append(f"  - Entry A: {item['entry_a']}")
            lines.append(f"  - Entry B: {item['entry_b']}")
        return lines

    def _format_staleness(self, staleness: Dict[str, List[str]]) -> List[str]:
        lines = [
            f"- Fresh: {len(staleness['fresh'])}",
            f"- Aging: {len(staleness['aging'])}",
            f"- Stale: {len(staleness['stale'])}",
        ]

        if staleness["stale"]:
            lines.append("- Stale entries:")
            lines.extend([f"  - {self._entry_topic(entry)}" for entry in staleness["stale"]])

        return lines


def run_health_scan() -> str:
    scanner = MemoryHealthScanner()
    result = scanner.scan()
    report = result["report"]
    report_path = get_hermes_home() / "memory_health_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report


def get_health_summary() -> Dict[str, Any]:
    scanner = MemoryHealthScanner()
    result = scanner.scan()
    return dict(result["summary"])

