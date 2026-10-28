"""Durable queue reservations and user-authored prompt versions."""

from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import os
from pathlib import Path
import uuid
from .state import atomic, read


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class OperatorStore:
    def __init__(self, state):
        self.state = Path(state)

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "queue.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    @contextmanager
    def control(self):
        """Serialize operator configuration with Start and other attached apps."""
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "control.lock").open("a") as control:
            try:
                fcntl.flock(control, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(
                    "Another app is changing profiles or submitting a flag; try again."
                ) from error
            yield

    @contextmanager
    def controlled(self):
        """Same lock ordering as profile edits: control first, queue second."""
        with self.control(), self.locked():
            yield

    def queue(self):
        items = read(self.state / "queue.json", [])
        for item in items:
            if item["state"] == "assigned" and item.get("run") and item.get("claim_id"):
                claim = Path(item["run"]) / "claims" / item["claim_id"]
                for name in ("submission-response.json", "giveup-response.json"):
                    if (claim / name).is_file() and read(claim / name):
                        item.update(
                            state="completed", closure_receipt=str(claim / name)
                        )
                        break
        return items

    def enqueue(self, claim_dir, profile_id=None):
        claim = Path(claim_dir)
        info = read(claim / "claim.json")
        result = read(claim / "result.json") or read(claim / "work/result.json")
        scores = []
        for receipt in (claim / "work/recorded-checks").glob("check-*.json"):
            data = read(receipt)
            check = data.get("checker_result", {})
            if check.get("status") == "match" and data.get("checker_exit_code") == 0:
                raise ValueError(
                    "This function already has a recorded MATCH; wait for its resolution."
                )
            if check.get("status") == "wip" and type(check.get("ndiff_insn")) is int:
                scores.append((check["ndiff_insn"], str(receipt)))
        record = Path(min(scores)[1]) if scores else None
        if not record or not record.resolve().is_relative_to(claim.resolve()):
            raise ValueError("No checked source is available for this queue item.")
        d = read(record)
        source = Path(d["source_path"])
        log = Path(d["log_path"])
        c = d["checker_result"]
        if (
            not source.resolve().is_relative_to(claim.resolve())
            or not log.resolve().is_relative_to(claim.resolve())
            or sha(source) != d["source_sha256"]
            or sha(log) != d["log_sha256"]
        ):
            raise ValueError("Source or evidence hash changed; queue entry refused.")
        if c.get("status") != "wip" or type(c.get("ndiff_insn")) is not int:
            raise ValueError("Queue requires a checked unfinished candidate.")
        target = {
            "address": info["address"],
            "bytes": info["size"],
            "record_path": str(record),
            "source_path": str(source),
            "source_sha256": d["source_sha256"],
            "instruction_differences": c.get("ndiff_insn"),
            "checked_at": d.get("finished_at"),
            "scope": "historical recorded comparator scope",
            "source": "operator queue",
            "claim_id": claim.name,
            "handoff_summary": result.get("residual_issue", ""),
        }
        with self.controlled():
            if profile_id is not None:
                cfg = read(self.state / "config.json")
                known = {p["id"] for p in cfg.get("profiles", [{"id": "default"}])}
                if profile_id not in known:
                    raise ValueError(
                        "The target profile no longer exists. Choose an existing profile."
                    )
            q = self.queue()
            if any(
                x["target"]["address"] == target["address"]
                and x["state"] in ("queued", "reserved", "assigned", "review_required")
                for x in q
            ):
                raise ValueError(
                    "That function is already queued, reserved or assigned."
                )
            item = {
                "id": uuid.uuid4().hex,
                "target": target,
                "profile_id": profile_id,
                "state": "queued",
                "at": now(),
            }
            q.append(item)
            atomic(self.state / "queue.json", q)
        return item

    def reserve(self, profile_id, worker, *, eligible=None, queue_enabled=True):
        from .routing import allowed

        with self.locked():
            q = self.queue()
            item = next(
                (
                    x
                    for x in q
                    if x["state"] == "queued"
                    and x.get("profile_id") in (None, profile_id)
                    and (queue_enabled or x.get("immediate", False))
                    and allowed(self.state, x["target"]["address"], profile_id)
                    and (eligible is None or eligible(x["target"]))
                ),
                None,
            )
            if item:
                item.update(state="reserved", worker=worker, reserved_at=now())
                atomic(self.state / "queue.json", q)
            return item

    def update(self, identifier, state, **data):
        with self.locked():
            q = self.queue()
            item = next(x for x in q if x["id"] == identifier)
            item.update(state=state, **data)
            atomic(self.state / "queue.json", q)

    def remove(self, identifier):
        with self.locked():
            q = self.queue()
            item = next(x for x in q if x["id"] == identifier)
            if item["state"] not in ("queued", "unavailable", "completed"):
                raise ValueError("An active reservation cannot be removed.")
            q.remove(item)
            atomic(self.state / "queue.json", q)

    def prompt(self, default, group=False):
        path = self.state / ("group-prompt.xml" if group else "prompt.xml")
        return path.read_text() if path.exists() else Path(default).read_text()

    def save_prompt(self, text, group=False):
        if not text.strip() or len(text.encode()) > 131072:
            raise ValueError("Prompt must contain 1–131,072 bytes.")
        with self.control():
            name = "group-prompt.xml" if group else "prompt.xml"
            path = self.state / name
            history = self.state / "prompt-history"
            history.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(text.encode()).hexdigest()
            version = history / (digest + ".xml")
            if not version.exists():
                with version.open("x", encoding="utf-8") as output:
                    output.write(text)
                    output.flush()
                    os.fsync(output.fileno())
            if sha(version) != digest:
                raise ValueError(
                    "Saved prompt history changed; preserve it for review."
                )
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8") as output:
                output.write(text)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            atomic(
                self.state / (name + ".metadata.json"),
                {"sha256": digest, "at": now(), "authored_by": "operator"},
            )
            return digest
